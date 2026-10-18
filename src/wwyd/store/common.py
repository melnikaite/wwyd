# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared validation and helpers for the store layer."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from typing import Any

from wwyd.errors import Invalid
from wwyd.ids import new_id, now_iso

AUDIENCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
CATEGORY_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_CATEGORY_DEPTH = 3

KINDS = ("trait", "preference", "habit", "decision_rule", "style", "boundary")
STATUSES = ("pending", "approved", "rejected", "archived")
VISIBILITIES = ("owner_only", "shared")
ORIGINS = ("derived", "added", "imported", "owner")
ARCHIVE_REASONS = ("merged", "superseded", "decayed", "budget", "owner")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TEXT = 500
MAX_DESCRIPTION = 1000


def check_audience_name(name: str) -> str:
    if not isinstance(name, str) or not AUDIENCE_NAME.match(name):
        raise Invalid("audience name must match [a-z0-9][a-z0-9_-]{0,39}", field="name")
    return name


def check_category_path(path: str) -> str:
    if not isinstance(path, str):
        raise Invalid("category path must be a string", field="path")
    segments = path.split("/")
    if len(segments) > MAX_CATEGORY_DEPTH:
        raise Invalid(f"category path deeper than {MAX_CATEGORY_DEPTH} levels", field="path")
    if not all(CATEGORY_SEGMENT.match(seg) for seg in segments):
        raise Invalid(
            "category path segments must match [a-z0-9][a-z0-9_-]{0,39}, separated by '/'",
            field="path",
        )
    return path


def parent_path(path: str) -> str | None:
    return path.rsplit("/", 1)[0] if "/" in path else None


def check_description(description: str | None) -> str | None:
    if description is not None and len(description) > MAX_DESCRIPTION:
        raise Invalid(f"description longer than {MAX_DESCRIPTION} characters", field="description")
    return description


def check_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise Invalid(f"limit must be between 1 and {MAX_LIMIT}", field="limit")
    return limit


def encode_cursor(seq: int) -> str:
    raw = json.dumps({"after": seq}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded))["after"]
        if not isinstance(value, int):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise Invalid("malformed cursor", field="cursor") from None


_WS = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[\s.!?…;:,]+$")


def normalize_text(text: str) -> str:
    """Normalisation used for absorb/negative-memory matching."""
    return _TRAILING_PUNCT.sub("", _WS.sub(" ", text.casefold()).strip())


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def record_change(
    conn: sqlite3.Connection,
    persona_id: str,
    op: str,
    payload: dict[str, Any],
    *,
    state: str = "applied",
    reason: str | None = None,
) -> str:
    change_id = new_id("chg")
    conn.execute(
        "INSERT INTO changes(id, persona_id, ts, op, state, reason, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (change_id, persona_id, now_iso(), op, state, reason, dumps(payload)),
    )
    return change_id
