# SPDX-License-Identifier: AGPL-3.0-or-later
"""``time_duration``: histograms of interval durations and response latency (SPEC §7.2).

Every interval ``when`` of 0–8 h (meetings, calls, blocks, whatever the episode class)
feeds ``length`` (``<minutes>`` rounded to 5, last bucket ``180`` = 180+). Every episode
with ``situation_at`` and a ``when`` at or after it feeds ``latency`` (``when`` −
``situation_at``, D23; ``<audience_id>.<1|0>.<edge>``, 1 = the situation arrived Mon–Fri
09:00–18:00 local time). ``days`` (``DD``). Facets: ``length`` and
``latency:<audience_id>``.
"""

from __future__ import annotations

from collections.abc import Sequence

from wwyd.analyzers.builtin.base import (
    MIN_SHARE,
    NO_AUDIENCE,
    Builtin,
    ClaimSpec,
    Context,
    Counter,
    Item,
    bucket,
    enough,
    pct,
    period_of,
    usable_interval,
)
from wwyd.analyzers.contract import Output
from wwyd.analyzers.keyfilter import NUMERIC, VOCAB
from wwyd.analyzers.state import State, totals

LENGTH_STEP = 5
LENGTH_MAX = 180
LENGTHS = tuple(str(m) for m in range(0, LENGTH_MAX + 1, LENGTH_STEP))
WORK_START, WORK_END = 9, 18
LATENCY_EDGES = (0, 5, 15, 60, 240, 1440)  # minutes; last bucket = more than a day
LATENCY_WORDS = {5: "5 min", 15: "15 min", 60: "an hour", 240: "4 hours", 1440: "a day"}
MAX_LATENCY_MINUTES = 14 * 24 * 60
MIN_SIDE = 10  # minimum responses in/out of work hours to describe that side


def run(items: Sequence[Item], ctx: Context) -> Output:
    c = Counter()
    for it in items:
        if it.month is None:
            continue
        counted = False
        arrived = it.local_situation_at
        if it.latency_s is not None and arrived is not None and it.audience_id != NO_AUDIENCE:
            minutes = it.latency_s / 60
            if minutes <= MAX_LATENCY_MINUTES:
                work = int(arrived.weekday() < 5 and WORK_START <= arrived.hour < WORK_END)
                edge = LATENCY_EDGES[bucket(minutes, LATENCY_EDGES)]
                c.add("latency", it.month, f"{it.audience_id}.{work}.{edge}")
                counted = True
        if usable_interval(it):
            minutes = it.duration_s / 60  # type: ignore[operator]
            rounded = min(round(minutes / LENGTH_STEP) * LENGTH_STEP, LENGTH_MAX)
            c.add("length", it.month, str(rounded))
            counted = True
        if counted:
            c.day(it)
    return c.output()


def _length_label(minutes: str) -> str:
    return f"{minutes}+" if minutes == str(LENGTH_MAX) else minutes


def _render_length(state: State, period, ctx: Context) -> ClaimSpec | None:  # noqa: ANN001
    t = totals(state, "length")
    hist = [int(t.get(m, 0)) for m in LENGTHS]
    n = sum(hist)
    if n == 0:
        return None
    stats = {"n": n, "minutes_5": hist}
    spec = ClaimSpec("length", "habit", None, 0.0, n, period, stats)
    if not enough(n, period):
        return spec
    top = sorted(range(len(hist)), key=lambda i: (-hist[i], i))[:2]
    share = hist[top[0]] / n
    if share >= MIN_SHARE:
        spec.share, stats["hits"] = share, hist[top[0]]
        spec.text = (
            f"{ctx.scope_path}: typically {_length_label(LENGTHS[top[0]])} min"
            f" ({pct(share)}% of {n})."
        )
        return spec
    hits = hist[top[0]] + hist[top[1]]
    spec.share, stats["hits"] = hits / n, hits
    if hits / n >= MIN_SHARE:
        a, b = sorted(top)
        spec.text = (
            f"{ctx.scope_path}: typically {_length_label(LENGTHS[a])} or"
            f" {_length_label(LENGTHS[b])} min ({pct(hits / n)}% of {n})."
        )
    return spec


def _within(hist: list[int]) -> tuple[str, float, int] | None:
    n = sum(hist)
    cum = 0
    for k in range(1, len(LATENCY_EDGES)):
        cum += hist[k - 1]
        if cum / n >= MIN_SHARE:
            return LATENCY_WORDS[LATENCY_EDGES[k]], cum / n, cum
    return None


def _render_latency(state: State, aud: str, period, ctx: Context) -> ClaimSpec:  # noqa: ANN001
    t = totals(state, "latency")
    inside = [int(t.get(f"{aud}.1.{e}", 0)) for e in LATENCY_EDGES]
    outside = [int(t.get(f"{aud}.0.{e}", 0)) for e in LATENCY_EDGES]
    n = sum(inside) + sum(outside)
    stats = {"n": n, "in_hours": inside, "off_hours": outside}
    spec = ClaimSpec(f"latency:{aud}", "style", None, 0.0, n, period, stats, audience_id=aud)
    if not enough(n, period):
        return spec
    a = _within(inside) if sum(inside) >= MIN_SIDE else None
    b = _within(outside) if sum(outside) >= MIN_SIDE else None
    p = ctx.scope_path
    if a and b:
        if a[0] == b[0]:
            text = f"{p}: responds within {a[0]}, in and out of work hours"
        else:
            text = f"{p}: responds within {a[0]} in work hours, within {b[0]} otherwise"
        share, hits = min(a[1], b[1]), a[2] + b[2]
    elif a:
        text, share, hits = f"{p}: responds within {a[0]} in work hours", a[1], a[2]
    elif b:
        text, share, hits = f"{p}: responds within {b[0]} outside work hours", b[1], b[2]
    else:
        return spec
    spec.share, stats["hits"] = share, hits
    spec.text = f"{text} ({pct(share)}% of {n} responses)."
    return spec


def render(state: State, ctx: Context) -> list[ClaimSpec]:
    period = period_of(state)
    out: list[ClaimSpec] = []
    if (length := _render_length(state, period, ctx)) is not None:
        out.append(length)
    audiences = sorted({k.split(".", 1)[0] for k in totals(state, "latency")})
    out.extend(_render_latency(state, aud, period, ctx) for aud in audiences)
    return out


BUILTIN = Builtin(
    name="time_duration",
    scope_kind="category",
    kind="habit",
    statement_template="{category}: typical interval lengths; response latency per audience",
    key_classes={
        "length": (NUMERIC,),
        "latency": (VOCAB, NUMERIC, NUMERIC),
        "days": (NUMERIC,),
    },
    run=run,
    render=render,
)
