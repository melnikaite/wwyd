# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hypothesis test of LLM analyzers (SPEC §7.4 step 3, D20).

Each hypothesis (an analyzer source) runs in the sandbox on the older 70% (by time) of the
category's processed, timed episodes and on the recent 30% holdout; test runs write no
state. It is accepted when, on the older part, ``n >= 30`` and ``hits / n >= 0.75``, a
one-sided exact binomial test of ``hits`` against the base rate (``base_hits / base_n``
when the analyzer reports them, else 0.5) passes after Benjamini–Hochberg at ``q = 0.1``
across the round's hypotheses, and the holdout share is at least 0.75 too (with at least
one holdout sample).

The statistics are pure Python: the binomial tail is summed in log space (``lgamma``), so
it stays exact and finite for the sample sizes a batch can have (≤ 1000 episodes).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

MIN_N = 30
MIN_SHARE = 0.75
Q = 0.1
TRAIN_SHARE = 0.7
DEFAULT_BASE = 0.5


def _log_pmf(k: int, n: int, p: float) -> float:
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p): the one-sided p-value of ``k`` hits in ``n``."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    logs = [_log_pmf(i, n, p) for i in range(k, n + 1)]
    top = max(logs)
    return min(1.0, math.exp(top) * sum(math.exp(x - top) for x in logs))


def benjamini_hochberg(pvalues: Sequence[float], q: float = Q) -> list[bool]:
    """Which hypotheses are discoveries at false-discovery rate ``q`` (step-up procedure)."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    cutoff = -1
    for rank, i in enumerate(order, start=1):
        if pvalues[i] <= q * rank / m:
            cutoff = rank
    passed = [False] * m
    for rank, i in enumerate(order, start=1):
        if rank <= cutoff:
            passed[i] = True
    return passed


def split(items: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    """Older 70% and recent 30% by time (items with ``local_start``; untimed ones are left
    out, they cannot be counted per month)."""
    timed = sorted(
        (it for it in items if it.local_start is not None),
        key=lambda it: (it.local_start, it.episode.index),
    )
    cut = int(len(timed) * TRAIN_SHARE)
    return timed[:cut], timed[cut:]


@dataclass
class Totals:
    n: float = 0.0
    hits: float = 0.0
    base_n: float = 0.0
    base_hits: float = 0.0

    @property
    def share(self) -> float:
        return self.hits / self.n if self.n > 0 else 0.0

    @property
    def base_rate(self) -> float:
        if self.base_n > 0:
            return min(1.0, max(0.0, self.base_hits / self.base_n))
        return DEFAULT_BASE


def totals(output: dict[str, dict[str, dict[str, float]]]) -> Totals:
    """Sum the key ``_`` of each metric over the months of a validated output."""
    out = Totals()
    for metric in ("n", "hits", "base_n", "base_hits"):
        value = sum(keys.get("_", 0.0) for keys in output.get(metric, {}).values())
        setattr(out, metric, max(0.0, value))
    out.hits = min(out.hits, out.n)
    out.base_hits = min(out.base_hits, out.base_n)
    return out


@dataclass
class Outcome:
    train: Totals | None  # None: the run failed
    holdout: Totals | None

    @property
    def pvalue(self) -> float:
        if self.train is None or self.train.n <= 0:
            return 1.0
        return binom_sf(int(round(self.train.hits)), int(round(self.train.n)), self.train.base_rate)

    def basic_pass(self) -> bool:
        t, h = self.train, self.holdout
        return (
            t is not None
            and h is not None
            and t.n >= MIN_N
            and t.share >= MIN_SHARE
            and h.n >= 1
            and h.share >= MIN_SHARE
        )


def decide(outcomes: Sequence[Outcome], q: float = Q) -> list[bool]:
    """Accept or reject every hypothesis of one round (BH over the whole round)."""
    bh = benjamini_hochberg([o.pvalue for o in outcomes], q)
    return [o.basic_pass() and ok for o, ok in zip(outcomes, bh, strict=True)]
