# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic stand-in for the LLM backend (installed by the ``fake_llm`` fixture)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from wwyd.llm import LLMResult

Handler = Callable[[dict[str, Any]], Any]


class FakeLLM:
    """Answers by purpose. ``responses[purpose]`` is a value or ``handler(call) -> value``;
    raising inside a handler simulates a failing backend. Every call is recorded in
    ``calls`` (purpose, system, user, schema, persona_id) so tests can assert on exactly
    what the model was shown."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def on(self, purpose: str, response: Any | Handler) -> None:
        self.responses[purpose] = response

    def for_persona(self, conn: Any, persona_id: str) -> _Bound:
        return _Bound(self, persona_id)

    def by_purpose(self, purpose: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["purpose"] == purpose]


class _Bound:
    def __init__(self, fake: FakeLLM, persona_id: str) -> None:
        self.fake = fake
        self.persona_id = persona_id

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 2048,
    ) -> LLMResult:
        call = {
            "purpose": purpose,
            "system": system,
            "user": user,
            "schema": schema,
            "persona_id": self.persona_id,
        }
        self.fake.calls.append(call)
        if purpose not in self.fake.responses:
            raise AssertionError(f"FakeLLM has no response for purpose {purpose!r}")
        response = self.fake.responses[purpose]
        data = response(call) if callable(response) else response
        return LLMResult(data=data, tokens_in=len(system + user) // 4, tokens_out=10)
