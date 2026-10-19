# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persona use: predict, get_persona; and the predict audit log (owner)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import Cursor, Name, Paths
from wwyd.predict import predict as run_predict
from wwyd.predict.persona import get_persona as build_persona
from wwyd.store import audit

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


def register(server: WwydServer) -> None:
    @server.scoped_tool(("owner", "respond"))
    async def predict(
        situation: Annotated[
            str,
            Field(
                min_length=1,
                max_length=8000,
                description="The message or situation, as the owner would see it.",
            ),
        ],
        audience: Annotated[
            str, Field(max_length=100, description="Audience name; unknown -> default.")
        ],
        context: Annotated[
            str | None,
            Field(
                max_length=2000,
                description="What the client found out, in plain words (e.g. 'free today "
                "from 19:30'), not raw data. Used for this call only; never stored.",
            ),
        ] = None,
        kind: Literal["reply", "decision"] = "reply",
        include_private: Annotated[
            bool, Field(description="Owner only: also use owner_only claims (drafts).")
        ] = False,
    ) -> dict[str, Any]:
        """Predict what the owner would reply or decide.

        Returns type "answer" (answer, depends_on, confidence, disclosure to show with it,
        used_claims) or "escalate" with a code: low_confidence, out_of_persona, boundary,
        rate_limited, spend_cap, llm_unavailable, audience_not_permitted. `answer` is
        always sendable as is in the owner's voice; when the owner's real answer depends
        on things wwyd cannot know (free time, weather, ...) it stays non-committal and
        `depends_on` says in plain words what it depends on (null when nothing does).
        Send `answer` as is, refine it with your own tools, or call again with the same
        situation and what you found out in `context` for the owner's wording; the server
        keeps no state. Situations and context are sent to the persona's LLM backend and
        are never stored.

        Every result carries `prediction_id` (the audit row ID). If the owner would have
        acted differently, an owner client sends a correction episode: the same
        situation with the owner's version in `said`/`decision` and `corrects:
        <prediction_id>`. Unknown or expired IDs are ignored.
        """
        return await run_predict(
            server.conn,
            current_caller(),
            situation=situation,
            audience=audience,
            context=context,
            kind=kind,
            include_private=include_private,
        )

    @server.scoped_tool(("owner", "read"), read_only=True)
    async def get_persona(
        audience: Annotated[Name | None, Field(description="Only claims that apply to it.")] = None,
        categories: Annotated[
            Paths | None, Field(description="Category paths; sub-categories included.")
        ] = None,
        max_tokens: Annotated[int, Field(description="Budget, estimated as chars/4.")] = 1500,
        format: Literal["text", "json"] = "text",
    ) -> dict[str, Any]:
        """Compact description of the owner for a system prompt: boundaries, style, then
        the other approved claims grouped by category, strongest first."""
        return build_persona(
            server.conn,
            current_caller().persona_id,
            audience=audience,
            categories=categories,
            max_tokens=max_tokens,
            format=format,
        )

    @server.scoped_tool(("owner",), read_only=True)
    async def list_audit(
        since: Annotated[Name | None, Field(description="ISO 8601 timestamp.")] = None,
        audience: Name | None = None,
        outcome: Annotated[
            Name | None,
            Field(description='answer | needs | escalate | "escalate:<code>".'),
        ] = None,
        limit: Annotated[int | None, Field(description="Page size, 1-200 (default 50).")] = None,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """predict audit log, newest first (kept 90 days; never contains any text)."""
        return audit.list_audit(
            server.conn,
            current_caller().persona_id,
            since=since,
            audience=audience,
            outcome=outcome,
            limit=limit,
            cursor=cursor,
        )
