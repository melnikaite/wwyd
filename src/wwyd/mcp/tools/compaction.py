# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compaction tools (SPEC §6.4): compact, list_changes, undo_change."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from wwyd.compact import compact as run_compact
from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import Cursor, Id
from wwyd.store import changes as store

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer

Step = Literal[
    "dedupe", "contradictions", "decay", "budget", "purge", "category_merge", "category_split"
]
State = Literal["applied", "proposed", "dismissed", "undone", "expired"]
Op = Literal[
    "merge",
    "absorb",
    "supersede",
    "decay",
    "archive_budget",
    "purge",
    "restore",
    "category_create",
    "category_merge",
    "category_rename",
    "category_split",
]


def register(server: WwydServer) -> None:
    owner = ("owner",)

    def persona_id() -> str:
        return current_caller().persona_id

    @server.scoped_tool(owner)
    async def compact(
        apply: Annotated[
            bool, Field(description="Write the changes. Default false: only report them.")
        ] = False,
        steps: Annotated[
            list[Step] | None, Field(description="Subset of steps to run (default: all).")
        ] = None,
    ) -> dict[str, Any]:
        """Consolidate claims: merge duplicates, resolve contradictions, decay stale claims,
        enforce the size budget, purge claims archived over 30 days ago, merge synonym categories
        and split oversized ones (both applied automatically, undoable with undo_change).
        Changes to approved claims come back as proposed (approve them with review_claims).
        With apply=false nothing is written and no LLM is called: borderline pairs are
        counted (judge_pending) and category candidates listed (category_pending)."""
        return await run_compact(server.conn, persona_id(), apply=apply, steps=steps)

    @server.scoped_tool(owner, read_only=True)
    async def list_changes(
        state: State | None = None,
        op: Op | None = None,
        limit: Annotated[int | None, Field(description="Page size, 1-200 (default 50).")] = None,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """The change log, newest first, with before/after claim snapshots."""
        return store.list_changes(
            server.conn, persona_id(), state=state, op=op, limit=limit, cursor=cursor
        )

    @server.scoped_tool(owner)
    async def undo_change(id: Id) -> dict[str, Any]:
        """Restore the before-snapshot of an applied change. Fails with conflict if a later
        applied change touched the same claims (undo that one first)."""
        return {"change": store.undo_change(server.conn, persona_id(), id)}
