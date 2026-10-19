# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compaction (SPEC §10).

Modules: ``constants`` (defaults), ``run`` (one run, ``compact``), ``dedupe`` (dedupe,
absorb, contradictions, LLM judge), ``maintenance`` (decay, budget, purge), ``scheduler``
(nightly). Imports here stay lazy: ``wwyd.store`` imports ``wwyd.compact.constants``.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from typing import Any


async def compact(conn: sqlite3.Connection, persona_id: str, **kwargs: Any) -> dict[str, Any]:
    from wwyd.compact.run import compact as _compact

    return await _compact(conn, persona_id, **kwargs)


async def run_after_job(
    conn: sqlite3.Connection, persona_id: str, claim_ids: Sequence[str], *, job_id: str
) -> dict[str, Any]:
    """Dedupe and resolve contradictions for the claims an ingest job created or touched;
    every change carries ``job_id``. Returns counts by op plus ``skipped_pairs``.

    With the persona setting ``llm_for_ingest`` off, the LLM judge is not called and
    borderline pairs are counted as skipped."""
    if not claim_ids:
        return {}
    from wwyd.store.personas import get_settings

    use_llm = bool(get_settings(conn, persona_id).get("llm_for_ingest", True))
    result = await compact(
        conn,
        persona_id,
        apply=True,
        steps=("dedupe", "contradictions"),
        claim_ids=list(claim_ids),
        job_id=job_id,
        use_llm=use_llm,
    )
    counts: dict[str, Any] = dict(Counter(c["op"] for c in result["changes"]))
    if result["skipped_pairs"]:
        counts["skipped_pairs"] = result["skipped_pairs"]
    return counts
