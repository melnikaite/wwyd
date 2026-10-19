# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCPServer subclass with per-caller tool filtering and stable error codes."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from mcp.server import MCPServer
from mcp.server.caching import CacheHint
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from wwyd import __version__
from wwyd.errors import Forbidden, Invalid, NotFound, RateLimited, TooLarge, WwydError
from wwyd.limits import get_limiter
from wwyd.logs import install_privacy_filter
from wwyd.mcp.caller import Caller, CallerProvider, bind_caller, unbind_caller

log = logging.getLogger("wwyd.mcp")

INSTRUCTIONS = (
    "wwyd holds one person's persona as small claims (traits, preferences, habits, decision "
    "rules, style, boundaries) grouped by category and audience. Owners curate claims with "
    "add_claims, review_claims, update_claim and search_claims."
)


def _aliases(fn: Callable[..., Any]) -> dict[str, str]:
    """Map pydantic ``Field(alias=...)`` names to parameter names (e.g. ``from`` -> ``from_``)."""
    out = {}
    for name, param in inspect.signature(fn, eval_str=True).parameters.items():
        for meta in getattr(param.annotation, "__metadata__", ()):
            if isinstance(meta, FieldInfo) and meta.alias:
                out[meta.alias] = name
    return out


def _payload_result(payload: dict[str, Any], *, is_error: bool) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=is_error,
    )


def rate_limited_escalation() -> CallToolResult:
    """What ``predict`` returns over the per-call rate limit (SPEC §8.1): no error, no LLM."""
    return _payload_result(
        {
            "type": "escalate",
            "escalate": "rate_limited",
            "reason": "too many requests; try again later",
            "hints": [],
        },
        is_error=False,
    )


def error_result(error: WwydError) -> CallToolResult:
    return _payload_result({"error": error.to_payload()}, is_error=True)


class WwydServer(MCPServer):
    """Tools declare the scopes allowed to see and call them; everything else is hidden."""

    def __init__(self, conn: sqlite3.Connection, caller_provider: CallerProvider, **kwargs: Any):
        install_privacy_filter()
        # tools/list depends on the caller's scope: never let a client reuse one caller's
        # list for another authorization context.
        kwargs.setdefault("cache_hints", {"tools/list": CacheHint(ttl_ms=0, scope="private")})
        super().__init__(name="wwyd", version=__version__, instructions=INSTRUCTIONS, **kwargs)
        self.conn = conn
        self._caller_provider = caller_provider
        self._tool_scopes: dict[str, frozenset[str]] = {}

    # --- registry -------------------------------------------------------------------

    def scoped_tool(
        self,
        scopes: Iterable[str],
        *,
        name: str | None = None,
        read_only: bool = False,
        destructive: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a tool callable only by callers whose scope is in ``scopes``."""
        allowed = frozenset(scopes)

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            aliases = _aliases(fn)
            target = fn
            if aliases:

                @functools.wraps(fn)
                async def target(**kwargs: Any) -> Any:
                    return await fn(**{aliases.get(k, k): v for k, v in kwargs.items()})

            self.add_tool(
                target,
                name=tool_name,
                description=inspect.getdoc(fn),
                annotations=ToolAnnotations(
                    read_only_hint=read_only, destructive_hint=destructive, open_world_hint=False
                ),
                structured_output=True,
            )
            self._tool_scopes[tool_name] = allowed
            return fn

        return decorator

    def tool_scopes(self) -> dict[str, frozenset[str]]:
        return dict(self._tool_scopes)

    def resolve_caller(self) -> Caller | None:
        """Single place that decides who is calling (stdio: fixed; M2: from the token)."""
        return self._caller_provider()

    def is_allowed(self, caller: Caller | None, tool_name: str) -> bool:
        scopes = self._tool_scopes.get(tool_name)
        return caller is not None and scopes is not None and caller.scope in scopes

    # --- MCPServer overrides --------------------------------------------------------

    async def list_tools(self):  # type: ignore[override]
        caller = self.resolve_caller()
        return [t for t in await super().list_tools() if self.is_allowed(caller, t.name)]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None):  # type: ignore[override]
        caller = self.resolve_caller()
        if name not in self._tool_scopes:
            return error_result(NotFound(f"unknown tool {name!r}"))
        if not self.is_allowed(caller, name):
            return error_result(Forbidden(f"tool {name!r} is not available with this access"))
        assert caller is not None
        try:
            get_limiter(self.conn).check_request(
                caller.user_id, caller.persona_id, caller.subject_id, name
            )
        except RateLimited as exc:
            return rate_limited_escalation() if name == "predict" else error_result(exc)
        token = bind_caller(caller)  # type: ignore[arg-type]
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            return self._map_error(name, exc)
        finally:
            unbind_caller(token)

    def _map_error(self, name: str, exc: ToolError) -> CallToolResult:
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, WwydError):
                return error_result(cause)
            if isinstance(cause, ValidationError):
                fields = sorted(
                    {
                        ".".join(str(p) for p in err["loc"]) or "(arguments)"
                        for err in cause.errors()
                    }
                )
                if any(err["type"] == "too_long" for err in cause.errors()):
                    # A list over its schema max_length: same code the store uses.
                    return error_result(
                        TooLarge(f"too many items: {', '.join(fields)}", fields=fields)
                    )
                return error_result(
                    Invalid(f"invalid arguments: {', '.join(fields)}", fields=fields)
                )
            cause = cause.__cause__
        log.error("tool %s failed unexpectedly", name, exc_info=exc)
        err = WwydError("internal error")
        err.code = "internal"  # type: ignore[assignment]
        return error_result(err)


def build_server(
    conn: sqlite3.Connection, caller_provider: CallerProvider, **kwargs: Any
) -> WwydServer:
    from wwyd.mcp.tools import register_all

    server = WwydServer(conn, caller_provider, **kwargs)
    register_all(server)
    return server
