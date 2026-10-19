# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hypothesis-loop statistics and host checks (SPEC §7.4, §13), no sandbox needed."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from wwyd.analyzers import authoring, hypothesis
from wwyd.analyzers.builtin.base import host_fields
from wwyd.analyzers.llm import fill_template, render
from wwyd.ingest.episodes import Episode
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.rare import RareWords

TZ = ZoneInfo("Europe/Berlin")


def test_binomial_tail_exact_values():
    assert math.isclose(hypothesis.binom_sf(10, 10, 0.5), 0.5**10)
    assert math.isclose(hypothesis.binom_sf(9, 10, 0.5), 11 / 1024)
    assert hypothesis.binom_sf(0, 10, 0.3) == 1.0
    assert hypothesis.binom_sf(11, 10, 0.3) == 0.0
    # Large n stays finite and tiny in log space.
    p = hypothesis.binom_sf(900, 1000, 0.5)
    assert 0 < p < 1e-100
    # Sum of the tail equals 1 - CDF computed directly for a small case.
    direct = sum(math.comb(20, k) * 0.3**k * 0.7 ** (20 - k) for k in range(12, 21))
    assert math.isclose(hypothesis.binom_sf(12, 20, 0.3), direct, rel_tol=1e-9)


def test_benjamini_hochberg():
    # m = 4, q = 0.1: thresholds 0.025, 0.05, 0.075, 0.1. Step-up: rank 3 (0.07) passes,
    # so ranks 1-3 are discoveries even though rank 2 alone would too.
    assert hypothesis.benjamini_hochberg([0.07, 0.01, 0.5, 0.04]) == [True, True, False, True]
    assert hypothesis.benjamini_hochberg([0.2, 0.3]) == [False, False]
    assert hypothesis.benjamini_hochberg([]) == []
    assert hypothesis.benjamini_hochberg([0.09], q=0.1) == [True]


def _items(n):
    base = datetime(2026, 1, 1, 9, 0, tzinfo=TZ)
    eps = [Episode(i, "c", "x", None, start=base + timedelta(days=n - i)) for i in range(n)]
    eps.append(Episode(n, "c", "x", None))  # untimed: left out
    return [host_fields(ep, TZ, {}) for ep in eps]


def test_split_70_30_by_time():
    train, hold = hypothesis.split(_items(10))
    assert len(train) == 7 and len(hold) == 3
    assert max(it.local_start for it in train) < min(it.local_start for it in hold)


def test_decide_thresholds_and_base_rate():
    T = hypothesis.Totals
    good = hypothesis.Outcome(T(40, 38), T(15, 14))
    small = hypothesis.Outcome(T(20, 20), T(10, 10))  # n < 30
    holdout_fails = hypothesis.Outcome(T(40, 38), T(15, 9))
    high_base = hypothesis.Outcome(T(40, 32, 100, 85), T(15, 13))  # 80% vs a base of 85%
    failed = hypothesis.Outcome(None, None)
    assert hypothesis.decide([good, small, holdout_fails, high_base, failed]) == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert high_base.train.base_rate == 0.85
    assert hypothesis.Totals(10, 9).base_rate == 0.5


def test_template_fill_and_render_windows():
    assert fill_template("Declines {share} ({n})", {"share": "80%", "n": "40"}) == (
        "Declines 80% (40)"
    )
    assert fill_template("Bad {name}", {"share": "1"}) is None
    state = {
        "n": {f"2025-{m:02d}": {"_": 10.0} for m in range(1, 13)},
        "hits": {f"2025-{m:02d}": {"_": 9.0} for m in range(1, 13)},
    }
    spec = render(state, "Declines {share} of {n}.", "habit")
    assert spec.text == "Declines 90% of 120." and spec.samples == 120
    assert len(spec.stats["n_by_month"]) == 12
    # An old month falls out of the 12-month window.
    state["n"]["2024-01"] = {"_": 500.0}
    assert render(state, "{n}", "habit").samples == 120
    # The last 3 months below the threshold weaken it even when the 12 months pass.
    for m in (10, 11, 12):
        state["hits"][f"2025-{m:02d}"] = {"_": 5.0}
    assert render(state, "{n}", "habit").text is None


def test_duplicate_statements_and_source_checks():
    assert authoring.is_duplicate(
        "Declines {share} of late meetings.", ["Declines {share} of the late meetings ({n})."]
    )
    assert not authoring.is_duplicate("Watches films to the end.", ["Declines late meetings."])
    eps = [
        Episode(
            i,
            "c",
            "x",
            None,
            start=datetime(2026, 1, 1 + i % 2, tzinfo=TZ),
            situation=f"call with Velmorine and mira {i}",
        )
        for i in range(6)
    ]
    guard = BatchGuard(other_texts=[e.situation for e in eps], all_texts=[e.situation for e in eps])
    rare = RareWords(eps)
    ok = authoring.Hypothesis("Declines {share}.", "habit", "import json\nprint(json.dumps({}))")
    assert authoring.check_hypothesis(ok, guard, rare) is None
    for src in ('x = "velmorine"\nprint(1)', "# about mira\nprint(1)", "mira_count = 1"):
        assert (
            authoring.check_hypothesis(
                authoring.Hypothesis("Declines {share}.", "habit", src), guard, rare
            )
            == "guard"
        )
    assert (
        authoring.check_hypothesis(
            authoring.Hypothesis("Calls mira {share}.", "habit", "print(1)"), guard, rare
        )
        == "guard"
    )
    assert (
        authoring.check_hypothesis(
            authoring.Hypothesis("Bad {placeholder}.", "habit", "print(1)"), guard, rare
        )
        == "invalid"
    )
