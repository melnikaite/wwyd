# SPDX-License-Identifier: AGPL-3.0-or-later
"""Privacy canary for ingest_raw (SPEC §2 P1/P2, §13).

Unique strings go into every free-text field of every episode shape (situation, decision,
said, where) and into audience; a fake LLM echoes its input back as claim texts, proposed
categories and category descriptions. Neither string may appear anywhere afterwards.
Category paths are client-supplied labels like audience names (SPEC §2 residual risk): they
carry no canary here.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from conftest import call
from ingest_data import calendar

from wwyd.ingest import jobs
from wwyd.logs import configure_logging
from wwyd.store.audiences import upsert_audience

pytestmark = pytest.mark.anyio

TOKEN = "zq7x-canary-41b9"
NAME = "Velmorine"
CANARIES = (TOKEN, NAME)


@pytest.fixture
def captured(caplog):
    configure_logging("DEBUG")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s %(args)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    caplog.set_level(logging.DEBUG)
    yield lambda: stream.getvalue() + "\n" + caplog.text
    root.removeHandler(handler)
    for h in list(root.handlers):
        if getattr(h, "_wwyd", False):
            root.removeHandler(h)
    root.setLevel(logging.WARNING)


def episodes():
    say = f"please ask {NAME} about {TOKEN} before the call"
    reply = f"ok, will ping {NAME} and check {TOKEN} later"
    ts = "2026-02-03T10:00:00+01:00"
    span = {"start": ts, "end": "2026-02-03T10:04:00+01:00"}
    return [
        # chat reply (unknown audience carries a canary)
        {
            "when": span,
            "where": f"{TOKEN} chat",
            "audience": NAME,
            "category": "chat",
            "situation": say,
            "said": reply,
        },
        {
            "when": ts,
            "where": NAME,
            "audience": "work-slack",
            "category": "chat/work",
            "situation": reply,
            "said": reply,
        },
        # call: others' turns in situation, the owner's in said
        {
            "when": span,
            "audience": TOKEN,
            "category": "work/calls",
            "situation": say,
            "said": reply,
        },
        # choices, with and without a situation
        {
            "when": ts,
            "audience": NAME,
            "category": "work/meetings",
            "situation": say,
            "decision": reply,
            "where": f"office of {NAME}",
        },
        {"category": "work/meetings", "situation": reply, "decision": say},
        {"category": "hobbies/films", "decision": f"watched {NAME} twice, {TOKEN}"},
        # statements about self
        {
            "when": ts,
            "category": "food",
            "said": f"My friend {NAME} gave me the code {TOKEN} to remember.",
        },
        {"category": "food", "said": say},
        # reply and decision at once
        {"category": "chat", "situation": say, "said": reply, "decision": say},
    ] + calendar()


def echo(call):
    """Echo the input back: the whole prompt, then line by line."""
    user = call["user"]
    if call["purpose"] == "describe_categories":
        paths = [line[2:] for line in user.splitlines()]
        return {
            "descriptions": [
                {"path": p, "description": d}
                for p in paths
                for d in (f"Films seen with {NAME}.", f"Codes like {TOKEN}.", user[:100])
            ]
        }
    texts = [user[:500]] + [line[:500] for line in user.splitlines() if line.strip()][:6]
    texts.append(f"Often works with {NAME}.")
    texts.append(f"Uses the code {TOKEN.upper()} at work.")
    return {
        "claims": [
            {
                "kind": "decision_rule" if call["purpose"] == "decisions" else "style",
                "category": "communication/style",
                "proposed_category": f"people/{NAME.lower()}",
                "text": t,
                "confidence": 0.9,
                "support": 2,
            }
            for t in texts
        ]
    }


def assert_clean(blob: str | bytes, where: str) -> None:
    for c in CANARIES:
        for variant in (c, c.lower(), c.upper()):
            needle = variant.encode() if isinstance(blob, bytes) else variant
            assert needle not in blob, f"{c!r} found in {where}"


async def test_ingest_canary(client, conn, pid, fake_llm, captured, tmp_path):
    upsert_audience(conn, pid, "work-slack")
    for purpose in ("statements", "replies", "decisions", "describe_categories"):
        fake_llm.on(purpose, echo)

    out = await call(client, "ingest_raw", {"episodes": episodes()})
    await jobs.wait(out["job_id"])
    status = await call(client, "job_status", {"job_id": out["job_id"]})
    assert status["status"] == "done"
    counts = status["counts"]
    assert counts["dropped_by_guard"] >= 10
    assert counts["unknown_audiences"] == 3
    assert counts["categories_created"] >= 3
    # The echo really reached the guard, through every pass and the description call.
    assert {c["purpose"] for c in fake_llm.calls} >= {
        "statements",
        "replies",
        "decisions",
        "describe_categories",
    }
    assert any(TOKEN in c["user"] for c in fake_llm.calls)
    assert any(NAME in c["user"] for c in fake_llm.by_purpose("decisions"))

    assert_clean(json.dumps(status), "job_status")
    listed = await call(client, "list_claims", {"status": "any", "limit": 200})
    assert_clean(json.dumps(listed, ensure_ascii=False), "list_claims")
    exported = await call(client, "export_persona")
    assert_clean(json.dumps(exported, ensure_ascii=False), "export_persona")
    settings = await call(client, "get_settings")
    assert_clean(json.dumps(settings, ensure_ascii=False), "get_settings")
    tree = await call(client, "list_categories")
    assert_clean(json.dumps(tree, ensure_ascii=False), "list_categories")
    changes = [dict(r) for r in conn.execute("SELECT * FROM changes")]
    assert_clean(json.dumps(changes, ensure_ascii=False), "changes")
    rows = [dict(r) for r in conn.execute("SELECT * FROM jobs")]
    assert_clean(json.dumps(rows, ensure_ascii=False), "jobs row")
    usage = [dict(r) for r in conn.execute("SELECT * FROM usage_daily")]
    assert_clean(json.dumps(usage, ensure_ascii=False), "usage_daily")

    db = tmp_path / "test.db"
    wal = tmp_path / "test.db-wal"
    assert wal.exists()
    assert_clean(wal.read_bytes(), "-wal before checkpoint")
    assert_clean(db.read_bytes(), "db file before checkpoint")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert_clean(db.read_bytes(), "db file after checkpoint")
    if wal.exists():
        assert_clean(wal.read_bytes(), "-wal after checkpoint")

    assert_clean(captured(), "logs")
