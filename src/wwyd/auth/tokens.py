# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grants, authorization codes, access/refresh tokens and API keys (SQLite, hashed).

Every bearer request is looked up here, so revoking a grant or key, or disabling a user,
takes effect on the very next request.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from wwyd.auth import clock
from wwyd.auth.scopes import Scope, parse_scope
from wwyd.db import transaction
from wwyd.ids import new_id

log = logging.getLogger("wwyd.auth.tokens")

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=10)
API_KEY_RE = re.compile(r"^wwyd_([a-z0-9]{8})_([A-Za-z0-9_-]{20,})$")
_PREFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass(frozen=True)
class Principal:
    """Who a valid bearer credential speaks for."""

    subject_id: str  # grt_… or key_…
    user_id: str
    persona_id: str
    scope: Scope
    client_id: str
    client_name: str | None
    resource: str | None
    expires_at: int | None


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str


# --- grants -----------------------------------------------------------------------------


def _issue(conn: sqlite3.Connection, grant_id: str) -> tuple[str, str, str]:
    """Insert a new access + refresh token pair; returns (access, refresh, refresh_hash)."""
    access, refresh = clock.new_secret(), clock.new_secret()
    ts = clock.now_iso()
    conn.execute(
        "INSERT INTO tokens(token_hash, grant_id, kind, created_at, expires_at)"
        " VALUES (?, ?, 'access', ?, ?), (?, ?, 'refresh', ?, ?)",
        (
            clock.digest(access),
            grant_id,
            ts,
            clock.in_iso(ACCESS_TTL),
            clock.digest(refresh),
            grant_id,
            ts,
            clock.in_iso(REFRESH_TTL),
        ),
    )
    return access, refresh, clock.digest(refresh)


def revoke_grant(conn: sqlite3.Connection, grant_id: str, user_id: str | None = None) -> bool:
    ts = clock.now_iso()
    with transaction(conn):
        cur = conn.execute(
            "UPDATE grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"
            + (" AND user_id = ?" if user_id else ""),
            (ts, grant_id, user_id) if user_id else (ts, grant_id),
        )
        conn.execute(
            "UPDATE tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
            (ts, grant_id),
        )
    return cur.rowcount == 1


def list_grants(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, client_id, client_name, scope, created_at, last_used_at FROM grants"
        " WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


# --- authorization codes ----------------------------------------------------------------


def create_code(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    client_id: str,
    scope: Scope,
    code_challenge: str,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    resource: str | None,
) -> str:
    code = clock.new_secret()
    conn.execute(
        "INSERT INTO oauth_codes(code_hash, user_id, client_id, scope, code_challenge,"
        " redirect_uri, redirect_uri_provided_explicitly, resource, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clock.digest(code),
            user_id,
            client_id,
            str(scope),
            code_challenge,
            redirect_uri,
            int(redirect_uri_provided_explicitly),
            resource,
            clock.now_iso(),
            clock.in_iso(CODE_TTL),
        ),
    )
    return code


def load_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    """A code row that may still be exchanged. A reused code revokes the grant it produced."""
    row = conn.execute(
        "SELECT * FROM oauth_codes WHERE code_hash = ?", (clock.digest(code),)
    ).fetchone()
    if row is None:
        return None
    if row["used_at"] is not None:
        if row["grant_id"]:
            log.warning("authorization code reused; revoking grant %s", row["grant_id"])
            revoke_grant(conn, row["grant_id"])
        return None
    if clock.is_past(row["expires_at"]):
        return None
    return row


def exchange_code(
    conn: sqlite3.Connection, code: str, client_name: str | None
) -> IssuedTokens | None:
    """Mark the code used, create the grant and issue tokens; None if already used."""
    h = clock.digest(code)
    with transaction(conn):
        row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (h,)).fetchone()
        if row is None or row["used_at"] is not None or clock.is_past(row["expires_at"]):
            return None
        grant_id, ts = new_id("grt"), clock.now_iso()
        conn.execute(
            "INSERT INTO grants(id, user_id, client_id, client_name, scope, resource, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                grant_id,
                row["user_id"],
                row["client_id"],
                client_name,
                row["scope"],
                row["resource"],
                ts,
            ),
        )
        conn.execute(
            "UPDATE oauth_codes SET used_at = ?, grant_id = ? WHERE code_hash = ?",
            (ts, grant_id, h),
        )
        access, refresh, _ = _issue(conn, grant_id)
    return IssuedTokens(access, refresh, int(ACCESS_TTL.total_seconds()), row["scope"])


# --- tokens -----------------------------------------------------------------------------

_GRANT_JOIN = (
    " FROM tokens t JOIN grants g ON g.id = t.grant_id JOIN users u ON u.id = g.user_id"
    " JOIN personas p ON p.user_id = u.id"
)
_GRANT_COLS = (
    "SELECT t.token_hash, t.kind, t.expires_at, t.revoked_at, t.rotated_at, g.id AS grant_id,"
    " g.user_id, g.client_id, g.client_name, g.scope, g.resource, g.revoked_at AS grant_revoked,"
    " u.disabled_at, p.id AS persona_id"
)


def _principal(row: sqlite3.Row, subject: str, expires_at: int | None) -> Principal | None:
    scope = parse_scope(row["scope"])
    if scope is None:
        return None
    return Principal(
        subject,
        row["user_id"],
        row["persona_id"],
        scope,
        row["client_id"],
        row["client_name"],
        row["resource"],
        expires_at,
    )


def verify_access_token(conn: sqlite3.Connection, token: str) -> Principal | None:
    row = conn.execute(
        _GRANT_COLS + _GRANT_JOIN + " WHERE t.token_hash = ? AND t.kind = 'access'",
        (clock.digest(token),),
    ).fetchone()
    if (
        row is None
        or row["revoked_at"]
        or row["grant_revoked"]
        or row["disabled_at"]
        or clock.is_past(row["expires_at"])
    ):
        return None
    conn.execute(
        "UPDATE grants SET last_used_at = ? WHERE id = ?", (clock.now_iso(), row["grant_id"])
    )
    return _principal(row, row["grant_id"], clock.epoch(row["expires_at"]))


def load_refresh(conn: sqlite3.Connection, token: str) -> Principal | None:
    """A refresh token that may be exchanged. Presenting a rotated one revokes the grant."""
    row = conn.execute(
        _GRANT_COLS + _GRANT_JOIN + " WHERE t.token_hash = ? AND t.kind = 'refresh'",
        (clock.digest(token),),
    ).fetchone()
    if row is None:
        return None
    if row["rotated_at"] and not row["grant_revoked"]:
        log.warning("rotated refresh token reused; revoking grant %s", row["grant_id"])
        revoke_grant(conn, row["grant_id"])
        return None
    if (
        row["revoked_at"]
        or row["grant_revoked"]
        or row["disabled_at"]
        or clock.is_past(row["expires_at"])
    ):
        return None
    return _principal(row, row["grant_id"], clock.epoch(row["expires_at"]))


def rotate_refresh(conn: sqlite3.Connection, token: str) -> IssuedTokens | None:
    h = clock.digest(token)
    with transaction(conn):
        row = conn.execute(
            "SELECT t.grant_id, g.scope FROM tokens t JOIN grants g ON g.id = t.grant_id"
            " WHERE t.token_hash = ? AND t.kind = 'refresh' AND t.rotated_at IS NULL"
            " AND t.revoked_at IS NULL AND g.revoked_at IS NULL",
            (h,),
        ).fetchone()
        if row is None:
            return None
        access, refresh, new_hash = _issue(conn, row["grant_id"])
        ts = clock.now_iso()
        conn.execute(
            "UPDATE tokens SET rotated_at = ?, replaced_by = ? WHERE token_hash = ?",
            (ts, new_hash, h),
        )
        conn.execute("UPDATE grants SET last_used_at = ? WHERE id = ?", (ts, row["grant_id"]))
    return IssuedTokens(access, refresh, int(ACCESS_TTL.total_seconds()), row["scope"])


def grant_of_token(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute(
        "SELECT grant_id FROM tokens WHERE token_hash = ?", (clock.digest(token),)
    ).fetchone()
    return row["grant_id"] if row else None


# --- API keys ---------------------------------------------------------------------------


def create_api_key(
    conn: sqlite3.Connection, user_id: str, name: str, scope: Scope
) -> tuple[str, str]:
    """Returns (key_id, raw key). The raw key is shown once and never stored."""
    name = name.strip()[:80] or "API key"
    key_id = new_id("key")
    for _ in range(5):
        prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(8))
        secret = clock.new_secret()
        try:
            conn.execute(
                "INSERT INTO api_keys(id, user_id, name, prefix, key_hash, scope, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, user_id, name, prefix, clock.digest(secret), str(scope), clock.now_iso()),
            )
        except sqlite3.IntegrityError:
            continue
        return key_id, f"wwyd_{prefix}_{secret}"
    raise RuntimeError("could not allocate an API key prefix")


def list_api_keys(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, prefix, scope, created_at, last_used_at FROM api_keys"
        " WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


def revoke_api_key(conn: sqlite3.Connection, key_id: str, user_id: str) -> bool:
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (clock.now_iso(), key_id, user_id),
    )
    return cur.rowcount == 1


def verify_api_key(conn: sqlite3.Connection, key: str) -> Principal | None:
    match = API_KEY_RE.match(key)
    if not match:
        return None
    row = conn.execute(
        "SELECT k.id, k.user_id, k.name, k.key_hash, k.scope, k.revoked_at, u.disabled_at,"
        " p.id AS persona_id FROM api_keys k JOIN users u ON u.id = k.user_id"
        " JOIN personas p ON p.user_id = u.id WHERE k.prefix = ?",
        (match.group(1),),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["key_hash"], clock.digest(match.group(2))):
        return None
    if row["revoked_at"] or row["disabled_at"]:
        return None
    scope = parse_scope(row["scope"])
    if scope is None:
        return None
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (clock.now_iso(), row["id"]))
    return Principal(
        row["id"],
        row["user_id"],
        row["persona_id"],
        scope,
        f"apikey:{row['id']}",
        row["name"],
        None,
        None,
    )


def verify_bearer(conn: sqlite3.Connection, token: str) -> Principal | None:
    if token.startswith("wwyd_"):
        return verify_api_key(conn, token)
    return verify_access_token(conn, token)


def principal_by_subject(conn: sqlite3.Connection, subject_id: str) -> Principal | None:
    """Re-resolve a subject (grant or key id) for the caller seam; None if no longer valid."""
    if subject_id.startswith("key_"):
        row = conn.execute(
            "SELECT k.id, k.user_id, k.name, k.scope, k.revoked_at, u.disabled_at,"
            " p.id AS persona_id FROM api_keys k JOIN users u ON u.id = k.user_id"
            " JOIN personas p ON p.user_id = u.id WHERE k.id = ?",
            (subject_id,),
        ).fetchone()
        if row is None or row["revoked_at"] or row["disabled_at"]:
            return None
        scope = parse_scope(row["scope"])
        return (
            Principal(
                row["id"],
                row["user_id"],
                row["persona_id"],
                scope,
                f"apikey:{row['id']}",
                row["name"],
                None,
                None,
            )
            if scope
            else None
        )
    row = conn.execute(
        "SELECT g.id, g.user_id, g.client_id, g.client_name, g.scope, g.resource, g.revoked_at,"
        " u.disabled_at, p.id AS persona_id FROM grants g JOIN users u ON u.id = g.user_id"
        " JOIN personas p ON p.user_id = u.id WHERE g.id = ?",
        (subject_id,),
    ).fetchone()
    if row is None or row["revoked_at"] or row["disabled_at"]:
        return None
    scope = parse_scope(row["scope"])
    if scope is None:
        return None
    return Principal(
        row["id"],
        row["user_id"],
        row["persona_id"],
        scope,
        row["client_id"],
        row["client_name"],
        row["resource"],
        None,
    )


# --- bulk revocation and lookups --------------------------------------------------------


def is_known_bearer(conn: sqlite3.Connection, token: str) -> bool:
    """Whether ``token`` is (or was) a real credential: an access token we issued or an API
    key whose secret matches, regardless of expiry or revocation. Used to tell guessing apart
    from clients presenting a stale token."""
    if token.startswith("wwyd_"):
        match = API_KEY_RE.match(token)
        if not match:
            return False
        row = conn.execute(
            "SELECT key_hash FROM api_keys WHERE prefix = ?", (match.group(1),)
        ).fetchone()
        return row is not None and hmac.compare_digest(
            row["key_hash"], clock.digest(match.group(2))
        )
    return (
        conn.execute(
            "SELECT 1 FROM tokens WHERE token_hash = ? AND kind = 'access'", (clock.digest(token),)
        ).fetchone()
        is not None
    )


def _revoke_grants(conn: sqlite3.Connection, grant_ids: list[str], ts: str) -> None:
    for grant_id in grant_ids:
        conn.execute(
            "UPDATE grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (ts, grant_id)
        )
        conn.execute(
            "UPDATE tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
            (ts, grant_id),
        )


def revoke_all(conn: sqlite3.Connection, user_id: str) -> int:
    """Revoke every live grant and API key of the user; returns how many."""
    ts = clock.now_iso()
    with transaction(conn):
        grant_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM grants WHERE user_id = ? AND revoked_at IS NULL", (user_id,)
            )
        ]
        _revoke_grants(conn, grant_ids, ts)
        keys = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (ts, user_id),
        ).rowcount
    return len(grant_ids) + keys


def revoke_for_audience(conn: sqlite3.Connection, user_id: str, audience_name: str) -> int:
    """Revoke the user's live grants and API keys whose ``respond:`` scope names
    ``audience_name``. Scopes bind audience *names*, so call this when an audience is deleted
    or renamed: a later audience with the same name must not inherit old tokens. Returns
    how many credentials were revoked."""

    def names(scope: str) -> bool:
        parsed = parse_scope(scope)
        return parsed is not None and parsed.kind == "respond" and audience_name in parsed.audiences

    ts = clock.now_iso()
    with transaction(conn):
        grant_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id, scope FROM grants WHERE user_id = ? AND revoked_at IS NULL"
                " AND scope LIKE 'respond:%'",
                (user_id,),
            )
            if names(r["scope"])
        ]
        _revoke_grants(conn, grant_ids, ts)
        key_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id, scope FROM api_keys WHERE user_id = ? AND revoked_at IS NULL"
                " AND scope LIKE 'respond:%'",
                (user_id,),
            )
            if names(r["scope"])
        ]
        for key_id in key_ids:
            conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (ts, key_id))
    return len(grant_ids) + len(key_ids)
