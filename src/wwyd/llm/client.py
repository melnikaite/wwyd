# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAI-compatible chat-completions client with the robustness kit (SPEC §12).

Never logs prompts, responses or response bodies; error messages carry only status codes.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx

from wwyd.errors import Invalid, LLMUnavailable, RateLimited, SpendCap
from wwyd.limits import estimate_cost, get_gate, get_limiter
from wwyd.llm import LLMResult, Purpose
from wwyd.llm.presets import Backend

log = logging.getLogger(__name__)

MIN_MAX_TOKENS = 1024  # small models wander; a short budget cuts JSON off mid-object
PREFLIGHT_TIMEOUT_S = 10.0

_transport: httpx.AsyncBaseTransport | None = None
_sync_transport: httpx.BaseTransport | None = None


def set_transport(
    transport: httpx.AsyncBaseTransport | None, sync: httpx.BaseTransport | None = None
) -> None:
    """Route LLM HTTP traffic through these transports (tests: ``httpx.MockTransport``);
    ``sync`` is used by the preflight."""
    global _transport, _sync_transport
    _transport, _sync_transport = transport, sync


# --- robustness kit -------------------------------------------------------------------------

_TEMPLATE_MARKERS = re.compile(
    r"<\|[^|<>\n]{1,40}\|>"  # <|im_start|>, <|eot_id|>, <|start_header_id|> ...
    r"|</?(?:s|think|start_of_turn|end_of_turn|bos|eos)>"
    r"|\[/?INST\]|<<SYS>>|<</SYS>>",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def sanitize(text: str) -> str:
    """Remove chat-template control markers from text that goes into a prompt."""
    return _TEMPLATE_MARKERS.sub(" ", text)


def _strip(text: str) -> str:
    text = _THINK.sub("", text).strip()
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def _first_json(text: str) -> Any:
    """Decode the first JSON object or array in ``text``."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return decoder.raw_decode(text, i)[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON value found")


def _wrap_array(data: Any, schema: dict[str, Any]) -> Any:
    """A bare array where the schema asks for an object with one array field gets wrapped."""
    if not isinstance(data, list) or schema.get("type") != "object":
        return data
    arrays = [k for k, v in (schema.get("properties") or {}).items() if v.get("type") == "array"]
    if len(arrays) == 1:
        return {arrays[0]: data}
    return {"items": data}


def parse_json(text: str, schema: dict[str, Any]) -> Any:
    """Apply the kit's repairs and decode; raises ValueError when nothing parses."""
    cleaned = _strip(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = _first_json(cleaned)
    return _wrap_array(data, schema)


def _message_text(payload: dict[str, Any]) -> str:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = message.get("content") or ""
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not str(content).strip():
        content = message.get("reasoning_content") or message.get("reasoning") or ""
    return str(content)


def _usage(payload: dict[str, Any], backend: Backend) -> tuple[int, int, float | None]:
    usage = payload.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    cost = usage.get("cost")
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        return tokens_in, tokens_out, float(cost)
    return (
        tokens_in,
        tokens_out,
        estimate_cost(tokens_in, tokens_out, backend.price_in_per_m, backend.price_out_per_m),
    )


def _headers(backend: Backend) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if backend.api_key:
        headers["Authorization"] = f"Bearer {backend.api_key}"
    return headers


def _retry_after(response: httpx.Response, default: int = 30) -> int:
    try:
        return max(1, int(float(response.headers.get("retry-after", default))))
    except ValueError:
        return default


# --- client -----------------------------------------------------------------------------------


class OpenAICompatClient:
    def __init__(
        self, conn: sqlite3.Connection, user_id: str, persona_id: str, backend: Backend
    ) -> None:
        self.conn = conn
        self.user_id = user_id
        self.persona_id = persona_id
        self.backend = backend

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=_transport, timeout=httpx.Timeout(120.0, connect=10.0)
        ) as http:
            return await http.post(
                f"{self.backend.base_url}/chat/completions",
                json=body,
                headers=_headers(self.backend),
            )

    async def _call(self, body: dict[str, Any]) -> dict[str, Any]:
        """One HTTP round trip through the gate; returns the decoded payload, records usage."""
        get_limiter(self.conn).check_llm_budget(self.user_id, self.persona_id)
        try:
            response = await get_gate().run(self._post(body))
            if response.status_code == 400 and "response_format" in body:
                # Some servers reject json_schema; the schema is also in the prompt.
                body = {k: v for k, v in body.items() if k != "response_format"}
                response = await get_gate().run(self._post(body))
        except httpx.HTTPError as exc:
            log.warning("LLM backend unreachable (%s)", type(exc).__name__)
            raise LLMUnavailable("LLM backend unreachable") from None
        status = response.status_code
        if status == 429:
            raise RateLimited("LLM backend rate limit", retry_after=_retry_after(response))
        if status == 402:
            raise SpendCap("LLM backend reports insufficient credit")
        if status >= 400:
            log.warning("LLM backend returned HTTP %d", status)
            raise LLMUnavailable("LLM backend error", status=status)
        try:
            payload = response.json()
        except ValueError:
            raise LLMUnavailable("LLM backend returned a non-JSON body") from None
        if not isinstance(payload, dict):
            raise LLMUnavailable("LLM backend returned an unexpected body")
        tokens_in, tokens_out, cost = _usage(payload, self.backend)
        get_limiter(self.conn).record_llm_usage(
            self.user_id, self.persona_id, tokens_in, tokens_out, cost
        )
        payload["_wwyd_usage"] = (tokens_in, tokens_out, cost)
        return payload

    async def complete_json(
        self,
        *,
        purpose: Purpose,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 2048,
    ) -> LLMResult:
        if not self.backend.model:
            raise LLMUnavailable("no model configured for the LLM backend")
        system_text = (
            sanitize(system)
            + "\n\nReply with one JSON value only, no prose and no Markdown, matching this JSON"
            " schema (every field is required):\n" + json.dumps(schema, separators=(",", ":"))
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": sanitize(user)},
        ]
        body: dict[str, Any] = {
            "model": self.backend.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max(max_tokens, MIN_MAX_TOKENS),
            "reasoning_effort": "none",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": purpose, "strict": True, "schema": schema},
            },
        }
        totals = [0, 0, None]
        for attempt in range(2):  # one repair retry
            payload = await self._call(body)
            t_in, t_out, cost = payload.pop("_wwyd_usage")
            totals[0] += t_in
            totals[1] += t_out
            if cost is not None:
                totals[2] = (totals[2] or 0.0) + cost
            text = _message_text(payload)
            try:
                data = parse_json(text, schema)
                return LLMResult(
                    data=data, tokens_in=totals[0], tokens_out=totals[1], cost_usd=totals[2]
                )
            except ValueError:
                log.info("LLM reply for %s was not valid JSON (attempt %d)", purpose, attempt + 1)
                body = {
                    **body,
                    "messages": [
                        *messages,
                        {"role": "assistant", "content": sanitize(text)[:4000]},
                        {
                            "role": "user",
                            "content": "That was not valid JSON. Reply again with only the"
                            " JSON object, nothing else.",
                        },
                    ],
                }
        raise LLMUnavailable("LLM reply could not be parsed as JSON")


# --- persona wiring and preflight ------------------------------------------------------------


def client_for_persona(conn: sqlite3.Connection, persona_id: str) -> OpenAICompatClient:
    from wwyd.store.personas import llm_backend

    row = conn.execute("SELECT user_id FROM personas WHERE id = ?", (persona_id,)).fetchone()
    if row is None:
        raise LLMUnavailable("persona not found")
    return OpenAICompatClient(conn, row["user_id"], persona_id, llm_backend(conn, persona_id))


Preflight = Callable[[Backend], None]


def http_preflight(backend: Backend) -> None:
    """``GET /models``; raises :class:`Invalid` when the backend is unusable."""
    try:
        with httpx.Client(transport=_sync_transport, timeout=PREFLIGHT_TIMEOUT_S) as http:
            response = http.get(f"{backend.base_url}/models", headers=_headers(backend))
    except httpx.HTTPError:
        raise Invalid("LLM backend is not reachable", field="llm") from None
    if response.status_code in (401, 403):
        raise Invalid("LLM backend rejected the API key", field="llm")
    if response.status_code >= 400:
        raise Invalid("LLM backend preflight failed", field="llm", status=response.status_code)
    try:
        models = [m.get("id") for m in response.json().get("data") or [] if isinstance(m, dict)]
    except (ValueError, AttributeError):
        models = []
    if backend.model and models and backend.model not in models:
        raise Invalid("model not offered by the LLM backend", field="llm.model")


_preflight: Preflight | None = http_preflight


def set_preflight(fn: Preflight | None) -> None:
    """Replace the preflight (tests); ``None`` skips it."""
    global _preflight
    _preflight = fn


def reset_preflight() -> None:
    set_preflight(http_preflight)


def preflight(backend: Backend) -> None:
    if _preflight is not None:
        _preflight(backend)
