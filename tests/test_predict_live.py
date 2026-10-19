# SPDX-License-Identifier: AGPL-3.0-or-later
"""Against a real LocalAI (``uv run pytest -m llm``)."""

from __future__ import annotations

import pytest
from test_predict import MEETING, STYLE, owner, seed

from wwyd.predict import predict

pytestmark = [pytest.mark.llm, pytest.mark.anyio]


async def test_live_predict_localai(conn, user, pid):
    seed(conn, pid, [STYLE, MEETING])
    out = await predict(
        conn, owner(user), situation="Can we do a 30 minute call on Tuesday?", audience="everyone"
    )
    assert out["type"] in ("answer", "needs", "escalate")
    if out["type"] == "escalate":
        assert out["escalate"] != "llm_unavailable", out
    if out["type"] == "answer":
        assert out["disclosure"]


async def test_live_needs_round_trip(conn, user, pid):
    seed(
        conn,
        pid,
        [
            STYLE,
            {
                **MEETING,
                "text": "Accepts a meeting only if there is a free slot of at least 20 minutes "
                "and 10 minutes of buffer before and after other calls.",
            },
        ],
    )
    situation = "Can you join a call tomorrow at 3pm?"
    first = await predict(conn, owner(user), situation=situation, audience="everyone")
    assert first["type"] == "needs", first
    assert first["needs"] and all(n["what"] for n in first["needs"])
    facts = [
        {"what": "free slots tomorrow", "value": "14:00-16:00"},
        {"what": "other calls tomorrow", "value": "none between 13:00 and 17:00"},
    ]
    second = await predict(conn, owner(user), situation=situation, audience="everyone", facts=facts)
    assert second["type"] == "answer", second
