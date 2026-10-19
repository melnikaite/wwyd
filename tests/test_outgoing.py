# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outgoing messages: ``said`` + explicit ``audience`` without ``situation`` is a reply to
that audience, not a statement about self (SPEC §6.1, §7.1)."""

from __future__ import annotations

from datetime import UTC

import pytest

from wwyd.analyzers.builtin import stylometry
from wwyd.analyzers.builtin.base import host_fields
from wwyd.ingest import passes
from wwyd.ingest.episodes import parse_batch


def batch(records):
    return parse_batch(
        records,
        tz=UTC,
        audiences={"colleagues", "everyone"},
        default_audience="everyone",
        normalize_category=lambda p: p,
    )


@pytest.mark.parametrize(
    ("rec", "classes"),
    [
        ({"said": "I never eat breakfast."}, ("statement",)),
        ({"said": "late, sorry", "audience": "colleagues"}, ("reply",)),
        ({"said": "late, sorry", "audience": "default"}, ("reply",)),
        ({"said": "late, sorry", "audience": "strangers"}, ("reply",)),  # unknown -> default
        ({"situation": "q?", "said": "a"}, ("reply",)),
        ({"situation": "q?", "said": "a", "audience": "colleagues"}, ("reply",)),
        ({"said": "x", "decision": "d"}, ("statement", "decision")),
        ({"said": "x", "decision": "d", "audience": "colleagues"}, ("reply", "decision")),
        ({"situation": "q?", "said": "x", "decision": "d"}, ("reply", "decision")),
        ({"decision": "d", "audience": "colleagues"}, ("decision",)),
    ],
)
def test_classes(rec, classes):
    assert batch([rec]).episodes[0].classes == classes


def test_default_and_unknown_resolve_to_default_audience():
    b = batch([{"said": "a", "audience": "default"}, {"said": "a", "audience": "strangers"}])
    assert [ep.audience for ep in b.episodes] == ["everyone", "everyone"]
    assert all(ep.is_outgoing for ep in b.episodes)
    assert b.unknown_audiences == 1
    assert b.counts()["reply"] == 2 and b.counts()["statement"] == 0


def _calls(records):
    b = batch(records)
    for ep in b.episodes:
        ep.category = "communication/style"
    return passes.build_calls(
        b, {"communication/style": "how the owner writes"}, default_audience="everyone",
        decision_min_episodes=100,
    )  # fmt: skip


def test_outgoing_goes_to_replies_for_its_audience():
    calls = _calls([{"said": "running late, sorry", "audience": "colleagues"}])
    assert [(c.purpose, c.audiences) for c in calls] == [("replies", ["colleagues"])]
    user = calls[0].user
    assert "OWNER: running late, sorry" in user
    assert "OTHER" not in user
    assert "message the owner started" in passes.REPLIES_SYSTEM


def test_said_alone_stays_a_statement():
    calls = _calls([{"said": "I never eat breakfast."}])
    assert [(c.purpose, c.audiences) for c in calls] == [("statements", "all")]


def test_stylometry_sees_outgoing():
    b = batch([{"said": "Running late, sorry!", "audience": "colleagues",
                "when": "2026-03-02T09:00:00Z"}])  # fmt: skip
    items = [host_fields(ep, UTC, {"colleagues": "aud_x"}) for ep in b.episodes]
    assert items[0].audience_id == "aud_x"
    out = stylometry.run(items, None)
    assert out["n"]
