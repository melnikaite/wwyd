# SPDX-License-Identifier: AGPL-3.0-or-later
"""Routing (SPEC §8.1 step 4): one cheap call that sees only category paths and the situation."""

from __future__ import annotations

import logging

from wwyd.errors import LLMUnavailable
from wwyd.llm import LLM
from wwyd.predict.prompts import (
    ROUTE_SCHEMA,
    ROUTE_SYSTEM,
    WHEN_SCHEMA,
    WHEN_SYSTEM,
    route_user,
    when_user,
)

log = logging.getLogger(__name__)

MAX_ROUTES = 3


async def route(
    llm: LLM, categories: list[tuple[str, str]], situation: str
) -> tuple[list[str], int, str]:
    """Return (routed paths, tokens used, the situation's time in canonical English or "").
    An empty list means "fall back to FTS".

    Budget and spend errors propagate (the answer call would hit them too); a backend that
    is down or replies with junk only costs the routing step.
    """
    known = {path for path, _ in categories}
    try:
        result = await llm.complete_json(
            purpose="route",
            system=ROUTE_SYSTEM,
            user=route_user(categories, situation),
            schema=ROUTE_SCHEMA,
            max_tokens=256,
        )
    except LLMUnavailable:
        log.info("routing call failed; using full-text fallback")
        return [], 0, ""
    tokens = result.tokens_in + result.tokens_out
    data = result.data
    raw = data.get("categories") if isinstance(data, dict) else data
    when = _when(data)
    if not isinstance(raw, list):
        return [], tokens, when
    paths: list[str] = []
    for item in raw:
        if isinstance(item, str):
            path = item.strip().strip("/").lower()
            if path in known and path not in paths:
                paths.append(path)
        if len(paths) >= MAX_ROUTES:
            break
    return paths, tokens, when


def _when(data: object) -> str:
    value = data.get("when") if isinstance(data, dict) else None
    return value.strip()[:64] if isinstance(value, str) else ""


async def extract_when(llm: LLM, situation: str) -> tuple[str, int]:
    """The situation's time in canonical English (SPEC §8.1 step 4), for when routing is
    skipped: ("", tokens) when there is none. A failing backend only costs the When line;
    budget and spend errors propagate like routing's."""
    try:
        result = await llm.complete_json(
            purpose="when",
            system=WHEN_SYSTEM,
            user=when_user(situation),
            schema=WHEN_SCHEMA,
            max_tokens=64,
        )
    except LLMUnavailable:
        log.info("time reference call failed; no When line")
        return "", 0
    return _when(result.data), result.tokens_in + result.tokens_out
