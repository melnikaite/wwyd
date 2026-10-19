# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client IP resolution and throttling of the OAuth endpoints and bearer auth on /mcp.

Sits in front of everything (pure ASGI). It never reads or logs submitted secrets: /token
bodies are parsed only for ``client_id``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wwyd.auth.throttle import Throttled, Throttles, client_ip, ip_key, is_internal

log = logging.getLogger("wwyd.auth.middleware")

IP_KEY = "wwyd.client_ip"  # the throttle key of the client IP (IPv6: its /64)
_MAX_TOKEN_BODY = 64 * 1024
FORM_BODY_CAP = 64 * 1024
MCP_BODY_OVERHEAD = 1024 * 1024


async def send_429(send: Send, retry_after: int, *, oauth: bool = True) -> None:
    if oauth:
        body = json.dumps(
            {"error": "rate_limited", "error_description": "Too many requests; try again later."}
        ).encode()
    else:
        body = b'{"error": "rate_limited"}'
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(retry_after).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuthSurfaceMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        throttles: Throttles,
        trusted_proxies: list[str],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.app = app
        self.t = throttles
        self.trusted = trusted_proxies
        self.conn = conn
        self._warned_proxy = False

    def _warn_untrusted_proxy(self, peer: str | None, forwarded_for: str | None) -> None:
        if self._warned_proxy or self.trusted or not forwarded_for or not is_internal(peer):
            return
        self._warned_proxy = True
        log.warning(
            "requests from %s carry X-Forwarded-For but trusted_proxies is empty: all clients "
            "behind this proxy share one IP, so per-IP auth throttles act globally. Set "
            "trusted_proxies (e.g. WWYD_TRUSTED_PROXIES=%s) to the proxy's address.",
            peer,
            peer,
        )

    def _known_bearer(self, authorization: str) -> bool:
        if self.conn is None:
            return False
        from wwyd.auth.tokens import is_known_bearer

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return False
        return is_known_bearer(self.conn, token.strip())

    def _client_exists(self, client_id: str) -> bool:
        if self.conn is None:
            return True
        return (
            self.conn.execute(
                "SELECT 1 FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
            is not None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        peer = scope.get("client")[0] if scope.get("client") else None
        self._warn_untrusted_proxy(peer, headers.get("x-forwarded-for"))
        ip = ip_key(client_ip(peer, headers.get("x-forwarded-for"), self.trusted))
        scope[IP_KEY] = ip
        path, method = scope["path"], scope["method"]
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        try:
            if path == "/authorize":
                self.t.authorize_ip.take(ip)
            elif path == "/register" and method == "POST":
                self.t.register_ip.take(ip)
                self.t.register_global.check("*")
                status = await self._run(scope, receive, send)
                if status == 201:
                    self.t.register_global.hit("*")
                return
            elif path in ("/token", "/revoke") and method == "POST":
                await self._token(scope, receive, send, ip)
                return
            elif path == "/mcp" or path.startswith("/mcp/"):
                self.t.bearer_ip.check(ip)
                status = await self._run(scope, receive, send)
                # Only guesses count: a known but expired or revoked token is a stale
                # client, not an attack, and must not lock out its neighbours.
                if (
                    status == 401
                    and "authorization" in headers
                    and not self._known_bearer(headers["authorization"])
                ):
                    self.t.bearer_ip.hit(ip)
                return
        except Throttled as exc:
            await send_429(send, exc.retry_after, oauth=not path.startswith("/mcp"))
            return
        await self.app(scope, receive, send)

    async def _run(self, scope: Scope, receive: Receive, send: Send) -> int:
        status = 0

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, capture)
        return status

    async def _token(self, scope: Scope, receive: Receive, send: Send, ip: str) -> None:
        chunks: list[bytes] = []
        size, more = 0, True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > _MAX_TOKEN_BODY:
                body = b'{"error": "invalid_request"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)
        try:
            client_id = parse_qs(body.decode("utf-8", "replace")).get("client_id", [""])[0][:300]
        except ValueError:
            client_id = ""
        # token_client is keyed on (client_id, ip): the client_id is unauthenticated and
        # public CIMD ids are shared by every user, so a client_id-only key would let anyone
        # lock all of them out.
        client_key = f"{client_id}|{ip}" if client_id else ""
        self.t.token_ip.check(ip)
        if client_key:
            self.t.token_client.check(client_key)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status = await self._run(scope, replay, send)
        if status >= 400:
            self.t.token_ip.hit(ip)
            # Only failures against a registered client count per client.
            if client_key and self._client_exists(client_id):
                self.t.token_client.hit(client_key)


class _TooLarge(Exception):
    pass


async def _send_413(send: Send) -> None:
    body = b'{"error": "request_too_large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def mcp_body_cap(conn: sqlite3.Connection, default_batch_bytes: int | None) -> int | None:
    """The /mcp body cap: the largest ingest batch any user may send (instance default or
    the highest admin ceiling) plus JSON-RPC overhead. None when batches are unlimited."""
    caps: list[int] = []
    if default_batch_bytes is None:
        return None
    caps.append(int(default_batch_bytes))
    row = conn.execute(
        "SELECT MAX(CAST(json_extract(limits, '$.batch_bytes') AS INTEGER)) FROM limit_overrides"
        " WHERE set_by = 'admin'"
    ).fetchone()
    if row and row[0] is not None:
        caps.append(int(row[0]))
    return max(caps) + MCP_BODY_OVERHEAD


class BodyLimitMiddleware:
    """Rejects request bodies above a cap with 413: by Content-Length up front, and by
    counting streamed (chunked) bodies as they are read. Web forms and OAuth endpoints get
    :data:`FORM_BODY_CAP`; /mcp gets ``mcp_cap()`` (re-evaluated at most every 30 s)."""

    def __init__(self, app: ASGIApp, mcp_cap: Callable[[], int | None]) -> None:
        self.app = app
        self._mcp_cap = mcp_cap
        self._cached: tuple[float, int | None] | None = None

    def mcp_cap(self) -> int | None:
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > 30:
            self._cached = (now, self._mcp_cap())
        return self._cached[1]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        cap = self.mcp_cap() if path == "/mcp" or path.startswith("/mcp/") else FORM_BODY_CAP
        if cap is None:
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > cap:
                    await _send_413(send)
                    return
        seen = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal seen, exceeded
            if exceeded:
                raise _TooLarge
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    exceeded = True
                    raise _TooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                return  # whatever the app answers, the client gets the 413 below
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            pass
        except BaseException:
            if not exceeded:
                raise
        if exceeded and not started:
            await _send_413(send)
