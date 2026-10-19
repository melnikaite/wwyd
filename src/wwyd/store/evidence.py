# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claim weights over the weighted evidence sources (SPEC §4.5).

Every source class counts once per episode, except ``correction`` (an episode correcting a
prediction, D23), which counts ``correction_weight`` times.

* :func:`evidence_weight` = ``confidence × log(1 + Σ weighted)``: contradictions and the
  size budget (§10).
* :func:`context_rank` = ``confidence × (1 + log10(1 + Σ weighted))``: the ordering of the
  predict context and get_persona (a claim without sources still ranks by confidence)."""

from __future__ import annotations

import json
import math
from typing import Any

from wwyd.config import thresholds


def weighted_sources(sources: Any) -> float:
    if not isinstance(sources, dict):
        return 0.0
    factor = thresholds().correction_weight
    total = 0.0
    for name, n in sources.items():
        if isinstance(n, bool) or not isinstance(n, int | float) or n <= 0:
            continue
        total += n * (factor if name == "correction" else 1)
    return total


def _sources(evidence: str | dict[str, Any] | None) -> Any:
    try:
        data = json.loads(evidence or "{}") if not isinstance(evidence, dict) else evidence
    except ValueError:
        return None
    return data.get("sources") if isinstance(data, dict) else None


def evidence_weight(confidence: float, evidence: str | dict[str, Any] | None) -> float:
    """``confidence × log(1 + Σ weighted sources)``; ``evidence`` as JSON text or dict."""
    return (confidence or 0.0) * math.log1p(weighted_sources(_sources(evidence)))


def context_rank(confidence: float, evidence: str | dict[str, Any] | None) -> float:
    """``confidence × (1 + log10(1 + Σ weighted sources))``."""
    return (confidence or 0.0) * (1 + math.log10(1 + weighted_sources(_sources(evidence))))
