# SPDX-License-Identifier: AGPL-3.0-or-later
"""Annotation (SPEC §7.3, §13): few-distinct mapping, batched tagging, vocabulary reuse,
only label names persisted, guard drops."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import call

from wwyd.config import Thresholds, configure_thresholds, thresholds
from wwyd.ingest import annotate, jobs

pytestmark = pytest.mark.anyio


@pytest.fixture
def small_threshold():
    old = thresholds()
    configure_thresholds(Thresholds(10, old.split_threshold))
    yield
    configure_thresholds(old)


def quiet(fake_llm):
    fake_llm.on("author_analyzers", {"hypotheses": []})
    fake_llm.on("word_usage", {"note": ""})
    fake_llm.on("judge", {"verdicts": []})
    fake_llm.on("decisions", {"claims": []})
    fake_llm.on("statements", {"claims": []})
    fake_llm.on("replies", {"claims": []})
    fake_llm.on("describe_categories", {"descriptions": []})


async def run(client, episodes, **kw):
    out = await call(client, "ingest_raw", {"episodes": episodes, **kw})
    await jobs.wait(out["job_id"], timeout=60)
    return await call(client, "job_status", {"job_id": out["job_id"]})


def days(n):
    first = date(2026, 3, 2)
    return [(first + timedelta(days=i)).isoformat() for i in range(n)]


def films(n, distinct=True):
    return [
        {
            "when": f"{d}T21:00:00+01:00",
            "category": "hobbies/films",
            "decision": f"watched film number {i} to the end" if distinct else "watched",
        }
        for i, d in enumerate(days(n))
    ]


def numbered(call_):
    return [ln.split(". ", 1) for ln in call_["user"].splitlines() if ". " in ln]


async def test_few_distinct_mapping_and_labels_in_passes(
    client, conn, pid, fake_llm, small_threshold
):
    quiet(fake_llm)
    eps = [
        {
            "when": f"{d}T10:00:00+01:00",
            "category": "work/meetings",
            "situation": "weekly standup with the team" if i % 2 else "one to one with my lead",
            "decision": "Declined." if i % 3 == 0 else "accepted",
            "said": "ok, see you",
        }
        for i, d in enumerate(days(12))
    ]

    def label(call_):
        lines = numbered(call_)
        if "decisions" in call_["system"]:
            return {
                "labels": [
                    {"i": int(i), "label": "declined" if "declin" in v else "accepted"}
                    for i, v in lines
                ]
            }
        return {
            "labels": [
                {"i": int(i), "label": "standup" if "standup" in v else "1-1"} for i, v in lines
            ]
        }

    fake_llm.on("annotate", label)
    status = await run(client, eps)
    assert status["status"] == "done"
    ann = fake_llm.by_purpose("annotate")
    assert len(ann) == 2  # one mapping call per field
    # Distinct normalised values only: "declined." and "accepted".
    decision_call = next(c for c in ann if "decisions" in c["system"])
    assert len(numbered(decision_call)) == 2
    rows = conn.execute(
        "SELECT field, label FROM category_labels WHERE persona_id = ? ORDER BY field, label",
        (pid,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("decision", "accepted"),
        ("decision", "declined"),
        ("situation", "1-1"),
        ("situation", "standup"),
    ]
    assert status["counts"]["labels_created"] == 4
    assert status["counts"]["annotation_calls"] == 2
    # The passes run after annotation and carry the labels (names only).
    replies = fake_llm.by_purpose("replies")
    assert replies and "[labels: decision=declined; situation=1-1]" in replies[0]["user"]


async def test_batched_tagging_reuses_vocabulary(client, conn, pid, fake_llm, small_threshold):
    quiet(fake_llm)
    seen_vocab = []

    def tag(call_):
        seen_vocab.append(call_["system"])
        return {
            "tags": [
                {"i": int(i), "tags": ["Drama", "slow burn", "x" * 50]} for i, _ in numbered(call_)
            ]
        }

    fake_llm.on("annotate", tag)
    status = await run(client, films(60))
    calls = fake_llm.by_purpose("annotate")
    assert len(calls) == 3  # 60 distinct decisions > 50: tagging, 20 per call
    assert all(len(numbered(c)) <= 20 for c in calls)
    labels = {r[0] for r in conn.execute("SELECT label FROM category_labels WHERE field='tag'")}
    assert labels == {"drama", "slow-burn"}  # normalised; the 50-char tag is invalid
    assert status["counts"]["labels_created"] == 2
    # A second job sees the stored tag vocabulary in its prompt.
    seen_vocab.clear()
    await run(client, films(60))
    assert "drama" in seen_vocab[0] and "slow-burn" in seen_vocab[0]


async def test_guard_drops_names_and_nothing_but_names_persisted(
    client, conn, pid, fake_llm, small_threshold, tmp_path
):
    quiet(fake_llm)
    eps = [
        {
            "when": f"{d}T10:00:00+01:00",
            "category": "work/meetings",
            "situation": f"lunch invite from Velmorine about zq7x-{i}",
            "decision": "accepted",
        }
        for i, d in enumerate(days(12))
    ]

    def echo(call_):
        return {
            "labels": [
                {"i": int(i), "label": "velmorine" if "velmorine" in v else "accepted"}
                for i, v in numbered(call_)
            ]
        }

    fake_llm.on("annotate", echo)
    status = await run(client, eps)
    assert status["counts"]["dropped_by_guard"] >= 1
    labels = {r[0] for r in conn.execute("SELECT label FROM category_labels")}
    assert "velmorine" not in labels
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = (tmp_path / "test.db").read_bytes()
    assert b"lunch invite" not in blob and b"Velmorine" not in blob


async def test_small_increments_are_not_annotated(client, fake_llm):
    quiet(fake_llm)
    fake_llm.on("annotate", {"labels": []})
    await run(client, films(5, distinct=False))
    assert fake_llm.by_purpose("annotate") == []
    # With llm_for_ingest false nothing is annotated even when large.
    await run(client, films(120, distinct=False), llm_for_ingest=False)
    assert fake_llm.by_purpose("annotate") == []


def test_normalize_label():
    assert annotate.normalize_label(" Slow Burn ") == "slow-burn"
    assert annotate.normalize_label("a/b.c") == "a-b-c"
    assert annotate.normalize_label("1-1") == "1-1"
    assert annotate.normalize_label("") is None
    assert annotate.normalize_label(3) is None
