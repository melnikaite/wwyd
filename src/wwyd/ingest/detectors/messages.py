# SPDX-License-Identifier: AGPL-3.0-or-later
"""Activity and message detectors: active_hours, reply_latency:<aud>, message_length:<aud>
(SPEC §7.2; formulas in docs/habits.md). Message texts are only measured (length, line
count); the timestamps are bucketed in the persona's zone."""

from __future__ import annotations

from collections import defaultdict
from datetime import tzinfo

from wwyd.ingest.detectors.calendar import ATTENDING
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
from wwyd.ingest.records import Batch, Message

WORK_START, WORK_END = 9, 18  # "work hours" for reply latency: Mon-Fri 09:00-18:00 local
LATENCY_EDGES = (0, 5, 15, 60, 240, 1440)  # minutes; last bucket = more than a day
LATENCY_WORDS = {5: "5 min", 15: "15 min", 60: "an hour", 240: "4 hours", 1440: "a day"}
LENGTH_EDGES = (0, 40, 100, 200, 400, 800)  # characters
LINE_EDGES = (1, 2, 3, 6)  # 1, 2, 3-5, 6+
ACTIVE_TRIM = 0.1  # active window drops the earliest and latest 10% of weekday activity
RARE_DAY = 0.15  # a day is "rare" below 15% of the average weekday's activity
MIN_SIDE = 10  # minimum replies in/out of work hours to describe that side


def _period(batch_dates):  # noqa: ANN001, ANN202
    return date_span(batch_dates)


def collect(batch: Batch, tz: tzinfo) -> list[Finding]:
    findings: list[Finding] = []
    owner = [m for m in batch.messages if m.from_owner]
    attending = [e for e in batch.events if e.response in ATTENDING]

    stamps = [m.ts.astimezone(tz) for m in owner] + [e.start.astimezone(tz) for e in attending]
    if stamps:
        hist = [0] * (7 * 48)
        for t in stamps:
            hist[t.weekday() * 48 + t.hour * 2 + (t.minute >= 30)] += 1
        first, last = _period([t.date() for t in stamps])
        sources = {"message": len(owner), "calendar_event": len(attending)}
        findings.append(
            Finding(
                "habit:active_hours",
                "habit",
                "schedule/routine",
                {"weekday_halfhour_hist": hist},
                {k: v for k, v in sources.items() if v},
                first,
                last,
            )
        )

    by_audience: dict[str, list[Message]] = defaultdict(list)
    for m in batch.messages:
        by_audience[m.audience].append(m)
    for audience, msgs in sorted(by_audience.items()):
        findings.extend(_audience_findings(audience, msgs, tz))
    return findings


def _audience_findings(audience: str, msgs: list[Message], tz: tzinfo) -> list[Finding]:
    out: list[Finding] = []
    threads: dict[str, list[Message]] = defaultdict(list)
    for m in msgs:
        threads[m.thread].append(m)
    in_hours = [0] * len(LATENCY_EDGES)
    off_hours = [0] * len(LATENCY_EDGES)
    pair_days = []
    for thread in threads.values():
        thread.sort(key=lambda m: (m.ts, m.position if m.position is not None else 0))
        waiting: Message | None = None
        for m in thread:
            if not m.from_owner:
                waiting = waiting or m
                continue
            if waiting is not None:
                latency = (m.ts - waiting.ts).total_seconds() / 60
                if latency >= 0:
                    local = waiting.ts.astimezone(tz)
                    work = local.weekday() < 5 and WORK_START <= local.hour < WORK_END
                    (in_hours if work else off_hours)[bucket(latency, LATENCY_EDGES)] += 1
                    pair_days.append(local.date())
                waiting = None
    if pair_days:
        first, last = _period(pair_days)
        out.append(
            Finding(
                f"habit:reply_latency:{audience}",
                "style",
                "communication/style",
                {"in_hours_hist": in_hours, "off_hours_hist": off_hours},
                {"message": len(pair_days)},
                first,
                last,
                audience=audience,
            )
        )

    owner = [m for m in msgs if m.from_owner]
    if owner:
        chars = [0] * len(LENGTH_EDGES)
        lines = [0] * len(LINE_EDGES)
        for m in owner:
            text = m.text.strip()
            chars[bucket(len(text), LENGTH_EDGES)] += 1
            n_lines = len([ln for ln in text.splitlines() if ln.strip()]) or 1
            lines[bucket(n_lines, LINE_EDGES)] += 1
        first, last = _period([m.ts.astimezone(tz).date() for m in owner])
        out.append(
            Finding(
                f"habit:message_length:{audience}",
                "style",
                "communication/style",
                {"chars_hist": chars, "lines_hist": lines},
                {"message": len(owner)},
                first,
                last,
                audience=audience,
            )
        )
    return out


# --- renderers -----------------------------------------------------------------------------


def _hhmm(slot: int) -> str:
    return f"{slot // 2:02d}:{30 * (slot % 2):02d}"


def render_active_hours(stats: Stats, days: int) -> Rendered | None:
    hist = stats["weekday_halfhour_hist"]
    n = sum(hist)
    if n < MIN_SAMPLES or days < MIN_PERIOD_DAYS:
        return None
    weekdays = [sum(hist[d * 48 + s] for d in range(5)) for s in range(48)]
    total = sum(weekdays)
    if total < MIN_SAMPLES:
        return None
    cum, start, end = 0, None, None
    for s, count in enumerate(weekdays):
        cum += count
        if start is None and cum > ACTIVE_TRIM * total:
            start = s
        if end is None and cum >= (1 - ACTIVE_TRIM) * total:
            end = s + 1
            break
    assert start is not None and end is not None
    share = sum(weekdays[start:end]) / total
    if share < MIN_SHARE:
        return None
    per_day = [sum(hist[d * 48 : (d + 1) * 48]) for d in range(7)]
    avg_weekday = total / 5
    rare = [WEEKDAYS[d] for d in range(7) if per_day[d] <= RARE_DAY * avg_weekday]
    end_label = "24:00" if end == 48 else _hhmm(end)
    text = f"Active {_hhmm(start)}–{end_label} on weekdays"
    if rare:
        text += f", rarely on {join_words(rare)}"
    return Rendered(f"{text} ({pct(share)}% of {total} weekday events and messages).", share, n)


def _within(hist: list[int]) -> tuple[str, float] | None:
    n = sum(hist)
    cum = 0
    for k in range(1, len(LATENCY_EDGES)):
        cum += hist[k - 1]
        if cum / n >= MIN_SHARE:
            return LATENCY_WORDS[LATENCY_EDGES[k]], cum / n
    return None


def render_reply_latency(audience: str):  # noqa: ANN201
    def render(stats: Stats, days: int) -> Rendered | None:
        inside, outside = stats["in_hours_hist"], stats["off_hours_hist"]
        n = sum(inside) + sum(outside)
        if n < MIN_SAMPLES or days < MIN_PERIOD_DAYS:
            return None
        a = _within(inside) if sum(inside) >= MIN_SIDE else None
        b = _within(outside) if sum(outside) >= MIN_SIDE else None
        if a and b:
            if a[0] == b[0]:
                text = f"Replies to {audience} within {a[0]}, in and out of work hours"
            else:
                text = f"Replies to {audience} within {a[0]} in work hours, within {b[0]} otherwise"
            share = min(a[1], b[1])
        elif a:
            text, share = f"Replies to {audience} within {a[0]} in work hours", a[1]
        elif b:
            text, share = f"Replies to {audience} within {b[0]} outside work hours", b[1]
        else:
            return None
        return Rendered(f"{text} ({pct(share)}% of {n} replies).", share, n)

    return render


def render_message_length(audience: str):  # noqa: ANN201
    def render(stats: Stats, days: int) -> Rendered | None:
        chars, lines = stats["chars_hist"], stats["lines_hist"]
        n = sum(chars)
        if n < MIN_SAMPLES or days < MIN_PERIOD_DAYS:
            return None
        cum = 0
        for k in range(1, len(LENGTH_EDGES)):
            cum += chars[k - 1]
            if cum / n >= MIN_SHARE:
                limit, share = LENGTH_EDGES[k], cum / n
                break
        else:
            return None
        text = f"Writes to {audience} in messages under {limit} characters"
        if lines[0] / n >= MIN_SHARE:
            text += ", usually one line"
        elif (lines[0] + lines[1]) / n >= MIN_SHARE:
            text += ", usually 1–2 lines"
        return Rendered(f"{text} ({pct(share)}% of {n} messages).", share, n)

    return render
