# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one clock all auth code reads (tests replace :data:`now`); secret helpers."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from wwyd.ids import format_ts, parse_ts


def _real_now() -> datetime:
    return datetime.now(UTC)


now = _real_now  # monkeypatch ``wwyd.auth.clock.now`` in tests


def now_iso() -> str:
    return format_ts(now())


def in_iso(delta: timedelta) -> str:
    return format_ts(now() + delta)


def is_past(ts: str | None) -> bool:
    return ts is not None and parse_ts(ts) <= now()


def epoch(ts: str) -> int:
    return int(parse_ts(ts).timestamp())


def mono() -> float:
    """Seconds on the auth clock (used by throttles; follows the patched clock)."""
    return now().timestamp()


def new_secret() -> str:
    """256-bit random, URL-safe."""
    return secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
