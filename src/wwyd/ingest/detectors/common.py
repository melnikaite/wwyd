# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pieces of the habit detectors (SPEC §7.2).

A detector is split in two pure functions:

* ``collect(batch, tz) -> list[Finding]`` turns one batch into fixed-size sufficient
  statistics (histograms and counters);
* ``render(stats, period_days) -> Rendered | None`` turns statistics, possibly merged across
  many runs by adding buckets, into the claim text and numbers, or ``None`` below threshold.

Only the statistics are stored (in ``evidence.stats``), never the records.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

MIN_SAMPLES = 30
MIN_SHARE = 0.75
MIN_PERIOD_DAYS = 21

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

Stats = dict[str, list[int]]


@dataclass
class Finding:
    key: str
    kind: str
    category: str
    stats: Stats
    sources: dict[str, int]
    first: date
    last: date
    audience: str | None = None  # set for per-audience keys

    @property
    def period(self) -> str:
        return f"{self.first.isoformat()}..{self.last.isoformat()}"


@dataclass
class Rendered:
    text: str
    share: float
    samples: int
    confidence: float = field(init=False)

    def __post_init__(self) -> None:
        self.confidence = confidence(self.share, self.samples)


Renderer = Callable[[Stats, int], "Rendered | None"]


def confidence(share: float, samples: int) -> float:
    """Grows with the share and, up to 150 samples, with the sample size; capped at 0.95."""
    return round(min(0.95, 0.35 + 0.6 * share * min(1.0, samples / 150)), 2)


def bucket(value: float, edges: Sequence[float]) -> int:
    """Index of the bucket ``[edges[i], edges[i+1])``; below ``edges[0]`` maps to 0 and
    ``>= edges[-1]`` to the last (open) bucket ``len(edges) - 1``."""
    idx = 0
    for i, edge in enumerate(edges):
        if value >= edge:
            idx = i
    return idx


def add_stats(old: Stats, new: Stats) -> Stats:
    """Merge two runs by adding buckets; a histogram whose shape changed is replaced."""
    out = {k: list(v) for k, v in old.items()}
    for name, values in new.items():
        prev = out.get(name)
        if isinstance(prev, list) and len(prev) == len(values):
            out[name] = [a + b for a, b in zip(prev, values, strict=True)]
        else:
            out[name] = list(values)
    return out


def pct(share: float) -> int:
    return int(share * 100 + 0.5)


def join_words(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def period_days(period: str | None) -> int:
    if not period:
        return 0
    start, end = period.split("..")
    first = date.fromisoformat(start if len(start) == 10 else f"{start}-01")
    last = date.fromisoformat(end if len(end) == 10 else f"{end}-28")
    return (last - first).days + 1


def date_span(days: Sequence[date]) -> tuple[date, date]:
    return min(days), max(days)
