# SPDX-License-Identifier: AGPL-3.0-or-later
"""Routing (SPEC §8.1 step 4): one cheap call that sees only category paths and the situation."""

from __future__ import annotations

import logging

from wwyd.errors import LLMUnavailable
from wwyd.llm import LLM
from wwyd.predict.prompts import ROUTE_SCHEMA, ROUTE_SYSTEM, route_user

log = logging.getLogger(__name__)

MAX_ROUTES = 3


async def route(
    llm: LLM, categories: list[tuple[str, str]], situation: str
) -> tuple[list[str], int]:
    """Return (routed paths, tokens used). An empty list means "fall back to FTS".

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
        return [], 0
    tokens = result.tokens_in + result.tokens_out
    data = result.data
    raw = data.get("categories") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return [], tokens
    paths: list[str] = []
    for item in raw:
        if isinstance(item, str):
            path = item.strip().strip("/").lower()
            if path in known and path not in paths:
                paths.append(path)
        if len(paths) >= MAX_ROUTES:
            break
    return paths, tokens
