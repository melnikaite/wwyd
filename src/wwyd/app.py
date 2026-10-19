# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HTTP application: web pages, the OAuth 2.1 authorization server and /mcp in one
FastAPI app. Our app owns the lifespan and runs the MCP session manager inside it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib import resources
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mcp.server.auth.routes import build_metadata, create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wwyd.auth import accounts
from wwyd.auth.middleware import AuthSurfaceMiddleware, BodyLimitMiddleware, mcp_body_cap
from wwyd.auth.provider import Resolver, WwydProvider, WwydTokenVerifier, http_caller_provider
from wwyd.auth.revoke import revocation_route
from wwyd.auth.throttle import Throttles
from wwyd.config import Config, ConfigError, db_path
from wwyd.db import open_db
from wwyd.limits import configure as configure_limits
from wwyd.mcp.server import WwydServer, build_server

log = logging.getLogger("wwyd.app")

SCOPES_SUPPORTED = ["owner", "read", "respond"]
SCOPES_NOTE = (
    "respond is parametrised as respond:<audience>[,<audience>...] (audience names, no "
    "spaces); the user picks the scope on the consent page."
)


@dataclass
class AppState:
    conn: sqlite3.Connection
    config: Config
    provider: WwydProvider
    throttles: Throttles
    mcp: WwydServer


def startup_notice(conn: sqlite3.Connection, config: Config) -> str | None:
    token = accounts.setup_link_if_needed(conn)
    if token:
        return f"Open this link to create the admin account: {config.public_url}/setup?t={token}"
    login = accounts.admin_without_password(conn)
    if login:
        return (
            f"No admin can sign in yet. Run `wwyd reset-password {login}` to get a link "
            "that sets a password."
        )
    return None


def transport_security(config: Config) -> TransportSecuritySettings:
    host = config.public_host
    hosts = [host, f"{host}:*"]
    origins = [config.public_url]
    if config.is_local:
        hosts += ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
        origins += ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def _as_metadata(auth: AuthSettings) -> dict[str, Any]:
    metadata = build_metadata(
        auth.issuer_url,
        None,
        auth.client_registration_options,
        RevocationOptions(enabled=True),  # type: ignore[arg-type]
    )
    data = metadata.model_dump(mode="json", exclude_none=True)
    data["token_endpoint_auth_methods_supported"] = [
        "none",
        "client_secret_post",
        "client_secret_basic",
    ]
    data["revocation_endpoint_auth_methods_supported"] = data[
        "token_endpoint_auth_methods_supported"
    ]
    data["scopes_supported"] = SCOPES_SUPPORTED
    data["code_challenge_methods_supported"] = ["S256"]
    data["client_id_metadata_document_supported"] = True
    return data


def create_app(
    config: Config,
    conn: sqlite3.Connection | None = None,
    *,
    cimd_transport: httpx.AsyncBaseTransport | None = None,
    cimd_resolver: Resolver | None = None,
    purge_interval: float = 3600.0,
    run_scheduler: bool = True,
) -> FastAPI:
    from wwyd.ingest.jobs import mark_interrupted
    from wwyd.web.pages import router as pages_router

    configure_limits(config.limits)
    config.llm.apply()
    if conn is None:
        conn = open_db(db_path())
    mark_interrupted(conn)
    throttles = Throttles()
    provider = WwydProvider(
        conn, config, throttles, http_transport=cimd_transport, resolver=cimd_resolver
    )
    try:
        auth = AuthSettings(
            issuer_url=config.public_url,  # type: ignore[arg-type] - str keeps the empty path
            resource_server_url=config.mcp_url,  # type: ignore[arg-type]
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )
    except ValueError as exc:
        raise ConfigError(f"invalid public_url: {exc}") from None
    server = build_server(
        conn,
        http_caller_provider(conn),
        token_verifier=WwydTokenVerifier(conn, config.mcp_url),
        auth=auth,
    )
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp", transport_security=transport_security(config)
    )
    state = AppState(conn, config, provider, throttles, server)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        notice = startup_notice(conn, config)
        if notice:
            print(notice, file=sys.stderr, flush=True)
        tasks = [asyncio.create_task(provider.purge_forever(purge_interval))]
        if run_scheduler:
            from wwyd.compact.scheduler import scheduler_task

            tasks.append(asyncio.create_task(scheduler_task(conn)))
        try:
            async with server.session_manager.run():
                yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.wwyd = state

    try:
        sdk_routes = create_auth_routes(
            provider,  # type: ignore[arg-type]
            issuer_url=auth.issuer_url,
            client_registration_options=auth.client_registration_options,
            revocation_options=auth.revocation_options,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    metadata = _as_metadata(auth)

    async def as_metadata(request: Request) -> JSONResponse:
        return JSONResponse(
            metadata,
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
        )

    async def healthz(request: Request) -> JSONResponse:
        # Liveness only: no DB access, not throttled (the auth throttles do not cover it).
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
    app.router.routes.append(
        Route("/.well-known/oauth-authorization-server", as_metadata, methods=["GET"])
    )
    # Our /revoke replaces the SDK's (public clients, API keys; see wwyd.auth.revoke).
    app.router.routes.append(revocation_route(provider))
    app.router.routes.extend(
        r
        for r in sdk_routes
        if not (
            isinstance(r, Route)
            and r.path in ("/.well-known/oauth-authorization-server", "/revoke")
        )
    )
    app.include_router(pages_router)
    static_dir = resources.files("wwyd.web") / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.mount("/", mcp_app)  # /mcp and /.well-known/oauth-protected-resource/mcp
    app.add_middleware(
        AuthSurfaceMiddleware,
        throttles=throttles,
        trusted_proxies=config.trusted_proxies,
        conn=conn,
    )
    # Outermost, so no layer (including the /token body read above) sees an oversized body.
    app.add_middleware(
        BodyLimitMiddleware, mcp_cap=lambda: mcp_body_cap(conn, config.limits.batch_bytes)
    )
    return app
