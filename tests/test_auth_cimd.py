# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client ID metadata documents (CIMD), with httpx and DNS mocked."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from authkit import authorize, consent, fake_clock, login, make_web, pkce  # noqa: F401

pytestmark = pytest.mark.anyio

CID = "https://app.example/oauth/client.json"
REDIRECT = "https://app.example/callback"


def _doc(**over):  # noqa: ANN003, ANN202
    return {
        "client_id": CID,
        "client_name": "Example App",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **over,
    }


class Server:
    def __init__(
        self, doc=None, ips=("93.184.216.34",), status=200, body: bytes | None = None, headers=None
    ):  # noqa: ANN001
        self.doc, self.ips, self.status, self.body, self.headers = (
            doc or _doc(),
            list(ips),
            status,
            body,
            headers,
        )
        self.fetches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        assert str(request.url) == CID
        body = self.body if self.body is not None else json.dumps(self.doc).encode()
        return httpx.Response(
            self.status, content=body, headers=self.headers or {"content-type": "application/json"}
        )

    async def resolve(self, host: str, port: int) -> list[str]:
        return self.ips


async def _web(server: Server):  # noqa: ANN202
    return await make_web(
        cimd_transport=httpx.MockTransport(server.handler), cimd_resolver=server.resolve
    )


async def test_good_document_full_flow():
    srv = Server()
    w, stack = await _web(srv)
    async with stack:
        w.user()
        async with w.browser() as b:
            await login(b)
            verifier, challenge = pkce()
            resp = await authorize(b, CID, REDIRECT, challenge)
            assert resp.status_code == 302 and "/consent" in resp.headers["location"], resp.text
            page = await b.get(resp.headers["location"])
            assert "Example App" in page.text and CID in page.text
            resp = await consent(b, resp.headers["location"])
            code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
            tok = await b.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT,
                    "client_id": CID,
                    "code_verifier": verifier,
                },
            )
            assert tok.status_code == 200, tok.text
        row = w.conn.execute(
            "SELECT source FROM oauth_clients WHERE client_id = ?", (CID,)
        ).fetchone()
        assert row["source"] == "cimd"
        assert srv.fetches == 1  # cached afterwards


@pytest.mark.parametrize(
    "server",
    [
        Server(doc=_doc(client_id="https://other.example/client.json")),  # mismatched client_id
        Server(ips=("10.0.0.5",)),  # private
        Server(ips=("127.0.0.1",)),  # loopback
        Server(ips=("169.254.169.254",)),  # link-local (cloud metadata)
        Server(ips=("93.184.216.34", "192.168.1.1")),  # any private address poisons it
        Server(ips=("::1",)),
        Server(ips=("::ffff:10.0.0.1",)),
        Server(status=302, headers={"location": "http://169.254.169.254/"}),  # no redirects
        Server(body=b"{" + b" " * 20_000 + b"}"),  # over 16 KB
        Server(doc=_doc(token_endpoint_auth_method="client_secret_basic")),
        Server(body=b"not json"),
    ],
)
async def test_bad_documents_rejected(server):
    w, stack = await _web(server)
    async with stack, w.browser() as b:
        resp = await authorize(b, CID, REDIRECT, pkce()[1])
    assert resp.status_code == 400
    assert w.conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0


async def test_http_client_id_is_not_cimd():
    srv = Server()
    w, stack = await _web(srv)
    async with stack, w.browser() as b:
        resp = await authorize(b, "http://app.example/client.json", REDIRECT, pkce()[1])
    assert resp.status_code == 400 and srv.fetches == 0


async def test_negative_result_cached(fake_clock):  # noqa: F811
    srv = Server(ips=("10.0.0.5",))
    w, stack = await _web(srv)
    async with stack, w.browser() as b:
        await authorize(b, CID, REDIRECT, pkce()[1])
        srv.ips = ["93.184.216.34"]
        assert (await authorize(b, CID, REDIRECT, pkce()[1])).status_code == 400  # cached failure
        fake_clock.advance(minutes=6)
        assert (await authorize(b, CID, REDIRECT, pkce()[1])).status_code == 302
    assert srv.fetches == 1  # the private-IP attempt never fetched


async def test_cimd_fetch_rate_limited_per_host(fake_clock):  # noqa: F811
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    w, stack = await make_web(cimd_transport=httpx.MockTransport(handler), cimd_resolver=resolve)
    async with stack:
        provider = w.state.provider
        for i in range(12):
            assert await provider.get_client(f"https://app.example/c{i}.json") is None
        assert len(seen) == 10  # cimd_host: 10 per minute
        fake_clock.advance(seconds=61)
        await provider.get_client("https://app.example/later.json")
        assert len(seen) == 11


async def test_cache_refreshes_after_a_day(fake_clock):  # noqa: F811
    srv = Server()
    w, stack = await _web(srv)
    async with stack:
        provider = w.state.provider
        assert (await provider.get_client(CID)).client_name == "Example App"
        srv.doc = _doc(client_name="Renamed")
        assert (await provider.get_client(CID)).client_name == "Example App"
        fake_clock.advance(hours=25)
        assert (await provider.get_client(CID)).client_name == "Renamed"
