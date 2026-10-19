# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic episodes with known ground truth for the analyzer tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from wwyd.ingest.episodes import Episode
from wwyd.store.audiences import upsert_audience
from wwyd.store.categories import category_ids_by_path

BERLIN = ZoneInfo("Europe/Berlin")


def cat_id(conn, pid, path="work/meetings"):
    return category_ids_by_path(conn, pid)[path]


def add_audience(conn, pid, name):
    upsert_audience(conn, pid, name, description="x")
    return conn.execute(
        "SELECT id FROM audiences WHERE persona_id = ? AND name = ?", (pid, name)
    ).fetchone()[0]


def workdays(start: date, count: int) -> list[date]:
    out, d = [], start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def meetings(
    category_id: str,
    days: list[date],
    *,
    slots=((10, 0, 25), (11, 0, 50)),  # (hour, minute, minutes)
    tz=BERLIN,
    start_index: int = 0,
    audience: str | None = None,
    skip_friday_pm: bool = True,
) -> list[Episode]:
    eps, i = [], start_index
    for d in days:
        for hour, minute, length in slots:
            if skip_friday_pm and d.weekday() == 4 and hour >= 13:
                continue
            start = datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)
            eps.append(
                Episode(
                    index=i,
                    category_id=category_id,
                    category="work/meetings",
                    audience=audience,
                    start=start,
                    end=start + timedelta(minutes=length),
                    situation="meeting",
                )
            )
            i += 1
    return eps


def replies(
    category_id: str, days: list[date], audience: str, *, latency_min=3, hour=10, tz=BERLIN
) -> list[Episode]:
    """Replies sent ``latency_min`` after the message arrived (``situation_at``, D23)."""
    eps = []
    for i, d in enumerate(days):
        arrived = datetime(d.year, d.month, d.day, hour, 0, tzinfo=tz)
        eps.append(
            Episode(
                index=1000 + i,
                category_id=category_id,
                category="communication/style",
                audience=audience,
                start=arrived + timedelta(minutes=latency_min),
                situation_at=arrived,
                situation="asked something",
                said="sure, will do",
            )
        )
    return eps
