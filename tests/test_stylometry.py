# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stylometry builtin with synthetic ground truth (SPEC §7.2, D22; docs/habits.md)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from analyzer_data import BERLIN, add_audience, cat_id, workdays

from wwyd.analyzers import run_analyzers
from wwyd.analyzers.builtin import BUILTINS, base
from wwyd.analyzers.builtin.stylometry import (
    ENDING_CLASSES,
    MAX_KEYS,
    ending_class,
    favourite_words,
    keyness,
    percentile,
    profile,
)
from wwyd.analyzers.state import read_state, totals
from wwyd.ingest.episodes import Episode

START = date(2026, 9, 7)  # a Monday

FAMILY = [
    "ok darling see you soon)))",
    "hugs darling, will call masha later 🙂",
    "darling yes)))",
    "hugs and kisses",
    "sure darling",
]
WORK = [
    "Thanks, I will send the report by Friday.",
    "Sure. The report is in the shared folder.",
    "Agreed. Let us move the review to Tuesday.",
    "I will check the report and reply today.",
    "Noted. The deadline stays as planned.",
]


@pytest.fixture
def wired(monkeypatch, conn):
    """Kept for the tests that request it: the runner now passes the connection itself."""


def said_episodes(cat, audience, texts, days, *, start_index=0, hour=19):
    eps, i = [], start_index
    for n, d in enumerate(days):
        start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=BERLIN)
        eps.append(
            Episode(
                index=i,
                category_id=cat,
                category="communication/style",
                audience=audience,
                start=start,
                end=start + timedelta(minutes=2),
                situation="asked something",
                said=texts[n % len(texts)],
            )
        )
        i += 1
    return eps


def _setup(conn, pid):
    cat = cat_id(conn, pid, "communication/style")
    fam = add_audience(conn, pid, "family")
    work = add_audience(conn, pid, "colleagues")
    return cat, fam, work


def _run(conn, pid, eps):
    counts: dict[str, int] = {}
    run_analyzers(conn, persona_id=pid, episodes=eps, tz=BERLIN, counts=counts)
    return counts


def _state(conn, pid, aud):
    row = conn.execute(
        "SELECT id FROM analyzers WHERE persona_id = ? AND name = 'stylometry' AND scope_id = ?",
        (pid, aud),
    ).fetchone()
    return read_state(conn, row["id"])


def _claim(conn, pid, aud):
    return conn.execute(
        "SELECT * FROM claims WHERE persona_id = ? AND key = ? AND status <> 'archived'",
        (pid, f"builtin:stylometry:{aud}"),
    ).fetchone()


def test_registered():
    b = BUILTINS["stylometry"]
    assert b.scope_kind == "audience" and b.kind == "style"
    assert b.claim_category == "communication/style" and tuple(b.text_fields) == ("said",)


def test_helpers_ground_truth():
    assert ending_class("ok)))") == "brackets"
    assert ending_class("ok :)") == "smiley"
    assert ending_class("ok") == "none"
    assert ending_class("Ok.") == "period"
    assert ending_class("ok 🙂") == "emoji"
    assert percentile([0, 5, 5], (0, 10, 20), 0.5) == 20.0
    assert percentile([0, 9, 1], (0, 10, 20), 0.9) == 20.0
    assert percentile([0, 0, 1], (0, 10, 20), 0.9) == 40.0
    assert percentile([], (0, 10), 0.5) == 0.0


def test_ground_truth_family_vs_work(conn, pid, wired):
    cat, fam, work = _setup(conn, pid)
    days = workdays(START, 30)  # 6 weeks
    eps = said_episodes(cat, "family", FAMILY, days) + said_episodes(
        cat, "colleagues", WORK, days, start_index=100, hour=10
    )
    counts = _run(conn, pid, eps)
    _run(conn, pid, [])  # no-op
    st = _state(conn, pid, fam)
    stats = profile(st)
    assert stats["n"] == 30
    assert stats["lowercase_share"] == 1.0
    assert stats["period_share"] == 0.0
    assert ENDING_CLASSES[int(stats["ending_dominant"])] in ("none", "brackets")
    assert stats["ending_hist"][ENDING_CLASSES.index("brackets")] == 12
    assert stats["emoji_share"] == 0.2
    assert stats["chars_p90"] <= 50
    words = totals(st, "words")
    # Names never survive: "masha" is in only 6 of 30 family episodes but lowercase, on ≥ 3
    # days, so it is the documented residual risk; capitalised canaries are covered below.
    assert "darling" in words and "hugs" in words
    assert counts["analyzer_keys_dropped"] >= 0

    wst = profile(_state(conn, pid, work))
    assert wst["capital_share"] == 1.0 and wst["period_share"] == 1.0

    # The work run happened after family's (IDs sorted), so re-render with both states.
    _run(
        conn,
        pid,
        said_episodes(cat, "family", FAMILY, workdays(date(2026, 10, 19), 5), start_index=500),
    )
    assert favourite_words(conn, pid, fam)[:2] == ["darling", "hugs"]
    assert "report" in favourite_words(conn, pid, work)
    claim = _claim(conn, pid, fam)
    assert claim["status"] == "pending" and claim["kind"] == "style"
    assert claim["text"].startswith("Writes to family in 1 short line (90% under ")
    assert "lowercase" in claim["text"] and "no final period" in claim["text"]
    assert "favourite words: darling, hugs" in claim["text"]
    assert len(claim["text"]) <= 500
    ev = json.loads(claim["evidence"])
    assert ev["sources"] == {"analyzer": 35}
    assert all(
        isinstance(v, int | float) or all(isinstance(x, int | float) for x in v)
        for v in ev["stats"].values()
    )
    aud = conn.execute("SELECT audience_id FROM claim_audiences WHERE claim_id = ?", (claim["id"],))
    assert [r[0] for r in aud] == [fam]
    wclaim = _claim(conn, pid, work)
    assert "starting with a capital" in wclaim["text"] and "ends with a period" in wclaim["text"]


def test_names_dropped_by_key_filter(conn, pid, wired):
    cat, fam, _ = _setup(conn, pid)
    days = workdays(START, 30)
    eps = said_episodes(cat, "family", ["ok darling"] * 5, days)
    # a capitalised name in non-initial position, on every day: name-like
    for e in eps[:20]:
        e.said = "ok call Olga later"
    # a lowercase name in only 3 episodes: below the 5-episode spread
    for e in eps[20:23]:
        e.said = "ok ask petya"
    _run(conn, pid, eps)
    keys = set(totals(_state(conn, pid, fam), "words")) | set(
        w for k in totals(_state(conn, pid, fam), "bigrams") for w in k.split()
    )
    assert "olga" not in keys and "petya" not in keys
    assert "ok" in keys and "call" in keys
    text = _claim(conn, pid, fam)["text"]
    assert "olga" not in text.casefold() and "petya" not in text


def test_thresholds_and_accumulation(conn, pid, wired):
    cat, fam, _ = _setup(conn, pid)
    # 20 messages over 4 weeks: below 30 samples, no claim
    _run(conn, pid, said_episodes(cat, "family", FAMILY, workdays(START, 20)))
    assert _claim(conn, pid, fam) is None
    # 15 more in the following weeks: 35 samples, period > 3 weeks
    _run(
        conn,
        pid,
        said_episodes(cat, "family", FAMILY, workdays(date(2026, 10, 5), 15), start_index=50),
    )
    assert _claim(conn, pid, fam) is not None
    assert profile(_state(conn, pid, fam))["n"] == 35


def test_short_period_has_no_claim(conn, pid):
    cat, fam, _ = _setup(conn, pid)
    eps = [
        e
        for d in workdays(START, 10)  # two weeks, 4 a day
        for e in said_episodes(cat, "family", FAMILY, [d] * 4)
    ]
    for i, e in enumerate(eps):
        e.index = i
    _run(conn, pid, eps)
    assert _claim(conn, pid, fam) is None


def test_word_cap_per_month():
    from wwyd.analyzers.builtin import host_fields
    from wwyd.analyzers.builtin.stylometry import run

    text = " ".join(f"w{chr(97 + i // 26)}{chr(97 + i % 26)}x" for i in range(300))
    ep = Episode(0, "cat", "c", None, start=datetime(2026, 9, 7, 10, tzinfo=BERLIN), said=text)
    ctx = base.Context("a", "audience", "aud", "family", BERLIN)
    out = run([host_fields(ep, BERLIN, {})], ctx)
    assert len(out["words"]["2026-09"]) == MAX_KEYS
    assert len(out["bigrams"]["2026-09"]) == MAX_KEYS


def test_keyness_measure():
    target = {"darling": 20, "the": 50, "ok": 30, "rare": 4}
    ref = {"the": 500, "ok": 300, "report": 100, "rare": 0}
    assert keyness(target, ref) == ["darling"]
    assert keyness(target, {}) == []
    # a word used as often everywhere is not a favourite
    assert keyness({"the": 50, "x": 50}, {"the": 50, "x": 50}) == []


def test_favourite_words_without_row(conn, pid):
    _, fam, _ = _setup(conn, pid)
    assert favourite_words(conn, pid, fam) == []
