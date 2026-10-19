# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for HTTP/auth tests: an in-process app, a browser-like client, the OAuth dance.

Everything runs in one event loop: httpx (pages, OAuth endpoints) and httpx2 (the MCP SDK's
client transport) both talk to the ASGI app directly; the app's lifespan is entered by hand.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from wwyd.app import AppState, create_app
from wwyd.auth import accounts, clock
from wwyd.config import Config
from wwyd.db import open_db

PUBLIC_URL = "http://127.0.0.1:8740"
PASSWORD = "correct horse battery"
CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)  # SDK handlers compare expiries with the real time

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def fake_clock(monkeypatch) -> FakeClock:
    fc = FakeClock()
    monkeypatch.setattr(clock, "now", fc)
    return fc


@dataclass
class Web:
    app: Any
    state: AppState
    config: Config
    stderr: list[str] = field(default_factory=list)

    @property
    def conn(self):  # noqa: ANN201
        return self.state.conn

    def browser(self, ip: str = "203.0.113.5") -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(ip, 50000))
        return httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL, follow_redirects=False)

    def user(
        self, login: str = "owner@example.com", password: str = PASSWORD, admin: bool | None = None
    ):  # noqa: ANN201
        user = accounts.create_account(self.conn, login, accounts.hash_password(password))
        if admin is not None:
            self.conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(admin), user.id))
        return accounts.user_by_id(self.conn, user.id)

    @contextlib.asynccontextmanager
    async def mcp(self, token: str, ip: str = "203.0.113.5") -> AsyncIterator[Client]:
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app, client=(ip, 50001)),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        async with http, Client(streamable_http_client(f"{PUBLIC_URL}/mcp", http_client=http)) as c:
            yield c


async def make_web(**config_kw: Any) -> tuple[Web, contextlib.AsyncExitStack]:
    import tempfile

    tmp = tempfile.mkdtemp()
    extra = {k: config_kw.pop(k) for k in list(config_kw) if k.startswith("cimd_")}
    config_kw.setdefault("public_url", PUBLIC_URL)
    config = Config(**config_kw)
    conn = open_db(f"{tmp}/wwyd.db")
    app = create_app(config, conn, **extra)
    stack = contextlib.AsyncExitStack()
    await stack.enter_async_context(app.router.lifespan_context(app))
    return Web(app, app.state.wwyd, config), stack


@pytest.fixture
async def web() -> AsyncIterator[Web]:
    w, stack = await make_web()
    async with stack:
        yield w
    w.conn.close()


def csrf_of(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "no csrf field on the page"
    return match.group(1)


async def login(
    b: httpx.AsyncClient, login: str = "owner@example.com", password: str = PASSWORD
) -> httpx.Response:
    page = await b.get("/login")
    return await b.post(
        "/login",
        data={"csrf": csrf_of(page.text), "login": login, "password": password, "next": "/account"},
    )


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


async def register(
    b: httpx.AsyncClient, redirect_uri: str = "https://claude.ai/api/mcp/auth_callback", **meta: Any
) -> dict:
    body = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Test App",
        **meta,
    }
    resp = await b.post("/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def authorize(
    b: httpx.AsyncClient,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    *,
    scope: str | None = None,
    state: str = "st4te",
    resource: str | None = PUBLIC_URL + "/mcp",
    method: str | None = "S256",
) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "state": state,
    }
    if method:
        params["code_challenge_method"] = method
    if scope:
        params["scope"] = scope
    if resource:
        params["resource"] = resource
    return await b.get("/authorize", params=params)


async def consent(
    b: httpx.AsyncClient,
    consent_url: str,
    decision: str = "allow",
    scope: str = "owner",
    audiences: list[str] | None = None,
) -> httpx.Response:
    page = await b.get(consent_url)
    assert page.status_code == 200, page.text
    data: dict[str, Any] = {
        "csrf": csrf_of(page.text),
        "req": parse_qs(urlsplit(consent_url).query)["req"][0],
        "decision": decision,
        "scope": scope,
    }
    if audiences:
        data["audiences[]"] = audiences
    return await b.post("/consent", data=data)


async def oauth_flow(
    b: httpx.AsyncClient,
    *,
    scope: str = "owner",
    audiences: list[str] | None = None,
    redirect_uri: str = "https://claude.ai/api/mcp/auth_callback",
    requested: str | None = None,
) -> dict[str, Any]:
    """Register, authorize, consent (signed in already) and exchange; returns the token JSON
    plus ``client_id``."""
    client = await register(b, redirect_uri)
    verifier, challenge = pkce()
    resp = await authorize(b, client["client_id"], redirect_uri, challenge, scope=requested)
    assert resp.status_code == 302, resp.text
    resp = await consent(b, resp.headers["location"], scope=scope, audiences=audiences)
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith(redirect_uri)
    q = parse_qs(urlsplit(location).query)
    assert q["state"] == ["st4te"]
    tok = await b.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    return {**tok.json(), "client_id": client["client_id"], "code": q["code"][0]}
