# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canary: logs at DEBUG never contain passwords, tokens, codes, keys or link tokens."""

from __future__ import annotations

import logging
import re

import pytest
from authkit import csrf_of, login, oauth_flow, web  # noqa: F401

from wwyd.auth import accounts
from wwyd.logs import install_privacy_filter

pytestmark = pytest.mark.anyio

CANARY_PASSWORD = "zq7x-canary-41b9-password"


async def test_no_secrets_in_debug_logs(web, caplog):  # noqa: F811
    install_privacy_filter()
    caplog.set_level(logging.DEBUG)
    secrets: list[str] = [CANARY_PASSWORD]
    token = accounts.create_link(web.conn, "invite")
    secrets.append(token)
    async with web.browser() as b:
        page = await b.get("/invite", params={"t": token})
        await b.post(
            "/invite",
            data={
                "csrf": csrf_of(page.text),
                "t": token,
                "login": "canary@example.com",
                "password": CANARY_PASSWORD,
                "password2": CANARY_PASSWORD,
            },
        )
    async with web.browser() as b:
        await login(b, "canary@example.com", "zq7x-wrong-password")
        await login(b, "canary@example.com", CANARY_PASSWORD)
        tok = await oauth_flow(b)
        secrets += [tok["access_token"], tok["refresh_token"], tok["code"]]
        page = await b.get("/account")
        resp = await b.post(
            "/account/keys", data={"csrf": csrf_of(page.text), "name": "k", "scope": "owner"}
        )
        key = re.search(r"wwyd_[a-z0-9]{8}_[A-Za-z0-9_-]+", resp.text).group(0)
        secrets.append(key)
        await b.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": tok["client_id"],
            },
        )
        await b.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": tok["client_id"],
            },
        )
        await b.post("/mcp", json={}, headers={"Authorization": "Bearer zq7x-bogus-bearer"})
        secrets.append("zq7x-bogus-bearer")
        secrets.append("zq7x-wrong-password")
    async with web.mcp(key) as c:
        await c.list_tools()
    text = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert caplog.records, "expected some log output"
    for secret in secrets:
        assert secret not in text
    assert "zq7x" not in text
