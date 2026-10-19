# SPDX-License-Identifier: AGPL-3.0-or-later
"""One compaction run: collects (and, with ``apply``, writes) changes (SPEC §10)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from wwyd.db import transaction
from wwyd.errors import Conflict
from wwyd.store import changes
from wwyd.store.evidence import evidence_weight as weight_of

STEPS = (
    "dedupe",
    "contradictions",
    "decay",
    "budget",
    "purge",
    "category_merge",
    "category_split",
)


def evidence_weight(row: sqlite3.Row) -> float:
    """``confidence × log(1 + Σ weighted sources)`` (SPEC §4.5, §10.2)."""
    return weight_of(row["confidence"], row["evidence"])


def _archived_by(plan: dict[str, Any]) -> list[str]:
    kind = plan["kind"]
    if kind == "merge":
        return [plan["drop"]]
    if kind == "supersede":
        return [plan["loser"]]
    if kind == "archive":
        return [plan["claim"]]
    return []


@dataclass
class Run:
    conn: sqlite3.Connection
    persona_id: str
    apply: bool
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    job_id: str | None = None
    changes: list[dict[str, Any]] = field(default_factory=list)
    consumed: set[str] = field(default_factory=set)
    judged_pairs: int = 0
    skipped_pairs: int = 0
    judge_pending: int = 0  # dry run: borderline pairs a real run would send to the judge
    use_llm: bool = True  # False: borderline pairs are skipped instead of judged
    # Category steps (SPEC §10.6): LLM failures per step, discarded split groupings, and
    # (dry run) what a real run would send to the LLM.
    skipped_steps: dict[str, int] = field(default_factory=dict)
    discarded_splits: int = 0
    category_pending: dict[str, list[str]] = field(default_factory=dict)

    def emit(self, op: str, plan: dict[str, Any], *, state: str, reason: str) -> bool:
        """Record one change (or, in a dry run, just report it). False if not emitted."""
        claims = changes.plan_claims(plan)
        if set(claims) & self.consumed:
            return False
        if state == "proposed" and changes.has_open_proposal(self.conn, self.persona_id, claims):
            return False
        change_id = None
        if self.apply:
            try:
                with transaction(self.conn):
                    change_id = changes.execute(
                        self.conn,
                        self.persona_id,
                        op,
                        plan,
                        state=state,
                        reason=reason,
                        job_id=self.job_id,
                    )
            except Conflict:
                return False
        self.consumed.update(_archived_by(plan))
        entry: dict[str, Any] = {"op": op, "state": state, "claims": claims, "reason": reason}
        if change_id is not None:
            entry = {"id": change_id, **entry}
        self.changes.append(entry)
        return True

    def live_rows(
        self,
        statuses: Sequence[str] = ("pending", "approved"),
        claim_ids: Sequence[str] | None = None,
    ) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT * FROM claims WHERE persona_id = ? AND status IN ({marks}) ORDER BY seq",
            [self.persona_id, *statuses],
        ).fetchall()
        wanted = set(claim_ids) if claim_ids is not None else None
        return [
            r
            for r in rows
            if r["id"] not in self.consumed and (wanted is None or r["id"] in wanted)
        ]

    def result(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "changes": self.changes,
            "judged_pairs": self.judged_pairs,
            "skipped_pairs": self.skipped_pairs,
        }
        if not self.apply:
            out["judge_pending"] = self.judge_pending
            if self.category_pending:
                out["category_pending"] = self.category_pending
        if self.skipped_steps:
            out["skipped_steps"] = self.skipped_steps
        if self.discarded_splits:
            out["discarded_splits"] = self.discarded_splits
        return out


async def compact(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    apply: bool = False,
    steps: Sequence[str] | None = None,
    now: datetime | None = None,
    claim_ids: Sequence[str] | None = None,
    job_id: str | None = None,
    use_llm: bool = True,
    forced_pairs: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Run the requested steps. With ``apply=False`` nothing is written and the LLM judge
    is not called (it would record usage); borderline pairs are counted in ``judge_pending``
    and the categories the category steps would send are listed in ``category_pending``."""
    from wwyd.compact import categories, dedupe, maintenance

    wanted = tuple(steps) if steps else STEPS
    run = Run(conn, persona_id, apply, now or datetime.now(UTC), job_id, use_llm=use_llm)
    if "dedupe" in wanted or "contradictions" in wanted:
        await dedupe.run_pairs(
            run,
            claim_ids=claim_ids,
            dedupe="dedupe" in wanted,
            contradictions="contradictions" in wanted,
            forced=forced_pairs,
        )
    maintenance.run_steps(run, wanted)
    await categories.run_steps(run, wanted)
    return run.result()
