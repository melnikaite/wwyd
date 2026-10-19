# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client IP resolution and throttling of the OAuth endpoints and bearer auth on /mcp.

Sits in front of everything (pure ASGI). It never reads or logs submitted secrets: /token
bodies are parsed only for ``client_id``.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wwyd.auth.throttle import Throttle, Throttled, Throttles, client_ip

IP_KEY = "wwyd.client_ip"
_MAX_TOKEN_BODY = 64 * 1024


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
    def __init__(self, app: ASGIApp, throttles: Throttles, trusted_proxies: list[str]) -> None:
        self.app = app
        self.t = throttles
        self.trusted = trusted_proxies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        peer = scope.get("client")[0] if scope.get("client") else None
        ip = client_ip(peer, headers.get("x-forwarded-for"), self.trusted)
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
            elif path == "/token" and method == "POST":
                await self._token(scope, receive, send, ip)
                return
            elif path == "/mcp" or path.startswith("/mcp/"):
                self.t.bearer_ip.check(ip)
                status = await self._run(scope, receive, send)
                if status == 401 and "authorization" in headers:
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
        keyed: list[tuple[Throttle, str]] = [(self.t.token_ip, ip)]
        if client_id:
            keyed.append((self.t.token_client, client_id))
        for throttle, key in keyed:
            throttle.check(key)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status = await self._run(scope, replay, send)
        if status >= 400:
            for throttle, key in keyed:
                throttle.hit(key)
