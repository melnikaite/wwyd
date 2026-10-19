# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json

import pytest
from conftest import call, make_server
from mcp import Client
from test_predict import seed

from wwyd.errors import Invalid, NotFound
from wwyd.predict.persona import get_persona
from wwyd.store.audiences import upsert_audience

pytestmark = pytest.mark.anyio


def persona_set(conn, pid):
    upsert_audience(conn, pid, "work")
    upsert_audience(conn, pid, "family")
    seed(
        conn,
        pid,
        [
            {"kind": "habit", "category": "food", "text": "Eats late.", "confidence": 0.5},
            {"kind": "trait", "category": "work/decisions", "text": "Decides fast.", "confidence": 0.9},
            {"kind": "style", "category": "communication/style", "text": "Short replies."},
            {"kind": "boundary", "category": "communication/boundaries", "text": "No salary talk."},
            {"kind": "preference", "category": "food", "text": "Likes ramen.", "confidence": 0.95},
            {"kind": "style", "category": "communication/style", "text": "Formal at work.",
             "audiences": ["work"]},
            {"kind": "trait", "category": "family", "text": "Private family note.",
             "visibility": "owner_only", "audiences": ["family"]},
        ],
    )  # fmt: skip


def test_order_and_grouping(conn, pid):
    persona_set(conn, pid)
    out = get_persona(conn, pid)
    text = out["text"]
    assert text.index("Boundaries:") < text.index("Style:") < text.index("food:")
    # Groups by best claim: food (0.95) before work/decisions (0.9) before family.
    assert text.index("food:") < text.index("work/decisions:") < text.index("family:")
    assert text.index("Likes ramen.") < text.index("Eats late.")
    assert "Private family note." in text  # owner/read view includes owner_only
    assert out == {"text": text, "claims_used": 7, "truncated": False}


def test_audience_and_categories(conn, pid):
    persona_set(conn, pid)
    family = get_persona(conn, pid, audience="family")["text"]
    assert "Formal at work." not in family and "Private family note." in family
    work = get_persona(conn, pid, audience="work", categories=["communication"])
    assert "Formal at work." in work["text"] and "ramen" not in work["text"]
    assert work["claims_used"] == 3
    with pytest.raises(NotFound):
        get_persona(conn, pid, audience="nope")


def test_truncation_and_json(conn, pid):
    persona_set(conn, pid)
    seed(conn, pid, [{"kind": "trait", "category": "hobbies", "text": "Plays chess. " * 20}])
    out = get_persona(conn, pid, max_tokens=50)
    assert out["truncated"] is True and 0 < out["claims_used"] < 8
    assert len(out["text"]) <= 200
    assert out["text"].startswith("Boundaries:")
    data = json.loads(get_persona(conn, pid, format="json")["text"])
    assert data[0]["section"] == "Boundaries" and data[0]["claims"][0]["kind"] == "boundary"
    with pytest.raises(Invalid):
        get_persona(conn, pid, max_tokens=10)


async def test_read_scope_tool(conn, user, pid):
    persona_set(conn, pid)
    async with Client(make_server(conn, user, "read")) as c:
        assert {t.name for t in (await c.list_tools()).tools} == {"get_persona"}
        out = await call(c, "get_persona", {"max_tokens": 1500})
        assert out["claims_used"] == 7
