# SPDX-License-Identifier: AGPL-3.0-or-later
"""RFC 7009 token revocation, replacing the MCP SDK's /revoke.

The SDK's handler requires a ``client_secret`` field even from public clients
(``token_endpoint_auth_method=none``); RFC 7009 lets them send ``client_id`` only. This
handler authenticates the client per its registered method with the SDK's
``ClientAuthenticator`` (which accepts public clients without a secret), and also revokes
wwyd API keys: possessing a key proves the right to revoke it, so no client is needed.
Unknown tokens, and tokens of another client, get 200 with nothing revoked."""

from __future__ import annotations

from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from wwyd.auth import tokens
from wwyd.auth.provider import WwydProvider

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status, headers=_NO_STORE
    )


def revocation_route(provider: WwydProvider) -> Route:
    authenticator = ClientAuthenticator(provider)  # type: ignore[arg-type]

    async def revoke(request: Request) -> Response:
        form = await request.form()
        token = form.get("token")
        if not isinstance(token, str) or not token:
            return _error(400, "invalid_request", "token is required")
        if token.startswith("wwyd_"):
            tokens.revoke_api_key_by_value(provider.conn, token)
            return Response(status_code=200, headers=_NO_STORE)
        try:
            client = await authenticator.authenticate_request(request)
        except AuthenticationError as exc:
            return _error(401, "invalid_client", exc.message)
        loaders = [
            provider.load_access_token,
            lambda t: provider.load_refresh_token(client, t),  # type: ignore[arg-type]
        ]
        if form.get("token_type_hint") == "refresh_token":
            loaders.reverse()
        for load in loaders:
            found = await load(token)
            if found is not None:
                if found.client_id == client.client_id:
                    await provider.revoke_token(found)
                break
        return Response(status_code=200, headers=_NO_STORE)

    return Route("/revoke", revoke, methods=["POST"])
