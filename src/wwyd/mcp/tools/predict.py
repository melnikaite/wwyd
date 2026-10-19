# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persona use: predict, get_persona; and the predict audit log (owner)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from wwyd.mcp.caller import current_caller
from wwyd.predict import predict as run_predict
from wwyd.predict.persona import get_persona as build_persona
from wwyd.store import audit

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


class Fact(BaseModel):
    """A fact the harness looked up, e.g. after a ``needs`` reply. Used for this call only."""

    model_config = ConfigDict(extra="forbid")

    what: str = Field(max_length=500)
    value: str = Field(max_length=2000)


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
        facts: Annotated[
            list[Fact] | None, Field(max_length=20, description="Answers to earlier needs.")
        ] = None,
        kind: Literal["reply", "decision"] = "reply",
        include_private: Annotated[
            bool, Field(description="Owner only: also use owner_only claims (drafts).")
        ] = False,
    ) -> dict[str, Any]:
        """Predict what the owner would reply or decide.

        Returns type "answer" (answer, confidence, disclosure to show with it, used_claims),
        "needs" (facts to look up and pass back in `facts`; the server keeps no state) or
        "escalate" with a code: low_confidence, out_of_persona, boundary, rate_limited,
        spend_cap, llm_unavailable, audience_not_permitted. Situations are sent to the
        persona's LLM backend and are never stored.
        """
        return await run_predict(
            server.conn,
            current_caller(),
            situation=situation,
            audience=audience,
            facts=[f.model_dump() for f in facts or []],
            kind=kind,
            include_private=include_private,
        )

    @server.scoped_tool(("owner", "read"), read_only=True)
    async def get_persona(
        audience: Annotated[str | None, Field(description="Only claims that apply to it.")] = None,
        categories: Annotated[
            list[str] | None, Field(description="Category paths; sub-categories included.")
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
        since: Annotated[str | None, Field(description="ISO 8601 timestamp.")] = None,
        audience: str | None = None,
        outcome: Annotated[
            str | None,
            Field(description='answer | needs | escalate | "escalate:<code>".'),
        ] = None,
        limit: Annotated[int | None, Field(description="Page size, 1-200 (default 50).")] = None,
        cursor: str | None = None,
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
