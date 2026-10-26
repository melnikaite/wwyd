# SPDX-License-Identifier: AGPL-3.0-or-later
"""Code counts what a small model miscounts (SPEC §7.6): the decisions pass names the
items a rule follows and code counts them."""

from __future__ import annotations

from datetime import UTC

from wwyd.ingest import passes
from wwyd.ingest.episodes import parse_batch

CATS = {"hobbies/beekeeping": "bees"}


def _batch(records):
    b = parse_batch(
        records,
        tz=UTC,
        audiences={"friends", "everyone"},
        default_audience="everyone",
        normalize_category=lambda p: p,
    )
    for ep in b.episodes:
        ep.category = "hobbies/beekeeping"
    return b


def _decisions_call():
    b = _batch([
        {"when": "2026-01-05T10:00:00Z", "situation": "Swarm call", "decision": "went"},
        {"when": "2026-03-05T10:00:00Z", "situation": "Hive check", "decision": "went"},
        {"when": "2026-05-05T10:00:00Z", "situation": "Honey fair", "decision": "skipped"},
    ])  # fmt: skip
    (call,) = passes.build_calls(b, CATS, default_audience="everyone", decision_min_episodes=100)
    assert call.purpose == "decisions" and len(call.item_stamps) == 3
    return call, passes.guard_for(b, set())


def _claim(items, kind="decision_rule"):
    return {"kind": kind, "category": "hobbies/beekeeping", "proposed_category": None,
            "text": "Goes whenever the bees need it.", "confidence": 0.9,
            "used_items": items}  # fmt: skip


def test_decisions_support_counted_by_code():
    call, guard = _decisions_call()
    assert "used_items" in passes.DECISIONS_SCHEMA["properties"]["claims"]["items"]["required"]
    counts = passes.DeriveCounts()
    (rule,) = passes.to_candidates(call, {"claims": [_claim([1, 2, 2, 9, True])]}, guard, counts)
    assert rule.kind == "decision_rule" and rule.sources == {"decision": 2}
    assert rule.period == "2026-01..2026-03"
    (single,) = passes.to_candidates(call, {"claims": [_claim([3])]}, guard, counts)
    assert single.kind == "preference"
    assert single.confidence <= passes.SINGLE_DECISION_CONFIDENCE
    assert single.period == "2026-05..2026-05" and single.sources == {"decision": 1}
    # A count instead of item numbers is not trusted.
    (bad,) = passes.to_candidates(call, {"claims": [_claim(3)]}, guard, counts)
    assert bad.kind == "preference" and bad.sources == {"decision": 3}
    assert bad.period == call.period
