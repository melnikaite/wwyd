# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1: tool arguments and results never reach the logs, at any level."""

from __future__ import annotations

import io
import logging

import pytest
from conftest import make_server
from mcp import Client

from wwyd.logs import configure_logging

pytestmark = pytest.mark.anyio

CANARY = "zqxcanary7731"


@pytest.fixture
def captured(caplog):
    configure_logging("DEBUG")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    caplog.set_level(logging.DEBUG)
    yield lambda: stream.getvalue() + "\n" + caplog.text
    root.removeHandler(handler)
    for h in list(root.handlers):
        if getattr(h, "_wwyd", False):
            root.removeHandler(h)
    root.setLevel(logging.WARNING)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_canary_never_logged(conn, user, captured, monkeypatch, mode):
    server = make_server(conn, user)
    async with Client(server, mode=mode) as c:
        calls = [
            (
                "add_claims",
                {
                    "claims": [
                        {"kind": "trait", "category": "food", "text": f"Likes {CANARY} tea"},
                        {"kind": "trait", "category": "food", "text": f"Mail {CANARY}@example.com"},
                        {"kind": "trait", "category": f"{CANARY}", "text": "x"},
                    ]
                },
            ),
            ("search_claims", {"query": f"{CANARY} tea", "status": "any"}),
            ("list_claims", {"limit": CANARY}),  # pydantic error quotes the input value
            ("list_claims", {"cursor": CANARY}),
            ("import_persona", {"document": {"format": CANARY}}),
            ("delete_persona", {"confirm": CANARY}),
            ("update_settings", {"timezone": CANARY}),
            ("upsert_audience", {"name": CANARY.upper()}),
        ]
        for name, args in calls:
            await c.call_tool(name, args)

        def boom(*a, **k):
            raise RuntimeError(f"crash with {CANARY}")

        monkeypatch.setattr("wwyd.store.claims.search_claims", boom)
        result = await c.call_tool("search_claims", {"query": CANARY})
        assert result.is_error and result.structured_content["error"]["code"] == "internal"
        assert CANARY not in str(result.content)

    text = captured()
    assert "tool search_claims failed unexpectedly" in text  # the crash itself is logged
    assert "RuntimeError" in text
    assert CANARY not in text
    assert CANARY.upper() not in text


def test_sdk_records_are_sanitised(captured):
    configure_logging("DEBUG")
    logging.getLogger("mcp.server.test").debug("params %r", {"text": CANARY})
    logging.getLogger("mcp.server.test").warning("bad %s", CANARY)
    logging.getLogger("mcp.server.test").info(f"f-string {CANARY}")
    try:
        raise ValueError(CANARY)
    except ValueError:
        logging.getLogger("wwyd.test").exception("own failure")
    text = captured()
    assert "own failure" in text and "ValueError" in text
    assert "bad %s [arguments redacted]" in text
    assert CANARY not in text
