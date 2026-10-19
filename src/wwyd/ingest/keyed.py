# SPDX-License-Identifier: AGPL-3.0-or-later
"""Upsert of keyed detector claims (SPEC §4.5, §7.2, §10.1 "keyed claims merge by key").

A detector finding is merged into the live (non-archived) claim with the same ``key``:
``evidence.stats`` buckets are added, ``sources`` summed, ``period`` widened, then the
share, confidence and text are recomputed from the merged statistics. Choices:

* **rejected** keyed claim: left untouched (negative memory); the finding is dropped, so a
  rejected habit is never resurrected by re-runs. Archiving it frees the key.
* **approved** claim: evidence and numbers are refreshed; the text is re-rendered only while
  ``origin`` is still ``derived`` (the owner never edited it). Status stays approved.
* **pending** claim: everything is refreshed.
* Below threshold with no live claim: nothing is stored (the statistics of that batch are
  not kept anywhere); with a live claim: the merged evidence is stored, text unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from wwyd.db import transaction
from wwyd.ids import now_iso
from wwyd.ingest.detectors import Finding, render
from wwyd.ingest.detectors.common import add_stats, period_days
from wwyd.store.audiences import audience_ids_by_name
from wwyd.store.categories import category_ids_by_path
from wwyd.store.claims import insert_claim
from wwyd.store.common import dumps, normalize_text, parent_path

Outcome = Literal["created", "updated", "rejected", "below_threshold", "skipped"]


def _widen(a: str | None, b: str) -> str:
    if not a:
        return b
    a0, a1 = a.split("..")
    b0, b1 = b.split("..")
    return f"{min(a0, b0)}..{max(a1, b1)}"


def _category_id(categories: dict[str, str], path: str) -> str | None:
    current: str | None = path
    while current is not None:
        if current in categories:
            return categories[current]
        current = parent_path(current)
    return None


def upsert_finding(
    conn: sqlite3.Connection, persona_id: str, finding: Finding
) -> tuple[Outcome, str | None]:
    with transaction(conn):
        row = conn.execute(
            "SELECT id, status, evidence, origin FROM claims"
            " WHERE persona_id = ? AND key = ? AND status <> 'archived'",
            (persona_id, finding.key),
        ).fetchone()
        if row is not None and row["status"] == "rejected":
            return "rejected", None
        old = json.loads(row["evidence"]) if row is not None else {}
        stats = add_stats(old.get("stats") or {}, finding.stats)
        sources = dict(old.get("sources") or {})
        for name, count in finding.sources.items():
            sources[name] = sources.get(name, 0) + count
        period = _widen(old.get("period"), finding.period)
        rendered = render(finding.key, stats, period_days(period))
        evidence = {"sources": sources, "period": period, "stats": stats}
        if rendered is not None:
            evidence["share"] = round(rendered.share, 3)
        ts = now_iso()

        if row is not None:
            if rendered is None:
                conn.execute(
                    "UPDATE claims SET evidence = ?, updated_at = ? WHERE id = ?",
                    (dumps({**old, **evidence}), ts, row["id"]),
                )
                return "updated", row["id"]
            if row["origin"] == "derived":
                conn.execute(
                    "UPDATE claims SET text = ?, norm_text = ? WHERE id = ?",
                    (rendered.text, normalize_text(rendered.text), row["id"]),
                )
            conn.execute(
                "UPDATE claims SET evidence = ?, confidence = ?, last_evidence_at = ?,"
                " updated_at = ? WHERE id = ?",
                (dumps(evidence), rendered.confidence, ts, ts, row["id"]),
            )
            return "updated", row["id"]

        if rendered is None:
            return "below_threshold", None
        category_id = _category_id(category_ids_by_path(conn, persona_id), finding.category)
        if category_id is None:
            return "skipped", None
        audience_ids: list[str] = []
        if finding.audience is not None:
            by_name = audience_ids_by_name(conn, persona_id)
            if finding.audience not in by_name:
                return "skipped", None
            audience_ids = [by_name[finding.audience]]
        claim_id = insert_claim(
            conn,
            persona_id,
            kind=finding.kind,
            category_id=category_id,
            text=rendered.text,
            audiences_all=finding.audience is None,
            audience_ids=audience_ids,
            visibility="shared",
            status="pending",
            confidence=rendered.confidence,
            evidence=evidence,
            origin="derived",
            key=finding.key,
            last_evidence_at=ts,
        )
        return "created", claim_id
