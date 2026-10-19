# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calendar detectors: meeting_buffer, meeting_length, meeting_free_slots, accept_buffer,
accept_size (SPEC §7.2; formulas in docs/habits.md).

Only start/end, attendee counts and responses exist in the input (P3). Times are converted
to the persona's zone before anything is bucketed by day or hour; durations and gaps are
computed on aware datetimes, so DST changes never distort them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, tzinfo

from wwyd.ingest.detectors.common import (
    MIN_PERIOD_DAYS,
    MIN_SAMPLES,
    MIN_SHARE,
    WEEKDAYS,
    Finding,
    Rendered,
    Stats,
    bucket,
    date_span,
    join_words,
    pct,
)
from wwyd.ingest.records import Batch, Event

ATTENDING = frozenset({"accepted", "organizer"})
INVITES = frozenset({"accepted", "tentative", "declined"})
MAX_EVENT_MINUTES = 8 * 60  # longer events (all-day, travel blocks) are ignored

GAP_EDGES = (0, 5, 10, 15, 30, 60)  # meeting_buffer, gaps >= 120 min are not "between calls"
MAX_BUFFER_GAP = 120
LENGTH_STEP = 5  # meeting_length: duration rounded to 5 min, last bucket 180+
LENGTH_BUCKETS = 37
INVITE_GAP_EDGES = (0, 5, 10, 15, 30)  # accept_buffer
SIZE_EDGES = (0, 3, 5, 9, 16)  # accept_size: <=2, 3-4, 5-8, 9-15, 16+
SIZE_LABELS = (2, 4, 8, 15)
AFTERNOON_HOUR = 13
MIN_CONTRAST_SAMPLES = 10


def _minutes(td) -> float:  # noqa: ANN001
    return td.total_seconds() / 60


def _usable(e: Event) -> bool:
    return 0 < _minutes(e.end - e.start) <= MAX_EVENT_MINUTES


def _by_local_day(events: list[Event], tz: tzinfo) -> dict[date, list[Event]]:
    days: dict[date, list[Event]] = defaultdict(list)
    for e in events:
        days[e.start.astimezone(tz).date()].append(e)
    for evs in days.values():
        evs.sort(key=lambda e: e.start)
    return days


def collect(batch: Batch, tz: tzinfo) -> list[Finding]:
    events = [e for e in batch.events if _usable(e)]
    if not events:
        return []
    attending = [e for e in events if e.response in ATTENDING]
    invites = [e for e in events if e.response in INVITES]
    findings: list[Finding] = []

    def span(evs: list[Event]) -> tuple[date, date]:
        return date_span([e.start.astimezone(tz).date() for e in evs])

    if attending:
        first, last = span(attending)
        att_days = _by_local_day(attending, tz)

        gaps = [0] * len(GAP_EDGES)
        for evs in att_days.values():
            end = evs[0].end
            for e in evs[1:]:
                gap = _minutes(e.start - end)
                if gap < MAX_BUFFER_GAP:
                    gaps[bucket(max(gap, 0), GAP_EDGES)] += 1
                end = max(end, e.end)
        findings.append(
            Finding(
                "habit:meeting_buffer",
                "habit",
                "work/meetings",
                {"gap_minutes_hist": gaps},
                {"calendar_event": len(attending)},
                first,
                last,
            )
        )

        lengths = [0] * LENGTH_BUCKETS
        for e in attending:
            lengths[min(round(_minutes(e.end - e.start) / LENGTH_STEP), LENGTH_BUCKETS - 1)] += 1
        findings.append(
            Finding(
                "habit:meeting_length",
                "habit",
                "work/meetings",
                {"duration_5min_hist": lengths},
                {"calendar_event": len(attending)},
                first,
                last,
            )
        )

        slots = [0] * 14
        for e in attending:
            local = e.start.astimezone(tz)
            slots[local.weekday() * 2 + (local.hour >= AFTERNOON_HOUR)] += 1
        findings.append(
            Finding(
                "habit:meeting_free_slots",
                "habit",
                "work/meetings",
                {"weekday_halfday_hist": slots},
                {"calendar_event": len(attending)},
                first,
                last,
            )
        )

    if invites:
        first, last = span(invites)
        att_days = _by_local_day(attending, tz)
        declined = [0] * len(INVITE_GAP_EDGES)
        other = [0] * len(INVITE_GAP_EDGES)
        used = 0
        for c in invites:
            gap = _gap_to_neighbours(c, att_days.get(c.start.astimezone(tz).date(), []))
            if gap is None:
                continue
            used += 1
            hist = declined if c.response == "declined" else other
            hist[bucket(max(gap, 0), INVITE_GAP_EDGES)] += 1
        if used:
            findings.append(
                Finding(
                    "rule:accept_buffer",
                    "decision_rule",
                    "work/meetings",
                    {"declined_gap_hist": declined, "other_gap_hist": other},
                    {"calendar_event": used},
                    first,
                    last,
                )
            )

        dec_size = [0] * len(SIZE_EDGES)
        oth_size = [0] * len(SIZE_EDGES)
        for c in invites:
            hist = dec_size if c.response == "declined" else oth_size
            hist[bucket(c.attendees, SIZE_EDGES)] += 1
        findings.append(
            Finding(
                "rule:accept_size",
                "decision_rule",
                "work/meetings",
                {"declined_by_size": dec_size, "other_by_size": oth_size},
                {"calendar_event": len(invites)},
                first,
                last,
            )
        )
    return findings


def _gap_to_neighbours(c: Event, same_day: list[Event]) -> float | None:
    """Smallest buffer (minutes) the invite would leave to an attended event that day."""
    best: float | None = None
    for a in same_day:
        if a is c:
            continue
        if a.end <= c.start:
            gap = _minutes(c.start - a.end)
        elif a.start >= c.end:
            gap = _minutes(a.start - c.end)
        else:
            gap = 0.0  # overlap
        best = gap if best is None else min(best, gap)
    return best


# --- renderers -----------------------------------------------------------------------------


def _enough(n: int, days: int) -> bool:
    return n >= MIN_SAMPLES and days >= MIN_PERIOD_DAYS


def render_meeting_buffer(stats: Stats, days: int) -> Rendered | None:
    hist = stats["gap_minutes_hist"]
    n = sum(hist)
    if not _enough(n, days):
        return None
    for minutes in (15, 10, 5):
        share = sum(hist[GAP_EDGES.index(minutes) :]) / n
        if share >= MIN_SHARE:
            return Rendered(
                f"Keeps at least {minutes} min between calls ({pct(share)}% of {n} same-day gaps).",
                share,
                n,
            )
    share = hist[0] / n
    if share >= MIN_SHARE:
        return Rendered(
            f"Usually schedules calls back to back ({pct(share)}% of {n} gaps under 5 min).",
            share,
            n,
        )
    return None


def _length_label(i: int) -> str:
    return f"{i * LENGTH_STEP}+" if i == LENGTH_BUCKETS - 1 else str(i * LENGTH_STEP)


def render_meeting_length(stats: Stats, days: int) -> Rendered | None:
    hist = stats["duration_5min_hist"]
    n = sum(hist)
    if not _enough(n, days):
        return None
    top = sorted(range(len(hist)), key=lambda i: (-hist[i], i))[:2]
    share = hist[top[0]] / n
    if share >= MIN_SHARE:
        return Rendered(
            f"Meetings are typically {_length_label(top[0])} min ({pct(share)}% of {n}).", share, n
        )
    share = (hist[top[0]] + hist[top[1]]) / n
    if share >= MIN_SHARE:
        a, b = sorted(top)
        return Rendered(
            f"Meetings are typically {_length_label(a)} or {_length_label(b)} min"
            f" ({pct(share)}% of {n}).",
            share,
            n,
        )
    return None


def _slot_name(i: int) -> str:
    return f"{WEEKDAYS[i // 2]} {'afternoons' if i % 2 else 'mornings'}"


def render_meeting_free_slots(stats: Stats, days: int) -> Rendered | None:
    hist = stats["weekday_halfday_hist"]
    n = sum(hist)
    if not _enough(n, days):
        return None
    workweek = hist[:10]
    mean = sum(workweek) / 10
    if mean < 3:
        return None
    free = sorted(
        (i for i in range(10) if workweek[i] <= (1 - MIN_SHARE) * mean),
        key=lambda i: (workweek[i], i),
    )[:2]
    if not free:
        return None
    free.sort()
    share = min(1 - workweek[i] / mean for i in free)
    names = join_words([_slot_name(i) for i in free])
    if all(workweek[i] == 0 for i in free):
        return Rendered(f"No meetings on {names}.", share, n)
    seen = sum(workweek[i] for i in free)
    return Rendered(f"Rarely has meetings on {names} ({seen} of {n} meetings).", share, n)


def render_accept_buffer(stats: Stats, days: int) -> Rendered | None:
    dec, oth = stats["declined_gap_hist"], stats["other_gap_hist"]
    n = sum(dec) + sum(oth)
    if not _enough(n, days):
        return None
    for minutes in (15, 10, 5):
        k = INVITE_GAP_EDGES.index(minutes)
        tight_dec, tight = sum(dec[:k]), sum(dec[:k]) + sum(oth[:k])
        loose_dec, loose = sum(dec[k:]), sum(dec[k:]) + sum(oth[k:])
        if tight < MIN_CONTRAST_SAMPLES or loose < MIN_CONTRAST_SAMPLES:
            continue
        share = tight_dec / tight
        if share >= MIN_SHARE and loose_dec / loose <= 1 - MIN_SHARE:
            return Rendered(
                f"Declines invites that leave less than {minutes} min of buffer"
                f" ({pct(share)}% of {tight} such invites).",
                share,
                n,
            )
    return None


def render_accept_size(stats: Stats, days: int) -> Rendered | None:
    dec, oth = stats["declined_by_size"], stats["other_by_size"]
    n = sum(dec) + sum(oth)
    if not _enough(n, days):
        return None
    for k, limit in enumerate(SIZE_LABELS, start=1):
        big_dec, big = sum(dec[k:]), sum(dec[k:]) + sum(oth[k:])
        small_dec, small = sum(dec[:k]), sum(dec[:k]) + sum(oth[:k])
        if big < MIN_CONTRAST_SAMPLES or small < MIN_CONTRAST_SAMPLES:
            continue
        share = big_dec / big
        if share >= MIN_SHARE and small_dec / small <= 1 - MIN_SHARE:
            return Rendered(
                f"Usually declines meetings with more than {limit} attendees"
                f" ({pct(share)}% of {big}).",
                share,
                n,
            )
    return None


RENDERERS = {
    "habit:meeting_buffer": render_meeting_buffer,
    "habit:meeting_length": render_meeting_length,
    "habit:meeting_free_slots": render_meeting_free_slots,
    "rule:accept_buffer": render_accept_buffer,
    "rule:accept_size": render_accept_size,
}
