# SPDX-License-Identifier: AGPL-3.0-or-later
"""Users, personas, seeding and persona settings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
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
    ("other", "Anything that fits no other category."),
)

DEFAULT_DISCLOSURE = "This reply was written automatically by an AI assistant on behalf of {owner}."
RAW_TEXT_NOTE = (
    "Raw text sent to ingest_raw and predict situations go to the LLM backend configured for "
    "this persona. With a local backend nothing leaves the machine; with a remote provider "
    "that provider sees the text. Set llm_for_ingest to false to derive claims from code only."
)
PRIVACY_NOTE = (
    "Text you send to predict or ingest is processed by the LLM backend configured for your "
    "persona; with a local backend nothing leaves this machine."
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
    from wwyd.store.audiences import audience_names, revoke_vanished

    with transaction(conn):
        before = audience_names(conn, persona_id)
        wipe_persona_content(conn, persona_id)
        # The persona row is kept (same ID), so ON DELETE CASCADE does not fire: remove the
        # per-persona operational rows explicitly.
        for table in ("audit", "jobs", "compaction_runs"):
            conn.execute(f"DELETE FROM {table} WHERE persona_id = ?", (persona_id,))
        conn.execute(
            "UPDATE personas SET display_name = ?, timezone = 'UTC', settings = ?,"
            " spend_capped_at = NULL WHERE id = ?",
            (_default_display_name(row["login"]), json.dumps(DEFAULT_SETTINGS), persona_id),
        )
        _seed_persona_content(conn, persona_id)
        revoke_vanished(conn, persona_id, before)
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
        "llm": _llm_public(stored.get("llm")),
        "raw_text_note": RAW_TEXT_NOTE,
        "privacy_note": PRIVACY_NOTE,
        **_limit_settings(conn, row["user_id"], persona_id),
    }


def _limit_settings(conn: sqlite3.Connection, user_id: str, persona_id: str) -> dict[str, Any]:
    from wwyd.limits import get_limiter

    limiter = get_limiter(conn)
    spend = limiter.spend_state(user_id, persona_id)
    return {
        "limits": {
            "effective": limiter.effective_limits(user_id),
            "ceilings": limiter.ceilings(user_id),
            "own": limiter.own_limits(user_id),
        },
        "usage_today": limiter.usage(user_id),
        "spend_capped": spend["spend_capped"],
        "spend_capped_at": spend["spend_capped_at"],
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
    limits: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update settings. ``limits`` sets the user's own (lower) limits; null removes one.
    ``llm`` replaces the LLM backend (see :func:`_llm_settings`)."""
    row = _persona_row(conn, persona_id)
    settings = {**DEFAULT_SETTINGS, **json.loads(row["settings"] or "{}")}
    if llm is not None:
        new_llm = _llm_settings(llm, settings.get("llm"))
        if new_llm is None:
            settings.pop("llm", None)
        else:
            settings["llm"] = new_llm
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
    if limits is not None:
        from wwyd.limits import get_limiter

        # Validated and stored before the other fields; a rejected value changes nothing.
        get_limiter(conn).set_own_limits(row["user_id"], limits)
    with transaction(conn):
        conn.execute(
            "UPDATE personas SET display_name = ?, timezone = ?, settings = ? WHERE id = ?",
            (new_display, new_tz, json.dumps(settings), persona_id),
        )
    return get_settings(conn, persona_id)


# --- LLM backend --------------------------------------------------------------------------

_LLM_FIELDS = frozenset(
    {"preset", "base_url", "model", "api_key", "price_in_per_m", "price_out_per_m"}
)


def _stored_backend(stored: dict[str, Any] | None) -> Any:
    from wwyd.llm.presets import Backend, instance_backend
    from wwyd.llm.secret import decrypt

    if not stored:
        return instance_backend()
    enc = stored.get("api_key_enc")
    return Backend(
        preset=stored["preset"],
        base_url=stored["base_url"],
        model=stored.get("model"),
        api_key=decrypt(enc) if enc else None,
        price_in_per_m=stored.get("price_in_per_m"),
        price_out_per_m=stored.get("price_out_per_m"),
    )


def llm_backend(conn: sqlite3.Connection, persona_id: str) -> Any:
    """The persona's resolved :class:`wwyd.llm.presets.Backend` (instance default if unset)."""
    row = _persona_row(conn, persona_id)
    return _stored_backend(json.loads(row["settings"] or "{}").get("llm"))


def _llm_public(stored: dict[str, Any] | None) -> dict[str, Any]:
    from wwyd.llm.presets import LOCAL_PRESETS
    from wwyd.llm.secret import mask

    backend = _stored_backend(stored)
    local = backend.preset in LOCAL_PRESETS or (
        backend.preset == "instance" and urlsplit(backend.base_url).hostname in _LOCAL_HOSTS
    )
    return {
        "preset": backend.preset,
        "base_url": backend.base_url,
        "model": backend.model,
        "api_key": None if backend.preset == "instance" else mask(backend.api_key),
        "price_in_per_m": backend.price_in_per_m,
        "price_out_per_m": backend.price_out_per_m,
        "local": local,
    }


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _same_destination(current: dict[str, Any], preset: str, base_url: str) -> bool:
    old, new = urlsplit(current.get("base_url") or ""), urlsplit(base_url)
    return (
        current.get("preset") == preset
        and old.scheme == new.scheme
        and (old.hostname or "").lower() == (new.hostname or "").lower()
        and old.port == new.port
    )


def _price(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 10_000:
        raise Invalid(f"{field} must be a number between 0 and 10000", field=f"llm.{field}")
    return float(value)


def _llm_settings(value: Any, current: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate an ``llm`` update and preflight it; returns the stored form (None = instance).

    ``{preset, base_url?, model?, api_key?, price_in_per_m?, price_out_per_m?}``: base_url
    and model default from the preset; an omitted api_key keeps the stored one, null clears it.
    """
    from wwyd.llm.client import check_base_url, preflight
    from wwyd.llm.presets import PRESET_NAMES, PRESETS, Backend
    from wwyd.llm.secret import decrypt, encrypt

    if not isinstance(value, dict) or set(value) - _LLM_FIELDS:
        raise Invalid(f"llm accepts only {sorted(_LLM_FIELDS)}", field="llm")
    preset = value.get("preset")
    if preset not in PRESET_NAMES:
        raise Invalid(f"llm.preset must be one of {list(PRESET_NAMES)}", field="llm.preset")
    if preset == "instance":
        return None
    default_url, default_model = PRESETS[preset]
    base_url = value.get("base_url") or default_url
    if not isinstance(base_url, str) or len(base_url) > 500:
        raise Invalid("llm.base_url must be an http(s) URL", field="llm.base_url")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Invalid("llm.base_url must be an http(s) URL", field="llm.base_url")
    model = value.get("model") or default_model
    if not isinstance(model, str) or not 1 <= len(model) <= 200:
        raise Invalid("llm.model is required for this preset", field="llm.model")
    if "api_key" in value:
        api_key = value["api_key"]
        if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 500):
            raise Invalid("llm.api_key must be a string", field="llm.api_key")
        api_key = api_key or None
    else:
        enc = (current or {}).get("api_key_enc")
        if enc and not _same_destination(current or {}, preset, base_url):
            # Never forward a stored key to a different host: the caller re-enters it (or
            # sends null to go without one).
            raise Invalid(
                "the backend host changed: send llm.api_key again (or null for none)",
                field="llm.api_key",
            )
        api_key = decrypt(enc) if enc else None
    if preset == "openrouter" and not api_key:
        raise Invalid("openrouter needs your own api_key", field="llm.api_key")
    backend = Backend(
        preset=preset,
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=api_key,
        price_in_per_m=_price(value.get("price_in_per_m"), "price_in_per_m"),
        price_out_per_m=_price(value.get("price_out_per_m"), "price_out_per_m"),
    )
    check_base_url(backend)
    preflight(backend)
    return {
        "preset": backend.preset,
        "base_url": backend.base_url,
        "model": backend.model,
        "api_key_enc": encrypt(api_key) if api_key else None,
        "price_in_per_m": backend.price_in_per_m,
        "price_out_per_m": backend.price_out_per_m,
    }
