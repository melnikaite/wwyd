# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from mcp import Client

from wwyd.db import open_db
from wwyd.mcp.caller import Caller, fixed_caller
from wwyd.mcp.server import WwydServer, build_server
from wwyd.store.personas import UserPersona, create_user


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Every test gets its own data dir; nothing ever touches ~/.wwyd."""
    path = tmp_path / "data"
    monkeypatch.setenv("WWYD_DATA_DIR", str(path))
    return path


@pytest.fixture
def conn(tmp_path) -> Iterator[Any]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def user(conn) -> UserPersona:
    return create_user(conn, "owner@example.com")


@pytest.fixture
def pid(user) -> str:
    return user.persona_id


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_server(conn, user: UserPersona, scope: str = "owner", **kw: Any) -> WwydServer:
    caller = Caller(user.user_id, user.persona_id, scope, **kw)  # type: ignore[arg-type]
    return build_server(conn, fixed_caller(caller))


@pytest.fixture
async def client(conn, user) -> AsyncIterator[Client]:
    async with Client(make_server(conn, user)) as c:
        yield c


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool and return structured content; asserts it is not an error."""
    result = await client.call_tool(name, args or {})
    assert not result.is_error, result.structured_content or result.content
    assert result.structured_content is not None
    # The text block mirrors the structured content.
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content


async def call_error(client: Client, name: str, args: dict[str, Any] | None = None) -> dict:
    result = await client.call_tool(name, args or {})
    assert result.is_error
    return result.structured_content["error"]


@pytest.fixture
def fake_llm() -> Iterator[Any]:
    """Route every persona's LLM calls to a FakeLLM for the test."""
    from fake_llm import FakeLLM

    from wwyd.llm import set_llm_factory

    fake = FakeLLM()
    set_llm_factory(fake.for_persona)
    yield fake
    set_llm_factory(None)


@pytest.fixture(autouse=True)
def no_live_llm(request, monkeypatch):
    """Outside ``-m llm`` the default backend points at a closed port, so a test that
    forgets ``fake_llm`` fails the same way locally as in CI instead of reaching LocalAI."""
    if request.node.get_closest_marker("llm") or request.node.get_closest_marker("real_presets"):
        return
    from dataclasses import replace

    from wwyd.llm import presets

    closed = replace(presets._DEFAULT_INSTANCE, base_url="http://127.0.0.1:9/v1")
    monkeypatch.setattr(presets, "_DEFAULT_INSTANCE", closed)
    monkeypatch.setattr(presets, "_instance", closed)
    monkeypatch.setitem(presets.PRESETS, "localai", ("http://127.0.0.1:9/v1", closed.model))
