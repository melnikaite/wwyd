# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyed analyzer claims (SPEC §4.5, §7.2, §10.1 "keyed claims merge by key", §10.2).

One live claim per key. The numbers live in ``analyzer_state``; every run re-renders the
claim from the accumulated state, so the evidence is *replaced* (not summed). Outcomes:

* **rejected** claim: untouched (negative memory); archiving it frees the key.
* passes, no live claim: a new ``pending`` derived claim.
* passes, live claim: evidence, confidence and ``last_evidence_at`` refreshed; the text is
  re-rendered while ``origin`` is still ``derived`` (the owner never edited it).
* below threshold, no live claim: nothing (the state keeps accumulating).
* below threshold, ``pending`` claim: archived (an applied ``decay`` change, reason
  ``analyzer_weakened``); a later passing run creates a fresh pending claim.
* below threshold, ``approved`` claim: numbers refreshed, text kept, and a ``proposed``
  archive change for the owner (once; not while one is open), like a lost contradiction.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from wwyd.analyzers.builtin.base import ClaimSpec
from wwyd.ids import now_iso
from wwyd.ingest.guard import find_identifier
from wwyd.store import changes
from wwyd.store.claims import insert_claim
from wwyd.store.common import dumps, normalize_text

Outcome = Literal[
    "created", "updated", "weakened", "proposed", "rejected", "below_threshold", "skipped"
]
WEAKENED_REASON = "analyzer_weakened"


def claim_key(name: str, scope_id: str, facet: str) -> str:
    return f"builtin:{name}:{scope_id}" + (f":{facet}" if facet else "")


def _evidence(spec: ClaimSpec) -> dict:
    ev: dict = {"sources": {"analyzer": int(spec.samples)}, "stats": spec.stats}
    if spec.text is not None or spec.share:
        ev["share"] = round(min(max(spec.share, 0.0), 1.0), 3)
    if spec.period is not None:
        ev["period"] = f"{spec.period[0].isoformat()}..{spec.period[1].isoformat()}"
    return ev


def upsert_keyed(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    key: str,
    spec: ClaimSpec,
    category_id: str,
    job_id: str | None = None,
) -> tuple[Outcome, str | None]:
    """Merge one rendered facet into its keyed claim; the caller holds the transaction."""
    row = conn.execute(
        "SELECT id, status, origin FROM claims"
        " WHERE persona_id = ? AND key = ? AND status <> 'archived'",
        (persona_id, key),
    ).fetchone()
    if row is not None and row["status"] == "rejected":
        return "rejected", None
    text = spec.text
    if text is not None and (len(text) > 500 or find_identifier(text) is not None):
        text = None  # rendered text holds numbers and paths only; never store a failing one
    evidence = dumps(_evidence(spec))
    ts = now_iso()

    if text is None:
        if row is None:
            return "below_threshold", None
        conn.execute(
            "UPDATE claims SET evidence = ?, updated_at = ? WHERE id = ?",
            (evidence, ts, row["id"]),
        )
        plan = {"kind": "archive", "claim": row["id"], "reason": "decayed"}
        if row["status"] == "pending":
            changes.execute(conn, persona_id, "decay", plan, reason=WEAKENED_REASON, job_id=job_id)
            return "weakened", None
        if not changes.has_open_proposal(conn, persona_id, [row["id"]]):
            changes.execute(
                conn,
                persona_id,
                "decay",
                plan,
                state="proposed",
                reason=WEAKENED_REASON,
                job_id=job_id,
            )
            return "proposed", row["id"]
        return "updated", row["id"]

    if row is not None:
        if row["origin"] == "derived":
            conn.execute(
                "UPDATE claims SET text = ?, norm_text = ? WHERE id = ?",
                (text, normalize_text(text), row["id"]),
            )
        conn.execute(
            "UPDATE claims SET evidence = ?, confidence = ?, last_evidence_at = ?,"
            " updated_at = ? WHERE id = ?",
            (evidence, spec.confidence, ts, ts, row["id"]),
        )
        return "updated", row["id"]

    audience_ids: list[str] = []
    if spec.audience_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM audiences WHERE persona_id = ? AND id = ?",
            (persona_id, spec.audience_id),
        ).fetchone()
        if exists is None:
            return "skipped", None
        audience_ids = [spec.audience_id]
    claim_id = insert_claim(
        conn,
        persona_id,
        kind=spec.kind,
        category_id=category_id,
        text=text,
        audiences_all=spec.audience_id is None,
        audience_ids=audience_ids,
        visibility="shared",
        status="pending",
        confidence=spec.confidence,
        evidence=_evidence(spec),
        origin="derived",
        key=key,
        last_evidence_at=ts,
    )
    return "created", claim_id
