# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic calendars and message streams as episodes, with known ground truth."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def ev(day: date, h: int, m: int, minutes: int, response="accepted", attendees=3, tz=BERLIN):
    """A meeting invite as a choice episode: the interval is the meeting's span."""
    start = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
    return {
        "when": {
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=minutes)).isoformat(),
        },
        "where": "online",
        "category": "work/meetings",
        "situation": f"meeting invite, {attendees} attendees",
        "decision": response,
    }


def workdays(first: date, weeks: int):
    for i in range(weeks * 7):
        d = first + timedelta(days=i)
        if d.weekday() < 5:
            yield d


def calendar(first=date(2026, 2, 2), weeks=6):
    """Mon-Fri: 09:00 25 min, 09:40 50 min (15 min gap), 11:00 25 min (35 min gap);
    Mon-Thu also 14:00 50 min and 15:00 25 min (10 min gap). Never on Friday afternoon.
    Plus declined invites: big meetings (12 attendees) and ones leaving < 5 min of buffer."""
    out = []
    for d in workdays(first, weeks):
        out += [ev(d, 9, 0, 25), ev(d, 9, 40, 50), ev(d, 11, 0, 25, attendees=6)]
        if d.weekday() < 4:
            out += [ev(d, 14, 0, 50), ev(d, 15, 0, 25)]  # 10 min gap
            # A big invite in the afternoon, well away from other meetings: declined.
            out.append(ev(d, 16, 30, 30, response="declined", attendees=12))
        # A tight invite right after the 11:00 meeting: declined.
        out.append(ev(d, 11, 27, 20, response="declined", attendees=3))
    return out


def messages(audience: str, first=date(2026, 2, 2), weeks=5, per_day=2, tz=BERLIN):
    """Chat replies on workdays at 10:00 / 15:00: the interval runs from the incoming
    message to the owner's reply 3 min later, one short line."""
    out = []
    for d in workdays(first, weeks):
        for k in range(per_day):
            ts = datetime(d.year, d.month, d.day, 10 + 5 * k, 0, tzinfo=tz)
            out.append(
                {
                    "when": {
                        "start": ts.isoformat(),
                        "end": (ts + timedelta(minutes=3)).isoformat(),
                    },
                    "audience": audience,
                    "category": "communication/chat",
                    "situation": "Can you look at this when you have a minute please",
                    "said": "sure, on it",
                }
            )
    return out
