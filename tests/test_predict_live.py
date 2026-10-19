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
