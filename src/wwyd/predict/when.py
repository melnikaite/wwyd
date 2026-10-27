# SPDX-License-Identifier: AGPL-3.0-or-later
"""The time a situation refers to (SPEC §8.1): the model only extracts and translates it
into a small canonical English form ("tomorrow 17:00", "next thu", "this weekend",
"in 3 days", "2026-10-30"); this module resolves that form deterministically in the
persona timezone. Anything outside the grammar resolves to ``None`` (no When line), so a
date the parser is unsure about is never shown.

Grammar (case-insensitive, surrounding words "on", "at", "the" ignored)::

    when   := [day] [time] | time
    day    := today | tonight | tomorrow | day after tomorrow | overmorrow | <weekday>
            | this <weekday> | next <weekday> | [this] weekend | next weekend
            | this week | next week | by <single day> | in N day(s) | in N week(s)
            | YYYY-MM-DD
    time   := point | range | morning | afternoon | evening | night
    point  := HH:MM | H[:MM] am|pm | noon | midnight
    range  := P-P | P–P | P—P | between P and P | from P to|till|until|- P
            | P to|till|until P | P P

Inside a range P may also be a bare hour ("15-17", "between 3 and 5 pm"); the side-by-side
form "P P" needs at least one point that is not bare. An am/pm on one point only is given to
the other bare point too, unless that puts the start after the end, then the opposite one
("3 and 5 pm" is 15:00-17:00, "11 to 1 pm" is 11:00-13:00). A range is stored as
"15:00-17:00". A bare number alone is still not a time.

Fallback: when no split parses but a leading day part does, and the rest looks like a time
attempt (a digit, am/pm or a part-of-day word) with no day word of its own and the text has
no "or", the day is kept with no time ("tomorrow 25:00", "fri 15:00 sharp"). Alternatives
("tomorrow or thu", "tomorrow 15:00 or fri 10:00") stay None.

A bare weekday (and "this <weekday>") is its next occurrence counting today, so "sat" asked
on a Saturday is today. "next <weekday>" is the next occurrence strictly after today, so
"next thu" asked on a Thursday is a week later and on any other day equals "thu".

A week runs Monday to Sunday whatever the persona's weekend days. "this week" (also what
"by the end of the week" becomes) is a deadline on Sunday; "by <day>" is a deadline on the
single day the rest resolves to ("by next fri" like "next fri"). A deadline covers
today to its last day and is never read as "the weekend".

Rendering (``when_line``) shows only the date or range and its mark, (weekend) or (working day). The time is
parsed but not shown: the situation already carries it, and a second, canonical time
confused the model's comparisons. A deadline renders no line at all: a "by <day>" line was
read as a constraint on the owner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_WEEKEND = ("saturday", "sunday")
_ABBREV = {name[:3]: i for i, name in enumerate(WEEKDAYS)} | {
    "tues": 1,
    "wed": 2,
    "thur": 3,
    "thurs": 3,
}
_DAY_INDEX = {name: i for i, name in enumerate(WEEKDAYS)} | _ABBREV
_PARTS = ("morning", "afternoon", "evening", "night")
_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
_FILLER = {"on", "at", "the", "of"}
_P = r"(\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?|noon|midnight)"
_RANGES = (
    re.compile(rf"^{_P}\s*[-–—]\s*{_P}$"),
    re.compile(rf"^between\s+{_P}\s+and\s+{_P}$"),
    re.compile(rf"^from\s+{_P}\s*(?:\s(?:to|till|until)\s|[-–—])\s*{_P}$"),
    re.compile(rf"^{_P}\s+(?:to|till|until)\s+{_P}$"),
)
_SIDE_BY_SIDE = re.compile(rf"^{_P}\s+{_P}$")
_DAY_WORDS = (
    set(_DAY_INDEX)
    | {"today", "tonight", "tomorrow", "overmorrow", "day", "days", "week", "weeks"}
    | {"weekend", "next", "this", "by", "in"}
)


@dataclass(frozen=True)
class When:
    start: date
    end: date  # == start for a single day
    time: str = ""  # "17:00", "evening" or ""
    deadline: bool = False  # "by <end>": the range today..end, shown as "by <end>"


def _weekend_set(weekend: list[str] | tuple[str, ...] | None) -> set[int]:
    names = DEFAULT_WEEKEND if weekend is None else weekend
    return {WEEKDAYS.index(n) for n in names if n in WEEKDAYS}


def _time(tokens: list[str]) -> str | None:
    """The time part, "" when empty, None when it is not in the grammar."""
    text = " ".join(tokens)
    if not text:
        return ""
    if text in _PARTS:
        return text
    if text == "noon":
        return "12:00"
    if text == "midnight":
        return "00:00"
    m = _TIME.match(text)
    if not m:
        return _range(text)
    hour, minute, ampm = int(m[1]), int(m[2] or 0), m[3]
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif m[2] is None:
        return None  # a bare number is not a time
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _point(text: str) -> tuple[int, int, str | None, bool] | None:
    """(hour, minute, am/pm, bare) of one range end; noon/midnight carry a fixed suffix."""
    if text == "noon":
        return 12, 0, "fixed", False
    if text == "midnight":
        return 0, 0, "fixed", False
    m = _TIME.match(text)
    if not m:
        return None
    return int(m[1]), int(m[2] or 0), m[3], m[2] is None and m[3] is None


def _clock(hour: int, minute: int, ampm: str | None) -> int | None:
    """Minutes since midnight, None when out of range."""
    if ampm in ("am", "pm"):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _range(text: str) -> str | None:
    m = next((r.match(text) for r in _RANGES if r.match(text)), None)
    if m is None:
        m = _SIDE_BY_SIDE.match(text)
        if m and all(p and p[3] for p in map(_point, (m[1], m[2]))):
            return None  # two bare numbers side by side are not a range
    if m is None:
        return None
    a, b = _point(m[1]), _point(m[2])
    if a is None or b is None:
        return None
    sa, sb = a[2], b[2]
    if sb in ("am", "pm") and sa is None:
        sa = sb  # "3 and 5 pm"
        start, end = _clock(a[0], a[1], sa), _clock(b[0], b[1], sb)
        if start is None or end is None or start > end:
            sa = "am" if sb == "pm" else "pm"  # "11 to 1 pm"
    elif sa in ("am", "pm") and sb is None:
        sb = sa
        start, end = _clock(a[0], a[1], sa), _clock(b[0], b[1], sb)
        if start is None or end is None or start > end:
            sb = "am" if sa == "pm" else "pm"
    start, end = _clock(a[0], a[1], sa), _clock(b[0], b[1], sb)
    if start is None or end is None:
        return None
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def _time_attempt(tokens: list[str]) -> bool:
    """The leftover looks like a failed time, not a second day or other words."""
    if any(t in _DAY_WORDS or re.search(r"\d{4}-\d{2}-\d{2}", t) for t in tokens):
        return False
    text = " ".join(tokens)
    return bool(re.search(r"\d|\b(?:am|pm)\b", text)) or any(t in _PARTS for t in tokens)


def _weekend_block(day: date, weekend: set[int], skip_current: bool) -> tuple[date, date] | None:
    """The contiguous weekend days starting at the first weekend day on/after ``day``."""
    if not weekend or len(weekend) == 7:
        return None
    d = day
    if skip_current:
        while d.weekday() in weekend:
            d += timedelta(days=1)
    while d.weekday() not in weekend:
        d += timedelta(days=1)
    start = end = d  # on a weekend day "this weekend" starts today
    while (end + timedelta(days=1)).weekday() in weekend:
        end += timedelta(days=1)
    return start, end


def _day(tokens: list[str], today: date, weekend: set[int]) -> tuple[date, date, str, bool] | None:
    """(start, end, default time, deadline) for the day part; None outside the grammar."""
    if tokens[:1] == ["by"]:
        inner = _plain_day(tokens[1:], today, weekend) if len(tokens) > 1 else None
        if inner is None or inner[0] != inner[1] or inner[0] < today:
            return None
        return today, inner[1], inner[2], True
    if " ".join(tokens) == "this week":
        return today, today + timedelta(days=6 - today.weekday()), "", True
    day = _plain_day(tokens, today, weekend)
    return (*day, False) if day else None


def _plain_day(tokens: list[str], today: date, weekend: set[int]) -> tuple[date, date, str] | None:
    text = " ".join(tokens)
    if text in ("", "today"):
        return today, today, ""
    if text == "tonight":
        return today, today, "evening"
    if text == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=1), ""
    if text in ("day after tomorrow", "overmorrow"):
        return today + timedelta(days=2), today + timedelta(days=2), ""
    if text in ("weekend", "this weekend", "next weekend"):
        block = _weekend_block(today, weekend, skip_current=False)
        if block and text == "next weekend":
            block = _weekend_block(block[1] + timedelta(days=1), weekend, skip_current=True)
        return (*block, "") if block else None
    if text == "next week":
        monday = today + timedelta(days=7 - today.weekday())
        return monday, monday + timedelta(days=6), ""
    if m := re.fullmatch(r"in (\d{1,3}) (day|days|week|weeks)", text):
        n = int(m[1]) * (7 if m[2].startswith("week") else 1)
        return today + timedelta(days=n), today + timedelta(days=n), ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            d = date.fromisoformat(text)
        except ValueError:
            return None
        return d, d, ""
    prefix, _, name = text.rpartition(" ")
    if name in _DAY_INDEX and prefix in ("", "this", "next"):
        ahead = (_DAY_INDEX[name] - today.weekday()) % 7
        if prefix == "next" and ahead == 0:
            ahead = 7
        d = today + timedelta(days=ahead)
        return d, d, ""
    return None


def resolve(
    text: str, today: date, weekend: list[str] | tuple[str, ...] | None = None
) -> When | None:
    """Resolve the canonical form against ``today`` (a date in the persona timezone)."""
    words = re.sub(r"[,.]", " ", (text or "").strip().lower()).split()
    words = [w for w in words if w not in _FILLER]
    if not words or len(words) > 8:
        return None
    days = _weekend_set(weekend)
    # Split into day part + time part: try the longest time suffix that parses.
    for cut in range(len(words), -1, -1):
        time = _time(words[cut:])
        if time is None:
            continue
        day = _day(words[:cut], today, days)
        if day is None:
            continue
        start, end, default, deadline = day
        if not words[:cut] and not time:
            return None
        return When(start, end, time or default, deadline)
    if "or" in words:
        return None
    for cut in range(len(words) - 1, 0, -1):
        day = _day(words[:cut], today, days)
        if day is not None and _time_attempt(words[cut:]):
            return When(day[0], day[1], "", day[3])
    return None


def when_line(when: When | None, weekend: list[str] | tuple[str, ...] | None = None) -> str:
    """``Saturday 2026-10-24 (weekend)`` or ``Thursday 2026-10-22 (working day)``: every day
    is marked both ways, since an unmarked day was read as a weekend. A range is marked
    ``(weekend)`` or ``(working days)`` when all its days are alike and left unmarked when
    mixed. No time (the situation carries it). "" for None and for a deadline (it was read
    as a constraint)."""
    if when is None or when.deadline:
        return ""
    days = _weekend_set(weekend)
    span = [when.start + timedelta(days=i) for i in range((when.end - when.start).days + 1)]
    marks = {d.weekday() in days for d in span}
    text = when.start.strftime("%A %Y-%m-%d")
    if when.end != when.start:
        text += f" to {when.end.strftime('%A %Y-%m-%d')}"
    if marks == {True}:
        text += " (weekend)"
    elif marks == {False}:
        text += " (working days)" if len(span) > 1 else " (working day)"
    return text
