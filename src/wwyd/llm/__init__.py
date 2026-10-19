# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM access (SPEC §12). Every caller goes through ``get_llm(conn, persona_id)``.

The returned object owns the whole call path: budget and spend checks (§11), the global
concurrency gate, the robustness kit, usage recording. Callers only build prompts and read
the parsed JSON. Errors: ``RateLimited`` (token budget), ``SpendCap``, ``LLMUnavailable``
(connection, timeout, queue timeout, unparseable output after the kit's repairs).

Tests install a fake with ``set_llm_factory`` (see ``tests/fake_llm.py``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Purpose = Literal[
    "route", "answer", "judge", "statements", "replies", "decisions", "describe_categories"
]


@dataclass(frozen=True)
class LLMResult:
    data: Any  # parsed JSON (object or array)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None


class LLM(Protocol):
    async def complete_json(
        self,
        *,
        purpose: Purpose,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 2048,
    ) -> LLMResult: ...


LLMFactory = Callable[[sqlite3.Connection, str], LLM]
_factory: LLMFactory | None = None


def set_llm_factory(factory: LLMFactory | None) -> None:
    """Override how personas get their LLM (tests); ``None`` restores the real client."""
    global _factory
    _factory = factory


def get_llm(conn: sqlite3.Connection, persona_id: str) -> LLM:
    if _factory is not None:
        return _factory(conn, persona_id)
    from wwyd.llm.client import client_for_persona

    return client_for_persona(conn, persona_id)
