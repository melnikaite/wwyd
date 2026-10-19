# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kind-prefixed random IDs and UTC timestamps."""

from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime

PREFIXES = frozenset({"usr", "prs", "aud", "cat", "clm", "chg", "grt", "key", "job", "adt", "anl"})


def new_id(prefix: str) -> str:
    """Return ``<prefix>_`` followed by 16 lowercase base32 characters (80 random bits)."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    return f"{prefix}_{base64.b32encode(secrets.token_bytes(10)).decode().lower()}"


def format_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return format_ts(datetime.now(UTC))


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
