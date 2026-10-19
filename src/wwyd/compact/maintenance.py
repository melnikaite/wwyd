# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decay (SPEC §10.3), size budget (§10.4) and purge (§10.5)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from wwyd.compact.constants import (
    CHARS_PER_TOKEN,
    DECAY_FLOOR,
    DECAY_HALF_LIFE_DAYS,
    DECAY_MIN_STEP,
    MAX_APPROVED_TOKENS,
    MAX_CLAIMS_PER_CATEGORY,
    PURGE_AFTER,
)
from wwyd.compact.run import Run, evidence_weight
from wwyd.db import transaction
from wwyd.ids import format_ts, parse_ts
from wwyd.store import changes


def run_steps(run: Run, steps: Sequence[str]) -> None:
    if "decay" in steps:
        decay(run)
    if "budget" in steps:
        budget(run)
    if "purge" in steps:
        purge(run)


def decayed_confidence(confidence: float, days: float) -> float:
    """``c × 0.5^(Δmonths/6)``."""
    return confidence * 0.5 ** (max(0.0, days) / DECAY_HALF_LIFE_DAYS)


def decay(run: Run) -> None:
    """Applied from the stored confidence since the later of ``last_evidence_at`` and the
    previous decay step (exact, since exponential decay compounds). ``origin: owner`` is
    exempt; approved claims below the floor become proposals."""
    values: dict[str, float] = {}
    for row in run.live_rows():
        if row["origin"] == "owner":
            continue
        refs = [x for x in (row["last_evidence_at"], row["decayed_at"]) if x] or [row["created_at"]]
        days = (run.now - parse_ts(max(refs))).total_seconds() / 86400
        new = decayed_confidence(row["confidence"], days)
        if new < DECAY_FLOOR:
            plan = {
                "kind": "archive",
                "claim": row["id"],
                "reason": "decayed",
                "confidence": round(new, 4),
            }
            state = "proposed" if row["status"] == "approved" else "applied"
            run.emit("decay", plan, state=state, reason="decayed")
        elif row["confidence"] - new >= DECAY_MIN_STEP:
            values[row["id"]] = round(new, 4)
    if values:
        run.emit("decay", {"kind": "confidence", "values": values}, state="applied", reason="decay")


def _archive_for_budget(run: Run, row, reason: str) -> None:  # type: ignore[no-untyped-def]
    state = "proposed" if row["status"] == "approved" else "applied"
    plan = {"kind": "archive", "claim": row["id"], "reason": "budget"}
    run.emit("archive_budget", plan, state=state, reason=reason)


def _lightest_first(rows):  # type: ignore[no-untyped-def]
    return sorted(rows, key=lambda r: (evidence_weight(r), r["last_evidence_at"] or "", r["seq"]))


def budget(run: Run) -> None:
    """At most N live claims per category and M approved tokens per persona. Claims that
    already have an open proposal count as on their way out."""
    by_category = defaultdict(list)
    for row in run.live_rows():
        by_category[row["category_id"]].append(row)
    for rows in by_category.values():
        excess = len(rows) - MAX_CLAIMS_PER_CATEGORY
        for row in _lightest_first(rows):
            if excess <= 0:
                break
            _archive_for_budget(run, row, "category_budget")
            excess -= 1
    approved = run.live_rows(statuses=("approved",))
    tokens = sum(len(r["text"]) for r in approved) / CHARS_PER_TOKEN
    for row in _lightest_first(approved):
        if tokens <= MAX_APPROVED_TOKENS:
            break
        _archive_for_budget(run, row, "token_budget")
        tokens -= len(row["text"]) / CHARS_PER_TOKEN


def purge(run: Run) -> None:
    """Hard-delete claims archived longer than the restore window; their change-log rows
    become ``expired``. The purge row itself carries IDs only."""
    cutoff = format_ts(run.now - PURGE_AFTER)
    ids = [
        r["id"]
        for r in run.conn.execute(
            "SELECT id FROM claims WHERE persona_id = ? AND status = 'archived'"
            " AND archived_at IS NOT NULL AND archived_at < ?",
            (run.persona_id, cutoff),
        )
    ]
    if not ids:
        return
    change_id = None
    if run.apply:
        run.conn.execute("PRAGMA secure_delete = ON")
        with transaction(run.conn):
            for claim_id in ids:
                run.conn.execute(
                    "DELETE FROM claims WHERE persona_id = ? AND id = ?", (run.persona_id, claim_id)
                )
            changes.expire_for_claims(run.conn, run.persona_id, ids)
            change_id = changes.record(
                run.conn,
                run.persona_id,
                "purge",
                {"claims": ids},
                state="expired",
                reason="restore_window_over",
                job_id=run.job_id,
            )
    entry = {"op": "purge", "state": "expired", "claims": ids, "reason": "restore_window_over"}
    run.changes.append({"id": change_id, **entry} if change_id else entry)
