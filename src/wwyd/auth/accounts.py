# SPDX-License-Identifier: AGPL-3.0-or-later
"""Users with passwords, web sessions and one-time links (setup, invite, reset)."""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from wwyd.auth import clock
from wwyd.db import transaction
from wwyd.store.personas import create_user

MIN_PASSWORD = 10
MAX_PASSWORD = 1024
SESSION_TTL = timedelta(days=14)
LINK_TTL = {"setup": timedelta(hours=24), "invite": timedelta(days=7), "reset": timedelta(hours=24)}
LOGIN_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")

LinkKind = Literal["setup", "invite", "reset"]

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class AccountError(Exception):
    """A user-facing validation error (message is safe to show)."""


@dataclass(frozen=True)
class User:
    id: str
    login: str
    is_admin: bool
    disabled_at: str | None
    created_at: str
    has_password: bool
    password_reset_by_admin_at: str | None
    persona_id: str


_USER_SQL = (
    "SELECT u.id, u.login, u.is_admin, u.disabled_at, u.created_at, u.password_hash,"
    " u.password_reset_by_admin_at, p.id AS persona_id"
    " FROM users u JOIN personas p ON p.user_id = u.id"
)


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(
        row["id"],
        row["login"],
        bool(row["is_admin"]),
        row["disabled_at"],
        row["created_at"],
        row["password_hash"] is not None,
        row["password_reset_by_admin_at"],
        row["persona_id"],
    )


def user_by_id(conn: sqlite3.Connection, user_id: str) -> User | None:
    return _user(conn.execute(_USER_SQL + " WHERE u.id = ?", (user_id,)).fetchone())


def user_by_login(conn: sqlite3.Connection, login: str) -> User | None:
    return _user(conn.execute(_USER_SQL + " WHERE u.login = ?", (login.strip(),)).fetchone())


def list_users(conn: sqlite3.Connection) -> list[User]:
    return [_user(r) for r in conn.execute(_USER_SQL + " ORDER BY u.created_at, u.login")]  # type: ignore[misc]


def user_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def admin_without_password(conn: sqlite3.Connection) -> str | None:
    """Login of an admin, if users exist but no admin can sign in."""
    if conn.execute(
        "SELECT 1 FROM users WHERE is_admin = 1 AND password_hash IS NOT NULL"
        " AND disabled_at IS NULL"
    ).fetchone():
        return None
    row = conn.execute("SELECT login FROM users WHERE is_admin = 1 ORDER BY created_at").fetchone()
    return row["login"] if row else None


# --- passwords --------------------------------------------------------------------------


def check_new_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Choose a password of at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AccountError("That password is too long.")
    return password


def check_login(login: str) -> str:
    login = login.strip()
    if not LOGIN_RE.match(login) or len(login) > 254:
        raise AccountError("Enter a valid email address as your login.")
    return login


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-ish time: unknown users are checked against a dummy hash."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_hash_for(conn: sqlite3.Connection, login: str) -> tuple[str | None, str | None]:
    """(user_id, hash) for an enabled user, or (None, None)."""
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE login = ? AND disabled_at IS NULL",
        (login.strip(),),
    ).fetchone()
    return (row["id"], row["password_hash"]) if row else (None, None)


def set_password(
    conn: sqlite3.Connection, user_id: str, pw_hash: str, *, by_admin: bool = False
) -> None:
    ts = clock.now_iso()
    conn.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = ?,"
        " password_reset_by_admin_at = CASE WHEN ? THEN ? ELSE password_reset_by_admin_at END"
        " WHERE id = ?",
        (pw_hash, ts, int(by_admin), ts, user_id),
    )
    invalidate_reset_links(conn, user_id)


def create_account(conn: sqlite3.Connection, login: str, pw_hash: str) -> User:
    login = check_login(login)
    with transaction(conn):
        if user_by_login(conn, login) is not None:
            raise AccountError("That login is already taken.")
        created = create_user(conn, login)
        set_password(conn, created.user_id, pw_hash)
    user = user_by_id(conn, created.user_id)
    assert user is not None
    return user


def set_disabled(conn: sqlite3.Connection, user_id: str, disabled: bool) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE users SET disabled_at = ? WHERE id = ?",
            (clock.now_iso() if disabled else None, user_id),
        )
        if disabled:
            end_sessions(conn, user_id)


# --- web sessions -----------------------------------------------------------------------


@dataclass(frozen=True)
class WebSession:
    token_hash: str
    user: User
    csrf_token: str


def create_session(conn: sqlite3.Connection, user_id: str) -> tuple[str, WebSession]:
    token, csrf = clock.new_secret(), clock.new_secret()
    ts = clock.now_iso()
    conn.execute(
        "INSERT INTO web_sessions(token_hash, user_id, csrf_token, created_at, last_seen_at,"
        " expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (clock.digest(token), user_id, csrf, ts, ts, clock.in_iso(SESSION_TTL)),
    )
    user = user_by_id(conn, user_id)
    assert user is not None
    return token, WebSession(clock.digest(token), user, csrf)


def get_session(conn: sqlite3.Connection, token: str | None) -> WebSession | None:
    """Look up a session cookie; slides its expiry. Disabled users have no session."""
    if not token:
        return None
    h = clock.digest(token)
    row = conn.execute(
        "SELECT user_id, csrf_token, expires_at FROM web_sessions WHERE token_hash = ?", (h,)
    ).fetchone()
    if row is None:
        return None
    if clock.is_past(row["expires_at"]):
        conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (h,))
        return None
    user = user_by_id(conn, row["user_id"])
    if user is None or user.disabled_at is not None:
        return None
    conn.execute(
        "UPDATE web_sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
        (clock.now_iso(), clock.in_iso(SESSION_TTL), h),
    )
    return WebSession(h, user, row["csrf_token"])


def end_session(conn: sqlite3.Connection, token_hash: str) -> None:
    conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))


def end_sessions(conn: sqlite3.Connection, user_id: str, *, except_hash: str | None = None) -> int:
    cur = conn.execute(
        "DELETE FROM web_sessions WHERE user_id = ? AND token_hash IS NOT ?",
        (user_id, except_hash),
    )
    return cur.rowcount


# --- one-time links ---------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    token_hash: str
    kind: LinkKind
    user_id: str | None
    created_by: str | None
    expires_at: str


def create_link(
    conn: sqlite3.Connection,
    kind: LinkKind,
    *,
    user_id: str | None = None,
    created_by: str | None = None,
) -> str:
    """Create a link; returns the raw token (shown once). A new reset link replaces the
    user's other outstanding reset links."""
    token = clock.new_secret()
    if kind == "reset" and user_id is not None:
        invalidate_reset_links(conn, user_id)
    conn.execute(
        "INSERT INTO one_time_links(token_hash, kind, user_id, created_by, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            clock.digest(token),
            kind,
            user_id,
            created_by,
            clock.now_iso(),
            clock.in_iso(LINK_TTL[kind]),
        ),
    )
    return token


def invalidate_reset_links(conn: sqlite3.Connection, user_id: str) -> int:
    """Drop the user's unused reset links (after a password change or a newer link)."""
    return conn.execute(
        "DELETE FROM one_time_links WHERE kind = 'reset' AND user_id = ? AND used_at IS NULL",
        (user_id,),
    ).rowcount


def peek_link(conn: sqlite3.Connection, kind: LinkKind, token: str | None) -> Link | None:
    """A usable (unused, unexpired) link of ``kind``, or None."""
    if not token:
        return None
    row = conn.execute(
        "SELECT token_hash, kind, user_id, created_by, expires_at, used_at FROM one_time_links"
        " WHERE token_hash = ? AND kind = ?",
        (clock.digest(token), kind),
    ).fetchone()
    if row is None or row["used_at"] is not None or clock.is_past(row["expires_at"]):
        return None
    return Link(
        row["token_hash"], row["kind"], row["user_id"], row["created_by"], row["expires_at"]
    )


def consume_link(conn: sqlite3.Connection, link: Link) -> bool:
    cur = conn.execute(
        "UPDATE one_time_links SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
        (clock.now_iso(), link.token_hash),
    )
    return cur.rowcount == 1


def setup_link_if_needed(conn: sqlite3.Connection) -> str | None:
    """While there are no users: invalidate old setup links and return a fresh token."""
    with transaction(conn):
        if user_count(conn) > 0:
            return None
        conn.execute("DELETE FROM one_time_links WHERE kind = 'setup'")
        return create_link(conn, "setup")


def purge_expired(conn: sqlite3.Connection) -> None:
    ts = clock.now_iso()
    conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (ts,))
    conn.execute("DELETE FROM one_time_links WHERE expires_at <= ? OR used_at IS NOT NULL", (ts,))
