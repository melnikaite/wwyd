# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running LLM-written analyzers (SPEC §7.4 step 5, §7.5).

An LLM analyzer tests one hypothesis: it reads ``{"episodes": [...]}`` on stdin (the
processed episodes of its category subtree with the host fields of §7.2) and prints
``{"n": {"YYYY-MM": {"_": n}}, "hits": {...}}`` (optionally ``base_n`` / ``base_hits``).
Only those four metrics under the key ``_`` are kept (anything else is counted as dropped
keys); the rest goes through the shared validate -> filter -> merge path.

The sandbox is blocking, so every run goes through ``asyncio.to_thread``; database writes
stay on the event loop thread. Run wall time is charged to the persona's
``sandbox_cpu_seconds_per_day``; once it is spent, the remaining LLM analyzers of the job are
skipped (``skipped_sandbox_budget``) and the job is not ``partial``. A failed run counts in
``sandbox_errors``; for LLM analyzers ``failures`` counts *consecutive* failures (reset by a
successful run), and the third one disables the analyzer.

Re-rendering: the claim ``anl:<id>`` is rendered from the state of the last 12 months (up to
the latest month with data). It passes with ``n >= 30`` and share ``>= 0.75`` there, unless
the last 3 months hold at least 10 samples with a share below 0.75; a failing render
weakens the claim like a contradiction (``analyzers.claims.upsert_keyed``). Evidence
``stats``: ``n`` and ``hits`` (12-month totals) and ``n_by_month`` / ``hits_by_month``,
lists over every month from the first to the last month of the window (the months of
``period``, oldest first; 0 for a month without data).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import date, tzinfo
from typing import Any

from wwyd import sandbox
from wwyd.analyzers.builtin.base import ClaimSpec, Item, host_fields, pct
from wwyd.analyzers.contract import ContractError, validate_output
from wwyd.analyzers.keyfilter import VOCAB
from wwyd.analyzers.state import State, read_state
from wwyd.db import transaction
from wwyd.ingest.episodes import Episode
from wwyd.ingest.guard import BatchGuard
from wwyd.limits import get_limiter, sandbox_concurrency

METRICS = ("n", "hits", "base_n", "base_hits")
KEY_CLASSES = {m: (VOCAB,) for m in METRICS}
MIN_N = 30
MIN_SHARE = 0.75
WINDOW_MONTHS = 12
RECENT_MONTHS = 3
RECENT_MIN_N = 10
DISABLE_AFTER = 3
PLACEHOLDERS = ("share", "n", "hits", "months")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

__all__ = ["SandboxRunner", "fill_template", "payload", "render", "run_active"]


# --- input ------------------------------------------------------------------------------


def episode_json(item: Item, path: str) -> dict[str, Any]:
    ep = item.episode
    return {
        "local_start": item.local_start.isoformat() if item.local_start else None,
        "weekday": item.weekday,
        "hour": item.hour,
        "month": item.month,
        "duration_s": item.duration_s,
        "latency_s": item.latency_s,
        "labels": dict(ep.labels),
        "tags": list(ep.tags),
        "category": path,
        "audience": ep.audience,
        "where": ep.where,
        "situation": ep.situation,
        "decision": ep.decision,
        "said": ep.said,
    }


def payload(items: Sequence[Item]) -> bytes:
    """The stdin bytes of one run (in memory only, never a file; P1)."""
    return json.dumps(
        {"episodes": [episode_json(it, it.episode.category) for it in items]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def in_subtree(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


# --- the sandbox and its daily budget ---------------------------------------------------


class SandboxRunner:
    """Runs sources in the process-wide sandbox and charges the persona's daily budget."""

    def __init__(self, conn: sqlite3.Connection, user_id: str, counts: dict[str, Any]) -> None:
        self.limiter = get_limiter(conn)
        self.user_id, self.counts = user_id, counts
        self.exhausted = False

    @staticmethod
    def available() -> bool:
        return sandbox.available()

    def has_budget(self) -> bool:
        if self.exhausted:
            return False
        left = self.limiter.sandbox_seconds_left(self.user_id)
        if left is not None and left <= 0:
            self.exhausted = True
        return not self.exhausted

    async def run(self, source: str, stdin: bytes) -> sandbox.RunResult | None:
        """The run's result, or None when today's sandbox time is spent (nothing ran)."""
        if not self.has_budget():
            return None
        box = await asyncio.to_thread(sandbox.get_sandbox, None, sandbox_concurrency())
        result = await asyncio.to_thread(box.run, source, stdin)
        self.limiter.record_sandbox_seconds(self.user_id, result.wall_s)
        return result


def keep_metrics(output: Any) -> tuple[dict[str, dict[str, dict[str, float]]], int]:
    """Validate an LLM analyzer's output; keep the four metrics under key ``_``. Returns the
    kept output and the number of keys dropped; raises ``ContractError`` when invalid."""
    valid = validate_output(output)
    kept: dict[str, dict[str, dict[str, float]]] = {}
    dropped = 0
    for metric, months in valid.items():
        for month, keys in months.items():
            for key, value in keys.items():
                if metric in METRICS and key == "_":
                    kept.setdefault(metric, {})[month] = {"_": value}
                else:
                    dropped += 1
    return kept, dropped


# --- rendering --------------------------------------------------------------------------


def _shift(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:])
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _month_range(first: str, last: str) -> list[str]:
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = _shift(cur, 1)
    return out


def _last_day(month: str) -> date:
    nxt = _shift(month, 1)
    return date.fromordinal(date(int(nxt[:4]), int(nxt[5:]), 1).toordinal() - 1)


def fill_template(template: str, values: dict[str, str]) -> str | None:
    """Fill ``{share}``, ``{n}``, ``{hits}``, ``{months}``; None on any other placeholder."""
    bad = False

    def sub(m: re.Match[str]) -> str:
        nonlocal bad
        name = m.group(1).strip()
        if name not in values:
            bad = True
            return ""
        return values[name]

    text = _PLACEHOLDER.sub(sub, template)
    if bad or "{" in text or "}" in text:
        return None
    return " ".join(text.split())


def template_ok(template: str) -> bool:
    return fill_template(template, {p: "1" for p in PLACEHOLDERS}) is not None


def render(state: State, template: str, kind: str) -> ClaimSpec:
    """The claim spec of an LLM analyzer from its accumulated state (see module docstring)."""
    n_by = {m: v.get("_", 0.0) for m, v in state.get("n", {}).items()}
    h_by = {m: v.get("_", 0.0) for m, v in state.get("hits", {}).items()}
    months = sorted(m for m, v in n_by.items() if v > 0)
    if not months:
        return ClaimSpec("", kind, None, 0.0, 0, None, {"n": 0, "hits": 0})
    last = months[-1]
    start = _shift(last, -(WINDOW_MONTHS - 1))
    window = [m for m in months if m >= start]
    span = _month_range(window[0], last)
    n = sum(n_by.get(m, 0.0) for m in span)
    hits = min(n, sum(h_by.get(m, 0.0) for m in span))
    share = hits / n if n else 0.0
    recent = [m for m in span if m >= _shift(last, -(RECENT_MONTHS - 1))]
    n3 = sum(n_by.get(m, 0.0) for m in recent)
    h3 = sum(h_by.get(m, 0.0) for m in recent)
    weak_recent = n3 >= RECENT_MIN_N and h3 / n3 < MIN_SHARE
    stats: dict[str, float | list[float]] = {
        "n": n,
        "hits": hits,
        "n_by_month": [n_by.get(m, 0.0) for m in span],
        "hits_by_month": [h_by.get(m, 0.0) for m in span],
    }
    period = (date(int(span[0][:4]), int(span[0][5:]), 1), _last_day(last))
    text = None
    if n >= MIN_N and share >= MIN_SHARE and not weak_recent:
        text = fill_template(
            template,
            {
                "share": f"{pct(share)}%",
                "n": str(int(n)),
                "hits": str(int(hits)),
                "months": str(len(span)),
            },
        )
    return ClaimSpec("", kind, text, share, int(n), period, stats)


# --- running active analyzers -----------------------------------------------------------


async def run_active(
    conn: sqlite3.Connection,
    *,
    persona_id: str,
    user_id: str,
    episodes: list[Episode],
    tz: tzinfo,
    counts: dict[str, Any],
    job_id: str | None = None,
    guard: BatchGuard | None = None,
) -> set[str]:
    """Run every active LLM analyzer over the processed episodes of its subtree, merge the
    state and re-render its claim; returns the touched claim IDs."""
    from wwyd.analyzers import _Run
    from wwyd.analyzers.claims import anl_key, upsert_keyed
    from wwyd.store import analyzers as store

    for name in ("sandbox_errors", "skipped_sandbox_budget", "analyzers_disabled"):
        counts.setdefault(name, 0)
    rows = conn.execute(
        "SELECT a.*, c.path AS path FROM analyzers a JOIN categories c ON c.id = a.scope_id"
        " WHERE a.persona_id = ? AND a.origin = 'llm' AND a.status = 'active'"
        " AND a.scope_kind = 'category' ORDER BY a.seq",
        (persona_id,),
    ).fetchall()
    if not rows or not episodes:
        return set()
    runner = SandboxRunner(conn, user_id, counts)
    if not runner.available():
        counts["sandbox_unavailable"] = counts.get("sandbox_unavailable", 0) + len(rows)
        return set()
    run = _Run(conn, persona_id, episodes, tz, counts, job_id)
    items = [host_fields(ep, tz, run.audiences) for ep in episodes]
    touched: set[str] = set()
    for i, row in enumerate(rows):
        scoped = [it for it in items if in_subtree(it.episode.category, row["path"])]
        if not scoped:
            continue
        result = await runner.run(row["source"], payload(scoped))
        if result is None:
            counts["skipped_sandbox_budget"] += len(rows) - i
            break
        kept: dict[str, Any] | None = None
        if result.ok:
            try:
                kept, dropped = keep_metrics(result.output)
                run.bump("analyzer_keys_dropped", dropped)
            except ContractError:
                kept = None
        del result
        with transaction(conn):
            if kept is None:
                counts["sandbox_errors"] += 1
                if store.record_llm_run(conn, row["id"], failed=True):
                    counts["analyzers_disabled"] += 1
                continue
            run.apply_output(row["id"], kept, KEY_CLASSES, (), row["scope_id"])
            run.bump("analyzers_run")
            spec = render(read_state(conn, row["id"]), row["statement_template"], row["kind"])
            if spec.text is not None and guard is not None and guard.check(spec.text):
                counts["dropped_by_guard"] = counts.get("dropped_by_guard", 0) + 1
                store.record_llm_run(conn, row["id"], failed=False)
                continue
            outcome, claim_id = upsert_keyed(
                conn,
                persona_id,
                key=anl_key(row["id"]),
                spec=spec,
                category_id=row["scope_id"],
                job_id=job_id,
            )
            name = {
                "created": "analyzer_claims_created",
                "updated": "analyzer_claims_updated",
                "weakened": "analyzer_claims_weakened",
                "proposed": "analyzer_claims_proposed",
            }.get(outcome)
            if name:
                run.bump(name)
            if claim_id is not None:
                touched.add(claim_id)
            store.record_llm_run(conn, row["id"], failed=False, claim_id=claim_id)
    return touched
