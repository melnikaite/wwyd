# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rate limits, daily quotas, spend cap, job slots and usage counters (SPEC §11).

Process-local state (token buckets, job slots) lives on the :class:`Limiter`; daily and
monthly counters live in ``usage_daily``. Day and month boundaries are UTC. Only metric
names and numbers are stored, never text.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

from wwyd.db import transaction
from wwyd.errors import Invalid, NotFound, RateLimited, SpendCap, TooLarge
from wwyd.ids import format_ts, parse_ts
from wwyd.limits.defaults import DEFAULTS, FLOAT_LIMIT_KEYS, USER_LIMIT_KEYS, LimitDefaults

Clock = Callable[[], float]  # epoch seconds
Limits = dict[str, int | float | None]


def estimate_cost(
    tokens_in: int, tokens_out: int, price_in_per_m: float | None, price_out_per_m: float | None
) -> float | None:
    """Cost in USD from per-1M-token prices; None when the backend has no prices."""
    if price_in_per_m is None and price_out_per_m is None:
        return None
    return (tokens_in * (price_in_per_m or 0.0) + tokens_out * (price_out_per_m or 0.0)) / 1e6


def _min(*values: int | float | None) -> int | float | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


class _Bucket:
    __slots__ = ("tokens", "ts")

    def __init__(self, tokens: float, ts: float) -> None:
        self.tokens, self.ts = tokens, ts


class Limiter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        defaults: LimitDefaults = DEFAULTS,
        clock: Clock = time.time,
    ) -> None:
        self.conn = conn
        self.defaults = defaults
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._jobs: dict[str, int] = {}

    # --- time helpers -------------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _secs_to_midnight(self) -> int:
        now = self._now()
        nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        return math.ceil((nxt - now).total_seconds())

    def _secs_to_month_end(self) -> int:
        now = self._now()
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return math.ceil((datetime(y, m, 1, tzinfo=UTC) - now).total_seconds())

    # --- limit configuration ------------------------------------------------------------

    def _override(self, user_id: str, set_by: str) -> Limits:
        row = self.conn.execute(
            "SELECT limits FROM limit_overrides WHERE user_id = ? AND set_by = ?",
            (user_id, set_by),
        ).fetchone()
        return json.loads(row["limits"]) if row else {}

    def ceilings(self, user_id: str) -> Limits:
        """Admin ceilings for the user (only keys the admin set)."""
        return self._override(user_id, "admin")

    def own_limits(self, user_id: str) -> Limits:
        """The user's own (lower) limits (only keys the user set)."""
        return self._override(user_id, "user")

    def _ceiling_values(self, user_id: str) -> Limits:
        """Per key, the admin ceiling when set (it replaces the default and may exceed it),
        otherwise the default: the most a user may set for themselves."""
        defaults, admin = self.defaults.user_limits(), self.ceilings(user_id)
        return {k: admin[k] if k in admin else defaults[k] for k in USER_LIMIT_KEYS}

    def effective_limits(self, user_id: str) -> Limits:
        ceil, own = self._ceiling_values(user_id), self.own_limits(user_id)
        return {k: _min(ceil[k], own.get(k)) for k in USER_LIMIT_KEYS}

    def _validate(self, values: Any) -> Limits:
        if not isinstance(values, dict):
            raise Invalid("limits must be an object", field="limits")
        out: Limits = {}
        for key, value in values.items():
            if key not in USER_LIMIT_KEYS:
                raise Invalid(f"unknown limit {key!r}", field="limits")
            if value is None:
                out[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise Invalid(f"limit {key} must be a number or null", field="limits")
            if not math.isfinite(value) or value < 0:
                raise Invalid(f"limit {key} must be >= 0", field="limits")
            if key not in FLOAT_LIMIT_KEYS:
                if value != int(value):
                    raise Invalid(f"limit {key} must be an integer", field="limits")
                value = int(value)
            out[key] = value
        return out

    def _store(self, user_id: str, set_by: str, changes: Limits) -> Limits:
        if self.conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise NotFound("user not found")
        merged = {**self._override(user_id, set_by), **changes}
        merged = {k: v for k, v in merged.items() if v is not None}
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO limit_overrides(user_id, set_by, limits, updated_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(user_id, set_by)"
                " DO UPDATE SET limits = excluded.limits, updated_at = excluded.updated_at",
                (user_id, set_by, json.dumps(merged), format_ts(self._now())),
            )
        return merged

    def set_admin_ceilings(self, user_id: str, values: dict[str, Any]) -> Limits:
        """Merge admin ceilings (any value >= 0; null removes the key). Returns all ceilings."""
        return self._store(user_id, "admin", self._validate(values))

    def set_own_limits(self, user_id: str, values: dict[str, Any]) -> Limits:
        """Merge the user's own limits; values above the ceiling (admin ceiling if set, else
        the default) are rejected.

        null removes the user's own value. Returns all of the user's own limits.
        """
        changes = self._validate(values)
        ceil = self._ceiling_values(user_id)
        for key, value in changes.items():
            if value is not None and ceil[key] is not None and value > ceil[key]:
                raise Invalid(
                    f"limit {key} may not exceed {ceil[key]}", field="limits", maximum=ceil[key]
                )
        return self._store(user_id, "user", changes)

    # --- usage counters -----------------------------------------------------------------

    def _add(self, user_id: str, metrics: dict[str, float]) -> None:
        day = self._today()
        with transaction(self.conn):
            for metric, value in metrics.items():
                if value:
                    self.conn.execute(
                        "INSERT INTO usage_daily(user_id, day, metric, value) VALUES (?, ?, ?, ?)"
                        " ON CONFLICT(user_id, day, metric) DO UPDATE SET value = value + ?",
                        (user_id, day, metric, value, value),
                    )

    def _get(self, user_id: str, metric: str, day: str | None = None) -> float:
        row = self.conn.execute(
            "SELECT value FROM usage_daily WHERE user_id = ? AND day = ? AND metric = ?",
            (user_id, day or self._today(), metric),
        ).fetchone()
        return row["value"] if row else 0.0

    def _month_spend(self, user_id: str) -> float:
        prefix = self._today()[:8]  # 'YYYY-MM-'
        row = self.conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM usage_daily"
            " WHERE user_id = ? AND metric = 'cost_usd' AND day >= ? AND day < ?",
            (user_id, prefix + "01", prefix + "32"),
        ).fetchone()
        return float(row[0])

    def usage(self, user_id: str, day: str | None = None) -> dict[str, float]:
        """All metrics of one UTC day (default today)."""
        rows = self.conn.execute(
            "SELECT metric, value FROM usage_daily WHERE user_id = ? AND day = ? ORDER BY metric",
            (user_id, day or self._today()),
        ).fetchall()
        return {r["metric"]: r["value"] for r in rows}

    def usage_range(self, user_id: str, start: str | date, end: str | date) -> dict[str, Any]:
        """Per-day metrics and totals for ``start <= day <= end`` (inclusive, UTC dates)."""
        start, end = str(start), str(end)
        rows = self.conn.execute(
            "SELECT day, metric, value FROM usage_daily WHERE user_id = ? AND day BETWEEN ? AND ?"
            " ORDER BY day, metric",
            (user_id, start, end),
        ).fetchall()
        days: dict[str, dict[str, float]] = {}
        totals: dict[str, float] = {}
        for r in rows:
            days.setdefault(r["day"], {})[r["metric"]] = r["value"]
            totals[r["metric"]] = totals.get(r["metric"], 0.0) + r["value"]
        return {"start": start, "end": end, "days": days, "totals": totals}

    # --- requests per minute --------------------------------------------------------------

    def _take(self, key: str, rpm: int | float | None) -> int:
        """Take a token; return 0 on success or the seconds to wait."""
        if rpm is None:
            return 0
        if rpm <= 0:
            return 60
        now, rate = self.clock(), rpm / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(float(rpm), now)
        bucket.tokens = min(float(rpm), bucket.tokens + (now - bucket.ts) * rate)
        bucket.ts = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0
        return max(1, math.ceil((1.0 - bucket.tokens) / rate))

    def check_request(self, user_id: str, persona_id: str, subject_id: str, tool: str) -> None:
        """Per-call RPM check for the token/key (``subject_id``) and the persona.

        On success counts ``calls:<tool>``. Raises :class:`RateLimited` otherwise; a token
        is only taken from the persona bucket when the subject bucket allowed the call.
        """
        lim = self.effective_limits(user_id)
        wait = self._take(f"subject:{subject_id}", lim["rpm_subject"])
        if not wait:
            wait = self._take(f"persona:{persona_id}", lim["rpm_persona"])
        if wait:
            raise RateLimited("too many requests", retry_after=wait, limit="rpm")
        self._add(user_id, {f"calls:{tool}": 1})

    # --- predict ----------------------------------------------------------------------------

    def check_predict(self, user_id: str, persona_id: str, audience_id: str) -> None:
        """Raise :class:`RateLimited` when the persona or audience daily predict cap is hit."""
        lim = self.effective_limits(user_id)
        cap = lim["predict_per_day"]
        if cap is not None and self._get(user_id, "predict") >= cap:
            raise RateLimited(
                "daily predict limit reached",
                retry_after=self._secs_to_midnight(),
                limit="predict_per_day",
            )
        cap = lim["predict_per_audience_per_day"]
        if cap is not None and self._get(user_id, f"predict:aud:{audience_id}") >= cap:
            raise RateLimited(
                "daily predict limit for this audience reached",
                retry_after=self._secs_to_midnight(),
                limit="predict_per_audience_per_day",
            )

    def record_predict(self, user_id: str, persona_id: str, audience_id: str) -> None:
        """Count one predict for the persona and the audience.

        Call only after an LLM call for the prediction was actually made (not for rate-limited,
        spend-capped or no-LLM early escalations).
        """
        self._add(user_id, {"predict": 1, f"predict:aud:{audience_id}": 1})

    # --- LLM tokens and spend ---------------------------------------------------------------

    def _spend_flag(self, persona_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT spend_capped_at FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()
        return row["spend_capped_at"] if row else None

    def _set_spend_flag(self, persona_id: str, value: str | None) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE personas SET spend_capped_at = ? WHERE id = ?", (value, persona_id)
            )

    def spend_state(self, user_id: str, persona_id: str) -> dict[str, Any]:
        """Refresh and return the spend-cap flag (clears it in a new month or under a raised
        cap). Returns ``{spend_capped, spend_capped_at, spend_month_usd, spend_cap_usd}``."""
        cap = self.effective_limits(user_id)["spend_cap_usd"]
        spent = self._month_spend(user_id)
        flag = self._spend_flag(persona_id)
        over = cap is not None and spent >= cap
        if flag is not None:
            same_month = parse_ts(flag).strftime("%Y-%m") == self._now().strftime("%Y-%m")
            if not (same_month and over):
                self._set_spend_flag(persona_id, None)
                flag = None
        if flag is None and over:
            flag = format_ts(self._now())
            self._set_spend_flag(persona_id, flag)
        return {
            "spend_capped": flag is not None,
            "spend_capped_at": flag,
            "spend_month_usd": spent,
            "spend_cap_usd": cap,
        }

    def check_llm_budget(self, user_id: str, persona_id: str) -> None:
        """Before an LLM call: :class:`RateLimited` for tokens/day, :class:`SpendCap` when the
        monthly spend cap is reached."""
        if self.spend_state(user_id, persona_id)["spend_capped"]:
            raise SpendCap("monthly spend cap reached", retry_after=self._secs_to_month_end())
        cap = self.effective_limits(user_id)["llm_tokens_per_day"]
        used = self._get(user_id, "llm_tokens_in") + self._get(user_id, "llm_tokens_out")
        if cap is not None and used >= cap:
            raise RateLimited(
                "daily LLM token limit reached",
                retry_after=self._secs_to_midnight(),
                limit="llm_tokens_per_day",
            )

    def record_llm_usage(
        self,
        user_id: str,
        persona_id: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float | None,
    ) -> None:
        """After an LLM call: count tokens and cost (None = free/local backend); sets the
        spend flag when the monthly cap is reached."""
        self._add(
            user_id,
            {
                "llm_tokens_in": max(0, tokens_in),
                "llm_tokens_out": max(0, tokens_out),
                "cost_usd": max(0.0, cost_usd or 0.0),
            },
        )
        if cost_usd:
            self.spend_state(user_id, persona_id)

    # --- ingest -----------------------------------------------------------------------------

    def check_ingest(self, user_id: str, persona_id: str, records: int, nbytes: int) -> None:
        """:class:`TooLarge` for per-call batch limits, :class:`RateLimited` when this batch
        would exceed today's ingest bytes."""
        lim = self.effective_limits(user_id)
        if lim["batch_records"] is not None and records > lim["batch_records"]:
            raise TooLarge(
                "too many records in one call", limit="batch_records", maximum=lim["batch_records"]
            )
        if lim["batch_bytes"] is not None and nbytes > lim["batch_bytes"]:
            raise TooLarge("batch too large", limit="batch_bytes", maximum=lim["batch_bytes"])
        cap = lim["ingest_bytes_per_day"]
        if cap is not None and self._get(user_id, "ingest_bytes") + nbytes > cap:
            raise RateLimited(
                "daily ingest limit reached",
                retry_after=self._secs_to_midnight(),
                limit="ingest_bytes_per_day",
            )

    def record_ingest(self, user_id: str, persona_id: str, records: int, nbytes: int) -> None:
        self._add(user_id, {"ingest_records": records, "ingest_bytes": nbytes})

    # --- jobs -------------------------------------------------------------------------------

    @asynccontextmanager
    async def job_slot(self, user_id: str, persona_id: str) -> AsyncIterator[None]:
        """Hold one of the persona's concurrent job slots; raises :class:`RateLimited` at once
        when all are taken (no queueing)."""
        cap = self.effective_limits(user_id)["concurrent_jobs"]
        running = self._jobs.get(persona_id, 0)
        if cap is not None and running >= cap:
            raise RateLimited("a job is already running", retry_after=30, limit="concurrent_jobs")
        self._jobs[persona_id] = running + 1
        try:
            yield
        finally:
            left = self._jobs[persona_id] - 1
            if left:
                self._jobs[persona_id] = left
            else:
                del self._jobs[persona_id]
