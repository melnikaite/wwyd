# SPDX-License-Identifier: AGPL-3.0-or-later
"""``time_gaps``: same-day gaps between consecutive non-reply intervals (SPEC §7.2).

Per local day the intervals are sorted by start; the gap is ``start - max(previous ends)``
(overlaps count as 0); gaps of 120 min or more are not "between" items. State: ``gap``
(``<edge>`` of 0, 5, 10, 15, 30, 60 min) and ``days`` (``DD``). Gaps are computed within
one job: two intervals of the same day that arrive in different jobs add no gap.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC

from wwyd.analyzers.builtin.base import (
    MIN_SHARE,
    Builtin,
    ClaimSpec,
    Context,
    Counter,
    Item,
    bucket,
    enough,
    hist,
    pct,
    period_of,
    usable_interval,
)
from wwyd.analyzers.contract import Output
from wwyd.analyzers.keyfilter import NUMERIC
from wwyd.analyzers.state import State

GAP_EDGES = (0, 5, 10, 15, 30, 60)
MAX_GAP = 120


def run(items: Sequence[Item], ctx: Context) -> Output:
    c = Counter()
    days: dict[object, list[Item]] = defaultdict(list)
    for it in items:
        if not it.episode.is_reply and usable_interval(it):
            days[it.local_start.date()].append(it)  # type: ignore[union-attr]
    for its in days.values():
        its.sort(key=lambda i: i.local_start.astimezone(UTC))  # type: ignore[union-attr]
        end = its[0].local_start.astimezone(UTC).timestamp() + its[0].duration_s  # type: ignore[union-attr,operator]
        for it in its[1:]:
            start = it.local_start.astimezone(UTC).timestamp()  # type: ignore[union-attr]
            gap = (start - end) / 60
            if gap < MAX_GAP:
                c.add("gap", it.month, str(GAP_EDGES[bucket(max(gap, 0), GAP_EDGES)]))  # type: ignore[arg-type]
                c.day(it)
            end = max(end, start + it.duration_s)  # type: ignore[operator]
    return c.output()


def render(state: State, ctx: Context) -> list[ClaimSpec]:
    h = hist(state, "gap", [str(e) for e in GAP_EDGES])
    n = sum(h)
    if n == 0:
        return []
    period = period_of(state)
    stats = {"n": n, "gap_minutes": h}
    spec = ClaimSpec("", "habit", None, 0.0, n, period, stats)
    if not enough(n, period):
        return [spec]
    for minutes in (15, 10, 5):
        hits = sum(h[GAP_EDGES.index(minutes) :])
        if hits / n >= MIN_SHARE:
            spec.share, stats["hits"] = hits / n, hits
            spec.text = (
                f"{ctx.scope_path}: keeps at least {minutes} min between items"
                f" ({pct(hits / n)}% of {n} same-day gaps)."
            )
            return [spec]
    spec.share, stats["hits"] = h[0] / n, h[0]
    if h[0] / n >= MIN_SHARE:
        spec.text = (
            f"{ctx.scope_path}: usually back to back ({pct(h[0] / n)}% of {n} gaps under 5 min)."
        )
    return [spec]


BUILTIN = Builtin(
    name="time_gaps",
    scope_kind="category",
    kind="habit",
    statement_template="{category}: minimum buffer between consecutive items",
    key_classes={"gap": (NUMERIC,), "days": (NUMERIC,)},
    run=run,
    render=render,
)
