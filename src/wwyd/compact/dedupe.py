# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedupe/absorb (SPEC §10.1) and contradictions (§10.2), with the batched LLM judge."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from wwyd.compact.constants import (
    BORDERLINE_JACCARD,
    DUPLICATE_JACCARD,
    FTS_CANDIDATES,
    JUDGE_MAX_PAIRS,
)
from wwyd.compact.run import Run, evidence_weight
from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.store.claims import find_similar, polarity_differs

log = logging.getLogger(__name__)

VERDICTS = ("same", "contradicts", "different")

JUDGE_SYSTEM = """\
You compare short statements about one person (the owner). For each numbered pair, answer:
- "same": both say the same thing about the owner (rewording, more or less detail).
- "contradicts": both cannot be true at the same time for the same situations.
- "different": they are about different things, or both can be true.
Statements may be in any language; judge the meaning, not the wording.
Answer only with JSON: {"verdicts": [{"pair": <number>, "verdict": "same"|"contradicts"|"different"}]}
with exactly one entry per pair.

Example input:
[{"pair": 1, "a": "Отвечает коротко, без приветствия.", "b": "Пишет короткие ответы и не здоровается."},
 {"pair": 2, "a": "Не назначает встречи на вечер пятницы.", "b": "Охотно соглашается на встречи в пятницу вечером."},
 {"pair": 3, "a": "Отвечает коротко, без приветствия.", "b": "Отвечает в течение нескольких минут."}]
Example output:
{"verdicts": [{"pair": 1, "verdict": "same"}, {"pair": 2, "verdict": "contradicts"},
 {"pair": 3, "verdict": "different"}]}
"""

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                },
                "required": ["pair", "verdict"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

Pair = tuple[str, str]


def _collect_pairs(run: Run, claim_ids: Sequence[str] | None) -> tuple[dict[Pair, float], dict]:
    """Candidate pairs (sorted id tuple → Jaccard) and the rows involved."""
    rows: dict[str, sqlite3.Row] = {}
    pairs: dict[Pair, float] = {}
    for seed in run.live_rows(claim_ids=claim_ids):
        rows[seed["id"]] = seed
        for cand, score in find_similar(
            run.conn,
            run.persona_id,
            kind=seed["kind"],
            category_id=seed["category_id"],
            text=seed["text"],
            exclude=seed["id"],
            limit=FTS_CANDIDATES,
        ):
            if score < BORDERLINE_JACCARD or cand["id"] in run.consumed:
                continue
            if seed["status"] == "rejected" and cand["status"] == "rejected":
                continue
            rows[cand["id"]] = cand
            a, b = sorted((seed["id"], cand["id"]))
            pairs[(a, b)] = score
    return pairs, rows


async def _judge(run: Run, pairs: list[Pair], rows: dict[str, sqlite3.Row]) -> dict[Pair, str]:
    """One judge call per category. Any LLM failure skips that category's pairs."""
    from wwyd.llm import get_llm

    by_category: dict[str, list[Pair]] = defaultdict(list)
    for pair in pairs:
        by_category[rows[pair[0]]["category_id"]].append(pair)
    verdicts: dict[Pair, str] = {}
    llm = None
    for group in by_category.values():
        batch, rest = group[:JUDGE_MAX_PAIRS], group[JUDGE_MAX_PAIRS:]
        run.skipped_pairs += len(rest)
        items = [
            {"pair": i + 1, "a": rows[a]["text"], "b": rows[b]["text"]}
            for i, (a, b) in enumerate(batch)
        ]
        try:
            llm = llm or get_llm(run.conn, run.persona_id)
            result = await llm.complete_json(
                purpose="judge",
                system=JUDGE_SYSTEM,
                user=json.dumps(items, ensure_ascii=False),
                schema=JUDGE_SCHEMA,
                max_tokens=64 + 24 * len(batch),
            )
        except (LLMUnavailable, SpendCap, RateLimited) as exc:
            log.info("compaction judge skipped: %s", exc.code)
            run.skipped_pairs += len(batch)
            continue
        got: dict[int, str] = {}
        data = result.data if isinstance(result.data, dict) else {}
        for v in data.get("verdicts") or []:
            if (
                isinstance(v, dict)
                and isinstance(v.get("pair"), int)
                and v.get("verdict") in VERDICTS
            ):
                got.setdefault(v["pair"], v["verdict"])
        for i, pair in enumerate(batch):
            if i + 1 in got:
                verdicts[pair] = got[i + 1]
                run.judged_pairs += 1
            else:
                run.skipped_pairs += 1
    return verdicts


async def run_pairs(
    run: Run, *, claim_ids: Sequence[str] | None, dedupe: bool, contradictions: bool
) -> None:
    pairs, rows = _collect_pairs(run, claim_ids)

    def sure(p: Pair) -> bool:
        return pairs[p] >= DUPLICATE_JACCARD and not polarity_differs(
            rows[p[0]]["text"], rows[p[1]]["text"]
        )

    verdicts = {p: "same" for p in pairs if sure(p)}
    borderline = [p for p in pairs if not sure(p)]
    if borderline:
        if not run.apply:
            run.judge_pending += len(borderline)
        elif run.use_llm:
            verdicts.update(await _judge(run, borderline, rows))
        else:
            run.skipped_pairs += len(borderline)
    for pair in sorted(verdicts, key=lambda p: -pairs[p]):
        a, b = rows[pair[0]], rows[pair[1]]
        if verdicts[pair] == "same" and dedupe:
            _same(run, a, b)
        elif verdicts[pair] == "contradicts" and contradictions:
            _contradicts(run, a, b)


def _same(run: Run, a: sqlite3.Row, b: sqlite3.Row) -> None:
    statuses = {a["status"], b["status"]}
    by_status = {a["status"]: a, b["status"]: b}
    if statuses == {"pending"}:
        keep, drop = sorted((a, b), key=lambda r: (r["created_at"], r["seq"]), reverse=True)
        plan = {"kind": "merge", "keep": keep["id"], "drop": drop["id"]}
        run.emit("merge", plan, state="applied", reason="duplicate")
    elif statuses == {"pending", "approved"}:
        plan = {
            "kind": "merge",
            "keep": by_status["approved"]["id"],
            "drop": by_status["pending"]["id"],
        }
        run.emit("absorb", plan, state="applied", reason="duplicate")
    elif statuses == {"pending", "rejected"}:
        plan = {
            "kind": "merge",
            "keep": by_status["rejected"]["id"],
            "drop": by_status["pending"]["id"],
        }
        run.emit("merge", plan, state="applied", reason="previously_rejected")
    elif statuses == {"approved"}:
        keep, drop = sorted(
            (a, b), key=lambda r: (evidence_weight(r), r["last_evidence_at"] or ""), reverse=True
        )
        plan = {"kind": "merge", "keep": keep["id"], "drop": drop["id"]}
        run.emit("merge", plan, state="proposed", reason="duplicate")


def _contradicts(run: Run, a: sqlite3.Row, b: sqlite3.Row) -> None:
    if "rejected" in (a["status"], b["status"]):
        return
    # Higher evidence weight wins; on a tie, the newer last_evidence_at wins.
    winner, loser = sorted(
        (a, b),
        key=lambda r: (round(evidence_weight(r), 9), r["last_evidence_at"] or "", r["seq"]),
        reverse=True,
    )
    state = "proposed" if loser["status"] == "approved" else "applied"
    plan = {"kind": "supersede", "winner": winner["id"], "loser": loser["id"]}
    run.emit("supersede", plan, state=state, reason="contradiction")
