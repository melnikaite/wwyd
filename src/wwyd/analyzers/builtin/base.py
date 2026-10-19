# SPDX-License-Identifier: AGPL-3.0-or-later
"""The builtin analyzer interface and shared helpers (SPEC §7.2; formulas in docs/habits.md).

A builtin is shipped code with two pure functions:

* ``run(items, ctx) -> Output`` turns the processed episodes of one scope (with host-computed
  fields) into mergeable counters in the output contract;
* ``render(state, ctx) -> list[ClaimSpec]`` turns the accumulated state into one spec per
  facet; a spec with ``text=None`` is below threshold (an existing claim weakens).

Rendered texts hold only numbers, weekday/hour words and the scope's category path.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo

from wwyd.analyzers.contract import Output
from wwyd.analyzers.state import State, totals
from wwyd.ingest.episodes import Episode

MIN_SAMPLES = 30
MIN_SHARE = 0.75
MIN_PERIOD_DAYS = 21
MAX_INTERVAL_MINUTES = 8 * 60  # longer intervals (all-day, travel blocks) are ignored

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
NO_AUDIENCE = "_"


@dataclass(frozen=True)
class Item:
    """An episode plus the host-computed fields of SPEC §7.2."""

    episode: Episode
    local_start: datetime | None
    weekday: int | None  # 0 = Monday
    hour: int | None
    month: str | None  # YYYY-MM
    day: str | None  # DD
    duration_s: float | None  # wall time on UTC instants: correct across DST
    audience_id: str  # audience ID, or "_" when the episode named none


def _local(dt: datetime, tz: tzinfo) -> datetime:
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def host_fields(ep: Episode, tz: tzinfo, audience_ids: Mapping[str, str]) -> Item:
    aud = audience_ids.get(ep.audience, NO_AUDIENCE) if ep.audience else NO_AUDIENCE
    if ep.start is None:
        return Item(ep, None, None, None, None, None, None, aud)
    start = _local(ep.start, tz)
    duration = None
    if ep.end is not None:
        end = _local(ep.end, tz)
        duration = (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()
    return Item(
        ep,
        start,
        start.weekday(),
        start.hour,
        f"{start.year:04d}-{start.month:02d}",
        f"{start.day:02d}",
        duration,
        aud,
    )


@dataclass
class Context:
    analyzer_id: str
    scope_kind: str
    scope_id: str
    scope_path: str  # category path (category scope) or audience name
    tz: tzinfo
    audience_ids: Mapping[str, str] = field(default_factory=dict)  # name -> id
    # Read-only access for renders that compare scopes (stylometry keyness); may be None.
    conn: sqlite3.Connection | None = None


@dataclass
class ClaimSpec:
    facet: str  # "" for a builtin with a single statement
    kind: str
    text: str | None  # None: below threshold
    share: float
    samples: int
    period: tuple[date, date] | None
    stats: dict[str, float | list[float]]
    audience_id: str | None = None  # the claim applies to this audience only

    @property
    def confidence(self) -> float:
        return confidence(self.share, self.samples)


@dataclass(frozen=True)
class Builtin:
    name: str
    scope_kind: str  # "category" or "audience"
    kind: str  # claim kind of the main statement (stored on the analyzer row)
    statement_template: str
    key_classes: Mapping[str, Sequence[str]]
    run: Callable[[Sequence[Item], Context], Output]
    render: Callable[[State, Context], list[ClaimSpec]]
    # Category of the claims of an audience-scoped builtin (category scope: the scope itself).
    claim_category: str | None = None
    # Episode fields whose text statistics back ``text`` keys (SPEC §7.2; stylometry: said).
    text_fields: Sequence[str] = ("said",)


# --- helpers ----------------------------------------------------------------------------


def confidence(share: float, samples: int) -> float:
    """Grows with the share and, up to 150 samples, with the sample size; capped at 0.95."""
    return round(min(0.95, 0.35 + 0.6 * share * min(1.0, samples / 150)), 2)


def bucket(value: float, edges: Sequence[float]) -> int:
    """Index of ``[edges[i], edges[i+1])``; below edges[0] -> 0, >= edges[-1] -> last."""
    idx = 0
    for i, edge in enumerate(edges):
        if value >= edge:
            idx = i
    return idx


def pct(share: float) -> int:
    return int(share * 100 + 0.5)


def join_words(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


class Counter:
    """Builds an output: ``add(metric, month, key, n)``."""

    def __init__(self) -> None:
        self._out: dict[str, dict[str, dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(float))
        )

    def add(self, metric: str, month: str, key: str, n: float = 1) -> None:
        self._out[metric][month][key] += n

    def day(self, item: Item) -> None:
        assert item.month is not None and item.day is not None
        self.add("days", item.month, item.day)

    def output(self) -> Output:
        return {m: {mo: dict(k) for mo, k in per.items()} for m, per in self._out.items()}


def period_of(state: State) -> tuple[date, date] | None:
    """First and last sampled day, from the ``days`` metric (``DD`` keys per month)."""
    days = [
        date(int(month[:4]), int(month[5:]), int(day))
        for month, keys in state.get("days", {}).items()
        for day, value in keys.items()
        if value > 0
    ]
    return (min(days), max(days)) if days else None


def period_days(period: tuple[date, date] | None) -> int:
    return 0 if period is None else (period[1] - period[0]).days + 1


def enough(n: int, period: tuple[date, date] | None) -> bool:
    return n >= MIN_SAMPLES and period_days(period) >= MIN_PERIOD_DAYS


def hist(state: State, metric: str, labels: Sequence[str], prefix: str = "") -> list[int]:
    """A metric as a list over ``labels`` (keys ``prefix + label``), summed over months."""
    t = totals(state, metric)
    return [int(t.get(prefix + label, 0)) for label in labels]


def usable_interval(item: Item) -> bool:
    return (
        item.duration_s is not None
        and item.month is not None
        and 0 < item.duration_s / 60 <= MAX_INTERVAL_MINUTES
    )
