# SPDX-License-Identifier: AGPL-3.0-or-later
"""``time_activity``: weekday x hour histogram of a category's episodes (SPEC §7.2).

State: ``wd_hour`` (``<weekday>.<HH>``), ``audience_weekday`` (``<audience_id>.<weekday>``)
and ``days`` (``DD``). Facets: ``hours`` (active window on weekdays), ``slots`` (rare weekday
half-days, the old ``meeting_free_slots``), ``weekends``.
"""

from __future__ import annotations

from collections.abc import Sequence

from wwyd.analyzers.builtin.base import (
    MIN_SHARE,
    WEEKDAY_KEYS,
    WEEKDAYS,
    Builtin,
    ClaimSpec,
    Context,
    Counter,
    Item,
    enough,
    join_words,
    pct,
    period_of,
)
from wwyd.analyzers.contract import Output
from wwyd.analyzers.keyfilter import NUMERIC, VOCAB
from wwyd.analyzers.state import State, totals

ACTIVE_TRIM = 0.1  # the active window drops the earliest and latest 10% of weekday activity
RARE_DAY = 0.15  # weekend days are "rare" below 15% of the average weekday's activity
AFTERNOON_HOUR = 13
MIN_SLOT_MEAN = 3
HOURS = tuple(f"{h:02d}" for h in range(24))


def run(items: Sequence[Item], ctx: Context) -> Output:
    c = Counter()
    for it in items:
        if it.local_start is None or it.month is None or it.weekday is None:
            continue
        wd = WEEKDAY_KEYS[it.weekday]
        c.add("wd_hour", it.month, f"{wd}.{it.hour:02d}")
        c.add("audience_weekday", it.month, f"{it.audience_id}.{wd}")
        c.day(it)
    return c.output()


def _grid(state: State) -> list[list[int]]:
    t = totals(state, "wd_hour")
    return [[int(t.get(f"{wd}.{h}", 0)) for h in HOURS] for wd in WEEKDAY_KEYS]


def _render_hours(grid: list[list[int]], n: int, period, ctx: Context) -> ClaimSpec:  # noqa: ANN001
    weekdays = [sum(grid[d][h] for d in range(5)) for h in range(24)]
    total = sum(weekdays)
    stats = {"n": total, "hours": weekdays}
    spec = ClaimSpec("hours", "habit", None, 0.0, total, period, stats)
    if not enough(total, period):
        return spec
    cum, start, end = 0, 0, 24
    found_start = False
    for h, count in enumerate(weekdays):
        cum += count
        if not found_start and cum > ACTIVE_TRIM * total:
            start, found_start = h, True
        if cum >= (1 - ACTIVE_TRIM) * total:
            end = h + 1
            break
    share = sum(weekdays[start:end]) / total
    spec.share = share
    stats["hits"] = sum(weekdays[start:end])
    if share >= MIN_SHARE:
        spec.text = (
            f"{ctx.scope_path}: active {start:02d}:00–{end:02d}:00 on weekdays"
            f" ({pct(share)}% of {total} weekday episodes)."
        )
    return spec


def _slot_name(i: int) -> str:
    return f"{WEEKDAYS[i // 2]} {'afternoons' if i % 2 else 'mornings'}"


def _render_slots(grid: list[list[int]], n: int, period, ctx: Context) -> ClaimSpec:  # noqa: ANN001
    slots = [
        sum(grid[d][h] for h in range(24) if (h >= AFTERNOON_HOUR) == bool(half))
        for d in range(5)
        for half in (0, 1)
    ]
    stats = {"n": n, "halfdays": slots}
    spec = ClaimSpec("slots", "habit", None, 0.0, n, period, stats)
    if not enough(n, period):
        return spec
    mean = sum(slots) / 10
    if mean < MIN_SLOT_MEAN:
        return spec
    free = sorted(
        (i for i in range(10) if slots[i] <= (1 - MIN_SHARE) * mean),
        key=lambda i: (slots[i], i),
    )[:2]
    if not free:
        return spec
    free.sort()
    spec.share = min(1 - slots[i] / mean for i in free)
    seen = sum(slots[i] for i in free)
    stats["hits"] = seen
    names = join_words([_slot_name(i) for i in free])
    if seen == 0:
        spec.text = f"{ctx.scope_path}: none on {names} (0 of {n} episodes)."
    else:
        spec.text = f"{ctx.scope_path}: rarely on {names} ({seen} of {n} episodes)."
    return spec


def _render_weekends(grid: list[list[int]], n: int, period, ctx: Context) -> ClaimSpec:  # noqa: ANN001
    per_day = [sum(row) for row in grid]
    weekend = per_day[5] + per_day[6]
    stats = {"n": n, "hits": weekend, "weekdays": per_day}
    spec = ClaimSpec("weekends", "habit", None, 0.0, n, period, stats)
    if not enough(n, period):
        return spec
    weekday_avg = sum(per_day[:5]) / 5
    if weekday_avg <= 0:
        return spec
    ratio = (weekend / 2) / weekday_avg
    spec.share = max(0.0, 1 - ratio)
    if weekend == 0:
        spec.text = f"{ctx.scope_path}: none on weekends (0 of {n} episodes)."
    elif ratio <= RARE_DAY:
        spec.text = f"{ctx.scope_path}: rarely on weekends ({weekend} of {n} episodes)."
    return spec


def render(state: State, ctx: Context) -> list[ClaimSpec]:
    grid = _grid(state)
    n = sum(map(sum, grid))
    if n == 0:
        return []
    period = period_of(state)
    return [
        _render_hours(grid, n, period, ctx),
        _render_slots(grid, n, period, ctx),
        _render_weekends(grid, n, period, ctx),
    ]


BUILTIN = Builtin(
    name="time_activity",
    scope_kind="category",
    kind="habit",
    statement_template="{category}: active hours on weekdays, rare half-days, weekends",
    key_classes={
        "wd_hour": (VOCAB, NUMERIC),
        "audience_weekday": (VOCAB, VOCAB),
        "days": (NUMERIC,),
    },
    run=run,
    render=render,
)
