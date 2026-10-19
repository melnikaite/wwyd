# SPDX-License-Identifier: AGPL-3.0-or-later
"""Our ``OAuthAuthorizationServerProvider`` for the MCP SDK's auth routes, the bearer token
verifier for ``/mcp`` and the HTTP caller seam.

Scope handling: the SDK checks a requested ``scope`` against the client's *registered*
``scope`` (``OAuthClientInformationFull.validate_scope``) and DCR registration against
``ClientRegistrationOptions.valid_scopes``. ``respond:<a>,<b>`` is parametrised and cannot
be enumerated, and the user picks the scope on the consent page anyway, so:

* ``valid_scopes`` is left unset; :meth:`WwydProvider.register_client` validates scopes;
* :class:`WwydClient` accepts any requested scope string; the first valid wwyd scope among
  them only *preselects* a choice on the consent page;
* the authorization code stores the scope the user chose, and ``load_authorization_code``
  returns it, so the issued token carries the consented scope even when it differs from the
  request. On refresh the SDK only allows narrowing to scopes the refresh token already has,
  which is always the consented one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl, ValidationError

from wwyd.auth import clock, tokens
from wwyd.auth.scopes import Scope, parse_scope
from wwyd.auth.throttle import Throttled, Throttles
from wwyd.config import Config
from wwyd.mcp.caller import Caller, CallerProvider
from wwyd.netguard import blocked_ip as _blocked_ip
from wwyd.netguard import default_resolver
from wwyd.netguard import pinned_url as _pinned_url

log = logging.getLogger("wwyd.auth.provider")

PENDING_TTL = timedelta(minutes=30)
DCR_UNUSED_TTL = timedelta(hours=24)
CIMD_CACHE_TTL = timedelta(hours=24)
CIMD_NEGATIVE_TTL = 300.0
CIMD_TIMEOUT = 5.0
CIMD_MAX_BYTES = 16 * 1024
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

Resolver = Callable[[str, int], Awaitable[list[str]]]


class WwydClient(OAuthClientInformationFull):
    """Client record with wwyd's scope and redirect-URI rules."""

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        # The user chooses the scope on the consent page; the request only preselects it.
        return requested_scope.split() if requested_scope else None

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            return super().validate_redirect_uri(None)
        registered = self.redirect_uris or []
        if redirect_uri in registered or str(redirect_uri) in {str(u) for u in registered}:
            return redirect_uri
        if _is_loopback(redirect_uri) and any(
            _is_loopback(u) and _same_ignoring_port(u, redirect_uri) for u in registered
        ):
            return redirect_uri  # RFC 8252 §7.3: any port on a registered loopback URI
        raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' not registered for client")


def _is_loopback(url: AnyUrl) -> bool:
    return url.scheme == "http" and (url.host or "") in LOOPBACK_HOSTS


def _same_ignoring_port(a: AnyUrl, b: AnyUrl) -> bool:
    return (a.scheme, a.host, a.path or "/", a.query) == (b.scheme, b.host, b.path or "/", b.query)


def client_display_name(client: OAuthClientInformationFull) -> str:
    if client.client_name:
        return client.client_name[:100]
    if client.client_id.startswith("https://"):
        return urlsplit(client.client_id).hostname or "Unnamed app"
    return "Unnamed app"


class WwydProvider:
    """OAuthAuthorizationServerProvider backed by SQLite."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        throttles: Throttles,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.throttles = throttles
        self.http_transport = http_transport
        self.resolver = resolver or default_resolver
        self._cimd_negative: dict[str, float] = {}

    @property
    def resource_url(self) -> str:
        return self.config.mcp_url

    # --- clients --------------------------------------------------------------------

    async def get_client(self, client_id: str) -> WwydClient | None:
        if client_id.startswith("https://"):
            return await self._get_cimd_client(client_id)
        row = self.conn.execute(
            "SELECT metadata FROM oauth_clients WHERE client_id = ? AND source = 'dcr'",
            (client_id,),
        ).fetchone()
        return WwydClient.model_validate_json(row["metadata"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.scope:
            for value in client_info.scope.split():
                if parse_scope(value) is None and value != "offline_access":
                    raise RegistrationError(
                        "invalid_client_metadata",
                        "scope must be owner, read or respond:<audience>[,<audience>…]",
                    )
        client = WwydClient.model_validate(client_info.model_dump())
        self.conn.execute(
            "INSERT INTO oauth_clients(client_id, source, client_name, metadata, created_at)"
            " VALUES (?, 'dcr', ?, ?, ?)",
            (
                client.client_id,
                client_display_name(client),
                client.model_dump_json(),
                clock.now_iso(),
            ),
        )
        log.info("registered OAuth client %s", client.client_id)

    async def _get_cimd_client(self, url: str) -> WwydClient | None:
        row = self.conn.execute(
            "SELECT metadata, fetched_at FROM oauth_clients WHERE client_id = ? AND source = 'cimd'",
            (url,),
        ).fetchone()
        if row and not clock.is_past(
            clock.format_ts(clock.parse_ts(row["fetched_at"]) + CIMD_CACHE_TTL)
        ):
            return WwydClient.model_validate_json(row["metadata"])
        negative = self._cimd_negative.get(url)
        if negative is not None and negative > clock.mono():
            return None
        client = await self._fetch_cimd(url)
        if client is None:
            self._cimd_negative[url] = clock.mono() + CIMD_NEGATIVE_TTL
            if len(self._cimd_negative) > 10_000:
                now = clock.mono()
                self._cimd_negative = {k: v for k, v in self._cimd_negative.items() if v > now}
            return None
        ts = clock.now_iso()
        self.conn.execute(
            "INSERT INTO oauth_clients(client_id, source, client_name, metadata, created_at, fetched_at)"
            " VALUES (?, 'cimd', ?, ?, ?, ?) ON CONFLICT(client_id) DO UPDATE SET"
            " client_name = excluded.client_name, metadata = excluded.metadata,"
            " fetched_at = excluded.fetched_at",
            (url, client_display_name(client), client.model_dump_json(), ts, ts),
        )
        return client

    async def _fetch_cimd(self, url: str) -> WwydClient | None:
        """Fetch a client ID metadata document: https only, public IPs only, no redirects,
        5 s timeout, 16 KB cap, ``client_id`` must equal the URL."""
        parts = urlsplit(url)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or not host
            or parts.username
            or parts.password
            or parts.fragment
            or parts.path in ("", "/")
        ):
            return None
        try:
            self.throttles.cimd_host.take(host)
            self.throttles.cimd_global.take("*")
        except Throttled:
            log.warning("CIMD fetch throttled")
            return None
        try:
            with anyio.fail_after(CIMD_TIMEOUT):
                addrs = await self.resolver(host, parts.port or 443)
        except (OSError, TimeoutError):
            log.info("CIMD host did not resolve")
            return None
        if not addrs or any(_blocked_ip(a) for a in addrs):
            log.warning("CIMD URL rejected: resolves to a non-public address")
            return None
        # Connect to the address we just validated, not to whatever a second DNS lookup
        # returns (DNS rebinding). ``Host`` and TLS SNI/certificate checks keep the hostname.
        pinned = _pinned_url(parts, addrs[0])
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.http_transport, timeout=CIMD_TIMEOUT, follow_redirects=False
                ) as http,
                http.stream(
                    "GET",
                    pinned,
                    headers={"Accept": "application/json", "Host": parts.netloc},
                    extensions={"sni_hostname": host},
                ) as resp,
            ):
                if resp.status_code != 200:
                    log.info("CIMD fetch returned HTTP %s", resp.status_code)
                    return None
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > CIMD_MAX_BYTES:
                        log.info("CIMD document too large")
                        return None
        except httpx.HTTPError:
            log.info("CIMD fetch failed")
            return None
        try:
            doc = json.loads(bytes(body))
        except ValueError:
            return None
        if not isinstance(doc, dict) or doc.get("client_id") != url:
            log.info("CIMD document rejected: client_id does not match its URL")
            return None
        if doc.get("token_endpoint_auth_method", "none") != "none" or "client_secret" in doc:
            log.info("CIMD document rejected: only public clients are supported")
            return None
        if not doc.get("redirect_uris"):
            return None
        try:
            return WwydClient.model_validate({**doc, "token_endpoint_auth_method": "none"})
        except ValidationError:
            return None

    # --- authorization --------------------------------------------------------------

    def canonical_resource(self, resource: str | None) -> str:
        """Tokens are bound to our MCP endpoint (RFC 8707); other resources are refused."""
        if resource is None:
            return self.resource_url
        if resource.rstrip("/") in (self.resource_url, self.config.public_url):
            return self.resource_url
        raise AuthorizeError("invalid_target", "unknown resource")

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        params.resource = self.canonical_resource(params.resource)
        pending_id = clock.new_secret()
        self.conn.execute(
            "INSERT INTO pending_authorizations(id, client_id, params, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                clock.digest(pending_id),
                client.client_id,
                params.model_dump_json(),
                clock.now_iso(),
                clock.in_iso(PENDING_TTL),
            ),
        )
        return f"{self.config.public_url}/consent?req={pending_id}"

    async def load_pending(self, pending_id: str) -> tuple[WwydClient, AuthorizationParams] | None:
        row = self.conn.execute(
            "SELECT client_id, params, expires_at FROM pending_authorizations WHERE id = ?",
            (clock.digest(pending_id),),
        ).fetchone()
        if row is None or clock.is_past(row["expires_at"]):
            return None
        client = await self.get_client(row["client_id"])
        if client is None:
            return None
        return client, AuthorizationParams.model_validate_json(row["params"])

    def _drop_pending(self, pending_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM pending_authorizations WHERE id = ?", (clock.digest(pending_id),)
        )
        return cur.rowcount == 1

    def approve(
        self,
        pending_id: str,
        client: WwydClient,
        params: AuthorizationParams,
        user_id: str,
        scope: Scope,
    ) -> str | None:
        if not self._drop_pending(pending_id):
            return None
        code = tokens.create_code(
            self.conn,
            user_id=user_id,
            client_id=client.client_id,
            scope=scope,
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        log.info("authorization approved for client %s", client.client_id)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, pending_id: str, params: AuthorizationParams) -> str | None:
        if not self._drop_pending(pending_id):
            return None
        return construct_redirect_uri(
            str(params.redirect_uri),
            error="access_denied",
            error_description="The user denied access",
            state=params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = tokens.load_code(self.conn, authorization_code)
        if row is None or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=[row["scope"]],
            expires_at=float(clock.epoch(row["expires_at"])),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(row["redirect_uri_provided_explicitly"]),
            resource=row["resource"],
            subject=row["user_id"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        issued = tokens.exchange_code(
            self.conn, authorization_code.code, client_display_name(client)
        )
        if issued is None:
            raise TokenError("invalid_grant", "authorization code is invalid")
        return OAuthToken(
            access_token=issued.access_token,
            expires_in=issued.expires_in,
            scope=issued.scope,
            refresh_token=issued.refresh_token,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        principal = tokens.load_refresh(self.conn, refresh_token)
        if principal is None or principal.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=principal.client_id,
            scopes=[str(principal.scope)],
            expires_at=principal.expires_at,
            resource=principal.resource,
            subject=principal.subject_id,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        issued = tokens.rotate_refresh(self.conn, refresh_token.token)
        if issued is None:
            raise TokenError("invalid_grant", "refresh token is invalid")
        return OAuthToken(
            access_token=issued.access_token,
            expires_in=issued.expires_in,
            scope=issued.scope,
            refresh_token=issued.refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        principal = tokens.verify_access_token(self.conn, token)
        return _access_token(principal, self.resource_url) if principal else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        grant_id = token.subject if token.subject and token.subject.startswith("grt_") else None
        grant_id = grant_id or tokens.grant_of_token(self.conn, token.token)
        if grant_id:
            tokens.revoke_grant(self.conn, grant_id)

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "not supported")

    # --- housekeeping ---------------------------------------------------------------

    def purge(self) -> int:
        """Drop unused DCR clients (no grant after 24 h) and expired pending state."""
        ts = clock.now_iso()
        cutoff = clock.format_ts(clock.now() - DCR_UNUSED_TTL)
        self.conn.execute("DELETE FROM pending_authorizations WHERE expires_at <= ?", (ts,))
        self.conn.execute(
            "DELETE FROM oauth_codes WHERE expires_at <= ? AND (used_at IS NOT NULL OR grant_id IS NULL)",
            (cutoff,),
        )
        cur = self.conn.execute(
            "DELETE FROM oauth_clients WHERE source = 'dcr' AND created_at <= ?"
            " AND client_id NOT IN (SELECT client_id FROM grants)",
            (cutoff,),
        )
        if cur.rowcount:
            log.info("purged %d unused OAuth clients", cur.rowcount)
        return cur.rowcount

    async def purge_forever(self, every: float = 3600.0) -> None:
        from wwyd.auth.accounts import purge_expired

        while True:
            try:
                self.purge()
                purge_expired(self.conn)
            except Exception:
                log.exception("purge failed")
            await asyncio.sleep(every)


def _access_token(principal: tokens.Principal, resource_url: str) -> AccessToken:
    return AccessToken(
        token="(not retained)",
        client_id=principal.client_id,
        scopes=[str(principal.scope)],
        expires_at=principal.expires_at,
        # API keys are bound to this server by construction.
        resource=principal.resource or resource_url,
        subject=principal.subject_id,
    )


class WwydTokenVerifier:
    """Accepts OAuth access tokens and API keys; looks both up in the DB every request."""

    def __init__(self, conn: sqlite3.Connection, resource_url: str) -> None:
        self.conn = conn
        self.resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = tokens.verify_bearer(self.conn, token)
        return _access_token(principal, self.resource_url) if principal else None


def http_caller_provider(conn: sqlite3.Connection) -> CallerProvider:
    """Caller seam for /mcp: the verified token's subject, re-checked against the DB."""

    def provide() -> Caller | None:
        access = get_access_token()
        if access is None or not access.subject:
            return None
        principal = tokens.principal_by_subject(conn, access.subject)
        if principal is None:
            return None
        return Caller(
            user_id=principal.user_id,
            persona_id=principal.persona_id,
            scope=principal.scope.kind,
            audiences=principal.scope.audiences if principal.scope.kind == "respond" else None,
            client_name=principal.client_name,
            subject_id=principal.subject_id,
        )

    return provide
