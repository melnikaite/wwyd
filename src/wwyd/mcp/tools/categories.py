# SPDX-License-Identifier: AGPL-3.0-or-later
"""Category tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from wwyd.mcp.caller import current_caller
from wwyd.store import categories as store

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


def register(server: WwydServer) -> None:
    owner = ("owner",)

    @server.scoped_tool(owner, read_only=True)
    async def list_categories() -> dict[str, Any]:
        """Category tree with claim_count (pending/approved claims directly in the category)
        and total_claim_count (including sub-categories)."""
        return {"categories": store.list_categories(server.conn, current_caller().persona_id)}

    @server.scoped_tool(owner)
    async def upsert_category(
        path: Annotated[str, Field(description="e.g. work/meetings; at most 3 levels.")],
        description: str | None = None,
        rename_to: Annotated[
            str | None, Field(description="New path; moves the whole subtree.")
        ] = None,
    ) -> dict[str, Any]:
        """Create a category (its parent must exist) or update its description or path."""
        return store.upsert_category(
            server.conn,
            current_caller().persona_id,
            path,
            description=description,
            rename_to=rename_to,
        )

    @server.scoped_tool(owner, destructive=True)
    async def merge_categories(
        from_: Annotated[
            list[str], Field(alias="from", description="Paths to merge (with sub-categories).")
        ],
        into: str,
    ) -> dict[str, Any]:
        """Move all claims of the given categories into another one and delete the sources.
        Recorded in the change log."""
        return store.merge_categories(server.conn, current_caller().persona_id, from_, into)
