# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deterministic resolver for the situation's time (SPEC §8.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

import wwyd.predict as P
from wwyd.errors import Invalid
from wwyd.predict.when import When, resolve, when_line
from wwyd.store.personas import create_user, get_settings, update_settings

THU = date(2026, 10, 22)
SAT = date(2026, 10, 24)


@pytest.mark.parametrize(
    ("text", "today", "start", "end", "time"),
    [
        ("today", THU, THU, THU, ""),
        ("tonight", THU, THU, THU, "evening"),
        ("tomorrow 17:00", THU, date(2026, 10, 23), None, "17:00"),
        ("Tomorrow at 5 pm", THU, date(2026, 10, 23), None, "17:00"),
        ("day after tomorrow", THU, SAT, None, ""),
        ("overmorrow 17:00", THU, SAT, None, "17:00"),
        ("the day after tomorrow at 9:30am", THU, SAT, None, "09:30"),
        ("sat", THU, SAT, None, ""),
        ("on Saturday morning", THU, SAT, None, "morning"),
        ("thu", THU, THU, None, ""),  # a bare weekday equal to today is today
        ("this thursday", THU, THU, None, ""),
        ("next thu 15:00", THU, date(2026, 10, 29), None, "15:00"),  # next X on X: a week on
        ("next sat", THU, SAT, None, ""),  # next X on another day: the coming X
        ("this weekend", THU, SAT, date(2026, 10, 25), ""),
        ("weekend", SAT, SAT, date(2026, 10, 25), ""),  # on Saturday: today and Sunday
        ("next weekend", SAT, date(2026, 10, 31), date(2026, 11, 1), ""),
        ("next weekend", THU, date(2026, 10, 31), date(2026, 11, 1), ""),
        ("next week", THU, date(2026, 10, 26), date(2026, 11, 1), ""),
        ("in 3 days", THU, date(2026, 10, 25), None, ""),
        ("in 2 weeks", THU, date(2026, 11, 5), None, ""),
        ("2026-10-30", THU, date(2026, 10, 30), None, ""),
        ("18:30", THU, THU, None, "18:30"),
        ("12 am", THU, THU, None, "00:00"),
        ("fri midnight", THU, date(2026, 10, 23), None, "00:00"),
        # Month and year roll over.
        ("tomorrow", date(2026, 10, 31), date(2026, 11, 1), None, ""),
        ("day after tomorrow", date(2026, 12, 31), date(2027, 1, 2), None, ""),
        ("mon", date(2026, 12, 30), date(2027, 1, 4), None, ""),
    ],
)
def test_resolve(text, today, start, end, time):
    assert resolve(text, today) == When(start, end or start, time)


@pytest.mark.parametrize(
    ("text", "today", "end", "time"),
    [
        ("this week", THU, date(2026, 10, 25), ""),
        ("this week", date(2026, 10, 26), date(2026, 11, 1), ""),
        ("by fri", THU, date(2026, 10, 23), ""),
        ("by thu", THU, THU, ""),
        ("by next thu", THU, date(2026, 10, 29), ""),
        ("by monday 12:00", THU, date(2026, 10, 26), "12:00"),
        ("by tomorrow", THU, date(2026, 10, 23), ""),
    ],
)
def test_resolve_deadline(text, today, end, time):
    """A deadline or period is a range from today, never "the weekend"."""
    assert resolve(text, today) == When(today, end, time, deadline=True)


@pytest.mark.parametrize("text", ["by", "by weekend", "by next week", "by 2020-01-01"])
def test_resolve_rejects_bad_deadline(text):
    assert resolve(text, THU) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "soon",
        "next month",
        "17",
        "25:00",
        "13 pm",
        "2026-02-30",
        "tomorrow or sat",
        "the 5th",
        "friday 17:00 or later",
        "in many days",
        "завтра",
    ],
)
def test_resolve_rejects_garbage(text):
    assert resolve(text, THU) is None


def test_when_line_marks():
    assert when_line(resolve("tomorrow 17:00", THU)) == "Friday 2026-10-23"
    assert when_line(resolve("sat", THU)) == "Saturday 2026-10-24 (weekend)"
    assert when_line(resolve("this weekend", THU)) == (
        "Saturday 2026-10-24 to Sunday 2026-10-25 (weekend)"
    )
    assert when_line(resolve("next week", THU)) == "Monday 2026-10-26 to Sunday 2026-11-01"
    assert when_line(None) == ""
    # A deadline renders no line (it was read as a constraint).
    assert when_line(resolve("this week", THU)) == ""
    assert when_line(resolve("by fri 18:00", THU)) == ""
    assert when_line(resolve("this week", SAT)) == ""
    # A custom weekend changes the marks and the weekend block.
    fri_sat = ["friday", "saturday"]
    assert when_line(resolve("sun", THU, fri_sat), fri_sat) == "Sunday 2026-10-25"
    assert resolve("this weekend", THU, fri_sat) == When(date(2026, 10, 23), SAT)
    assert resolve("this weekend", THU, []) is None
    assert when_line(resolve("sat", THU, []), []) == "Saturday 2026-10-24"


def test_resolved_when_uses_persona_timezone_and_dst(monkeypatch):
    # 21:30 UTC on Saturday 2026-10-24 is already Sunday in Tokyo, still Saturday in Berlin.
    monkeypatch.setattr(P, "_now", lambda: datetime(2026, 10, 24, 21, 30, tzinfo=UTC))
    assert P.resolved_when("tomorrow", "Asia/Tokyo", None) == "Monday 2026-10-26"
    assert P.resolved_when("tomorrow", "Europe/Berlin", None) == "Sunday 2026-10-25 (weekend)"
    # Berlin leaves summer time on 2026-10-25: that evening it is UTC+1, so 23:30 UTC is
    # already Monday there and 22:30 UTC still Sunday (under UTC+2 both would be Monday).
    monkeypatch.setattr(P, "_now", lambda: datetime(2026, 10, 25, 23, 30, tzinfo=UTC))
    assert P.resolved_when("today", "Europe/Berlin", None) == "Monday 2026-10-26"
    monkeypatch.setattr(P, "_now", lambda: datetime(2026, 10, 25, 22, 30, tzinfo=UTC))
    assert P.resolved_when("today", "Europe/Berlin", None) == "Sunday 2026-10-25 (weekend)"
    assert P.resolved_when("today", "Not/AZone", None) == "Sunday 2026-10-25 (weekend)"
    assert P.resolved_when("", "Europe/Berlin", None) == ""
    assert P.resolved_when("whenever", "Europe/Berlin", None) == ""


def test_weekend_days_setting(conn):
    pid = create_user(conn, "w@example.com").persona_id
    assert get_settings(conn, pid)["weekend_days"] == ["saturday", "sunday"]
    out = update_settings(conn, pid, weekend_days=["Saturday", "friday", "friday"])
    assert out["weekend_days"] == ["friday", "saturday"]
    assert update_settings(conn, pid, weekend_days=[])["weekend_days"] == []
    for bad in (["sat"], ["Samstag"], "saturday"):
        with pytest.raises(Invalid):
            update_settings(conn, pid, weekend_days=bad)
    assert get_settings(conn, pid)["weekend_days"] == []
