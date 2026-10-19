# SPDX-License-Identifier: AGPL-3.0-or-later
"""Analyzer tools (SPEC §6.7), owner only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import Cursor, Id, Path
from wwyd.store import analyzers as store

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer

Status = Literal["candidate", "active", "disabled", "retired"]
Limit = Annotated[int | None, Field(description="Page size, 1-200 (default 50).")]


def register(server: WwydServer) -> None:
    owner = ("owner",)

    @server.scoped_tool(owner, read_only=True)
    async def list_analyzers(
        scope: Annotated[
            Path | None, Field(description="Exact category path or audience name.")
        ] = None,
        status: Status | None = None,
        limit: Limit = None,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """List analyzers (builtin and LLM), oldest first, paginated with next_cursor."""
        return store.list_analyzers(
            server.conn,
            current_caller().persona_id,
            scope=scope,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    @server.scoped_tool(owner, read_only=True)
    async def get_analyzer(id: Id) -> dict[str, Any]:  # noqa: A002
        """One analyzer with its statement template, source (LLM analyzers), keyed claim IDs
        and a state summary: metrics, months, key count per metric and every key."""
        return store.get_analyzer(server.conn, current_caller().persona_id, id)

    @server.scoped_tool(owner)
    async def update_analyzer(
        id: Id,  # noqa: A002
        status: Annotated[
            Literal["active", "disabled", "retired"],
            Field(
                description="disabled analyzers are skipped by ingest jobs; retired (LLM only) "
                "is final and clears the source, keeping the statement as negative memory."
            ),
        ],
    ) -> dict[str, Any]:
        """Activate, disable or retire an analyzer."""
        return store.update_analyzer(server.conn, current_caller().persona_id, id, status=status)
