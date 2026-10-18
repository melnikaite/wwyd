# SPDX-License-Identifier: AGPL-3.0-or-later
"""Users, personas, seeding and persona settings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from wwyd.db import transaction
from wwyd.errors import Invalid, NotFound
from wwyd.ids import new_id, now_iso

DEFAULT_AUDIENCE = ("everyone", "Anyone not in another group")

# (path, description). Parents come before children.
SEED_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("communication", "How the owner communicates with other people."),
    ("communication/style", "Tone, length, greetings, language and pace of the owner's messages."),
    (
        "communication/boundaries",
        "Topics, channels and requests the owner refuses or handles only in a certain way.",
    ),
    ("work", "The owner's working life."),
    ("work/meetings", "Which meetings the owner accepts, how long they run, buffers between them."),
    ("work/decisions", "How the owner makes and delegates work decisions."),
    ("schedule", "When the owner does things."),
    ("schedule/routine", "Daily and weekly rhythm: working hours, availability, regular slots."),
    ("food", "What and how the owner likes to eat and drink."),
    ("travel", "How the owner travels and plans trips."),
    ("money", "Spending, saving and how the owner handles money matters."),
    ("health", "Exercise, sleep and health-related habits the owner chooses to share."),
    ("family", "How the owner deals with family matters and family members."),
    ("hobbies", "Free-time interests and activities."),
    ("tech", "Tools, devices and software the owner prefers and how they use them."),
)

DEFAULT_DISCLOSURE = "This reply was written automatically by an AI assistant on behalf of {owner}."
RAW_TEXT_NOTE = (
    "Raw text sent to ingest_raw and predict situations go to the LLM backend configured for "
    "this persona. With a local backend nothing leaves the machine; with a remote provider "
    "that provider sees the text. Set llm_for_ingest to false to derive claims from code only."
)
DEFAULT_SETTINGS: dict[str, Any] = {
    "escalate_below": 0.6,
    "disclosure_template": DEFAULT_DISCLOSURE,
    "llm_for_ingest": True,
}


@dataclass(frozen=True)
class UserPersona:
    user_id: str
    login: str
    persona_id: str
    is_admin: bool


def _seed_persona_content(conn: sqlite3.Connection, persona_id: str) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO audiences(id, persona_id, name, description, is_default, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        (new_id("aud"), persona_id, *DEFAULT_AUDIENCE, ts, ts),
    )
    ids: dict[str, str] = {}
    for path, description in SEED_CATEGORIES:
        cat_id = new_id("cat")
        parent = path.rsplit("/", 1)[0] if "/" in path else None
        conn.execute(
            "INSERT INTO categories(id, persona_id, parent_id, name, description, path,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cat_id,
                persona_id,
                ids.get(parent) if parent else None,
                path.rsplit("/", 1)[-1],
                description,
                path,
                ts,
                ts,
            ),
        )
        ids[path] = cat_id


def _default_display_name(login: str) -> str:
    return login.split("@", 1)[0] or login


def get_user(conn: sqlite3.Connection, login: str) -> UserPersona | None:
    row = conn.execute(
        "SELECT u.id, u.login, u.is_admin, p.id AS persona_id FROM users u"
        " JOIN personas p ON p.user_id = u.id WHERE u.login = ?",
        (login,),
    ).fetchone()
    if row is None:
        return None
    return UserPersona(row["id"], row["login"], row["persona_id"], bool(row["is_admin"]))


def create_user(conn: sqlite3.Connection, login: str) -> UserPersona:
    """Create a user (no password) with a seeded persona; the first user becomes admin."""
    login = login.strip()
    if not login or len(login) > 254 or any(c.isspace() for c in login):
        raise Invalid("login must be a non-empty string without whitespace", field="login")
    with transaction(conn):
        is_admin = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        user_id, persona_id, ts = new_id("usr"), new_id("prs"), now_iso()
        conn.execute(
            "INSERT INTO users(id, login, password_hash, is_admin, created_at)"
            " VALUES (?, ?, NULL, ?, ?)",
            (user_id, login, int(is_admin), ts),
        )
        conn.execute(
            "INSERT INTO personas(id, user_id, display_name, timezone, settings, created_at)"
            " VALUES (?, ?, ?, 'UTC', ?, ?)",
            (persona_id, user_id, _default_display_name(login), json.dumps(DEFAULT_SETTINGS), ts),
        )
        _seed_persona_content(conn, persona_id)
    return UserPersona(user_id, login, persona_id, is_admin)


def get_or_create_user(conn: sqlite3.Connection, login: str) -> tuple[UserPersona, bool]:
    existing = get_user(conn, login.strip())
    if existing is not None:
        return existing, False
    return create_user(conn, login), True


def _persona_row(conn: sqlite3.Connection, persona_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT p.*, u.login FROM personas p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
        (persona_id,),
    ).fetchone()
    if row is None:
        raise NotFound("persona not found")
    return row


def wipe_persona_content(conn: sqlite3.Connection, persona_id: str) -> None:
    """Delete claims, categories, audiences and changes of a persona (inside a transaction)."""
    conn.execute("DELETE FROM claims WHERE persona_id = ?", (persona_id,))
    conn.execute("DELETE FROM changes WHERE persona_id = ?", (persona_id,))
    conn.execute("DELETE FROM audiences WHERE persona_id = ?", (persona_id,))
    # Children before parents (parent_id is ON DELETE RESTRICT).
    conn.execute("DELETE FROM categories WHERE persona_id = ? AND path LIKE '%/%/%'", (persona_id,))
    conn.execute("DELETE FROM categories WHERE persona_id = ? AND path LIKE '%/%'", (persona_id,))
    conn.execute("DELETE FROM categories WHERE persona_id = ?", (persona_id,))


def delete_persona(conn: sqlite3.Connection, persona_id: str, confirm: str) -> dict[str, Any]:
    """Wipe all persona content and settings; re-seed an empty persona with the same ID."""
    row = _persona_row(conn, persona_id)
    if not isinstance(confirm, str) or confirm.strip().casefold() != row["login"].casefold():
        raise Invalid("confirm must equal the account login", field="confirm")
    with transaction(conn):
        wipe_persona_content(conn, persona_id)
        conn.execute(
            "UPDATE personas SET display_name = ?, timezone = 'UTC', settings = ?,"
            " spend_capped_at = NULL WHERE id = ?",
            (_default_display_name(row["login"]), json.dumps(DEFAULT_SETTINGS), persona_id),
        )
        _seed_persona_content(conn, persona_id)
    return {"deleted": True}


# --- settings -----------------------------------------------------------------------------


def get_settings(conn: sqlite3.Connection, persona_id: str) -> dict[str, Any]:
    row = _persona_row(conn, persona_id)
    stored = {**DEFAULT_SETTINGS, **json.loads(row["settings"] or "{}")}
    return {
        "display_name": row["display_name"],
        "timezone": row["timezone"],
        "escalate_below": stored["escalate_below"],
        "disclosure_template": stored["disclosure_template"],
        "llm_for_ingest": stored["llm_for_ingest"],
        "raw_text_note": RAW_TEXT_NOTE,
    }


def check_timezone(value: str) -> str:
    if value == "UTC":
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise Invalid("timezone must be an IANA zone name", field="timezone") from None
    if value not in available_timezones():
        raise Invalid("timezone must be an IANA zone name", field="timezone")
    return value


def update_settings(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    display_name: str | None = None,
    timezone: str | None = None,
    escalate_below: float | None = None,
    disclosure_template: str | None = None,
    llm_for_ingest: bool | None = None,
) -> dict[str, Any]:
    row = _persona_row(conn, persona_id)
    settings = {**DEFAULT_SETTINGS, **json.loads(row["settings"] or "{}")}
    new_display = row["display_name"]
    new_tz = row["timezone"]
    if display_name is not None:
        display_name = display_name.strip()
        if not 1 <= len(display_name) <= 100:
            raise Invalid("display_name must be 1-100 characters", field="display_name")
        new_display = display_name
    if timezone is not None:
        new_tz = check_timezone(timezone)
    if escalate_below is not None:
        if not 0 <= escalate_below <= 1:
            raise Invalid("escalate_below must be between 0 and 1", field="escalate_below")
        settings["escalate_below"] = float(escalate_below)
    if disclosure_template is not None:
        if "{owner}" not in disclosure_template or len(disclosure_template) > 500:
            raise Invalid(
                "disclosure_template must contain {owner} and be at most 500 characters",
                field="disclosure_template",
            )
        settings["disclosure_template"] = disclosure_template
    if llm_for_ingest is not None:
        settings["llm_for_ingest"] = bool(llm_for_ingest)
    with transaction(conn):
        conn.execute(
            "UPDATE personas SET display_name = ?, timezone = ?, settings = ? WHERE id = ?",
            (new_display, new_tz, json.dumps(settings), persona_id),
        )
    return get_settings(conn, persona_id)
