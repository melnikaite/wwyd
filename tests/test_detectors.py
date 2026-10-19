# SPDX-License-Identifier: AGPL-3.0-or-later
"""Habit detectors (SPEC §7.2) on synthetic data with known ground truth."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ingest_data import BERLIN, calendar, ev, messages

from wwyd.ingest import detectors
from wwyd.ingest.keyed import upsert_finding
from wwyd.ingest.records import parse_batch
from wwyd.store.claims import review_claims, update_claim

UTC = ZoneInfo("UTC")


def batch(records, tz=BERLIN, audiences=("everyone", "work-slack")):
    return parse_batch(records, tz=tz, audiences=set(audiences), default_audience="everyone")


def rendered(records, tz=BERLIN):
    out = {}
    for f in detectors.collect_all(batch(records, tz), tz):
        r = detectors.render(f.key, f.stats, (f.last - f.first).days + 1)
        out[f.key] = r
    return out


def test_calendar_ground_truth():
    r = rendered(calendar())
    assert r["habit:meeting_buffer"].text.startswith("Keeps at least 10 min between calls")
    assert r["habit:meeting_length"].text.startswith("Meetings are typically 25 or 50 min")
    assert r["habit:meeting_free_slots"].text == "No meetings on Friday afternoons."
    assert r["rule:accept_size"].text.startswith(
        "Usually declines meetings with more than 8 attendees"
    )
    assert r["rule:accept_buffer"].text.startswith(
        "Declines invites that leave less than 10 min of buffer"
    )
    active = r["habit:active_hours"].text
    assert active.startswith("Active 09:00–") and "rarely on Saturday and Sunday" in active
    for res in r.values():
        assert 0.75 <= res.share <= 1 and 0 < res.confidence <= 0.95


def test_below_threshold_samples_and_period():
    few = calendar(weeks=6)[:20]  # < 30 samples
    assert all(v is None for v in rendered(few).values())
    short = calendar(weeks=2)  # plenty of samples, but < 3 weeks
    assert all(v is None for v in rendered(short).values())


def test_messages_ground_truth():
    r = rendered(messages("work-slack"))
    assert r["habit:reply_latency:work-slack"].text.startswith(
        "Replies to work-slack within 5 min in work hours"
    )
    assert r["habit:message_length:work-slack"].text.startswith(
        "Writes to work-slack in messages under 40 characters, usually one line"
    )


def test_dst_and_offsets_are_honoured():
    # Berlin switches to summer time on 2026-03-29. Meetings at 09:00 local are sent in UTC:
    # 08:00Z before the switch, 07:00Z after it. All must land in the 09:00 bucket.
    recs = []
    for d in (date(2026, 3, 9) + timedelta(days=i) for i in range(42)):
        if d.weekday() < 5:
            local = datetime(d.year, d.month, d.day, 9, 0, tzinfo=BERLIN)
            recs.append(
                ev(d, 9, 0, 30, tz=BERLIN)
                | {
                    "start": local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": (local + timedelta(minutes=30))
                    .astimezone(UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
    found = {f.key: f for f in detectors.collect_all(batch(recs), BERLIN)}
    hist = found["habit:active_hours"].stats["weekday_halfhour_hist"]
    nine = sum(hist[d * 48 + 18] for d in range(5))
    assert nine == len(recs)
    # Durations across the switch are real minutes, not wall-clock differences.
    lengths = found["habit:meeting_length"].stats["duration_5min_hist"]
    assert lengths[6] == len(recs)
    # A naive datetime is read in the persona's zone.
    naive = batch(
        [
            ev(date(2026, 3, 30), 9, 0, 30)
            | {"start": "2026-03-30T09:00:00", "end": "2026-03-30T09:30:00"}
        ]
    )
    assert naive.events[0].start.utcoffset() == timedelta(hours=2)


def test_overnight_gap_is_not_a_buffer():
    # Gaps are only measured between meetings on the same local day.
    recs = [ev(date(2026, 2, 2), 23, 0, 30), ev(date(2026, 2, 3), 0, 5, 30)]
    f = {x.key: x for x in detectors.collect_all(batch(recs), BERLIN)}
    assert sum(f["habit:meeting_buffer"].stats["gap_minutes_hist"]) == 0


def _claim(conn, pid, key):
    return conn.execute(
        "SELECT * FROM claims WHERE persona_id = ? AND key = ?", (pid, key)
    ).fetchone()


def _upsert_all(conn, pid, records):
    out = {}
    for f in detectors.collect_all(batch(records), BERLIN):
        out[f.key] = upsert_finding(conn, pid, f)
    return out


def test_evidence_merges_across_runs(conn, pid):
    first = _upsert_all(conn, pid, calendar(first=date(2026, 2, 2), weeks=4))
    assert first["habit:meeting_buffer"][0] == "created"
    row1 = _claim(conn, pid, "habit:meeting_buffer")
    ev1 = json.loads(row1["evidence"])

    second = _upsert_all(conn, pid, calendar(first=date(2026, 3, 2), weeks=4))
    assert second["habit:meeting_buffer"] == ("updated", row1["id"])
    rows = conn.execute(
        "SELECT * FROM claims WHERE persona_id = ? AND key = 'habit:meeting_buffer'", (pid,)
    ).fetchall()
    assert len(rows) == 1
    ev2 = json.loads(rows[0]["evidence"])
    assert ev2["stats"]["gap_minutes_hist"] == [2 * x for x in ev1["stats"]["gap_minutes_hist"]]
    assert ev2["sources"]["calendar_event"] == 2 * ev1["sources"]["calendar_event"]
    assert ev2["period"] == "2026-02-02..2026-03-27"
    assert str(sum(ev2["stats"]["gap_minutes_hist"])) in rows[0]["text"]
    assert rows[0]["status"] == "pending" and rows[0]["origin"] == "derived"


def test_below_threshold_twice_creates_nothing(conn, pid):
    assert _upsert_all(conn, pid, calendar(weeks=6)[:10])["habit:meeting_buffer"][0] == (
        "below_threshold"
    )
    assert _claim(conn, pid, "habit:meeting_buffer") is None


def test_rejected_keyed_claim_is_not_resurrected(conn, pid):
    _upsert_all(conn, pid, calendar())
    row = _claim(conn, pid, "habit:meeting_length")
    review_claims(conn, pid, approve=[], reject=[row["id"]])
    again = _upsert_all(conn, pid, calendar(first=date(2026, 4, 6)))
    assert again["habit:meeting_length"] == ("rejected", None)
    rows = conn.execute(
        "SELECT status FROM claims WHERE persona_id = ? AND key = 'habit:meeting_length'", (pid,)
    ).fetchall()
    assert [r["status"] for r in rows] == ["rejected"]


def test_owner_edited_text_is_kept(conn, pid):
    _upsert_all(conn, pid, calendar())
    row = _claim(conn, pid, "habit:meeting_free_slots")
    review_claims(conn, pid, approve=[row["id"]], reject=[])
    update_claim(conn, pid, row["id"], text="Keeps Friday afternoons free.")
    _upsert_all(conn, pid, calendar(first=date(2026, 4, 6)))
    after = _claim(conn, pid, "habit:meeting_free_slots")
    assert after["text"] == "Keeps Friday afternoons free."
    assert after["status"] == "approved"
    stats = json.loads(after["evidence"])["stats"]["weekday_halfday_hist"]
    assert sum(stats) > 0
