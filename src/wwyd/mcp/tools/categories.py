# SPDX-License-Identifier: AGPL-3.0-or-later
"""Category tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import Description, Path, Paths
from wwyd.store import categories as store

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


def register(server: WwydServer) -> None:
    owner = ("owner",)

    @server.scoped_tool(owner, read_only=True)
    async def list_categories() -> dict[str, Any]:
        """Category tree with claim_count (pending/approved claims directly in the category),
        total_claim_count (including sub-categories) and aliases (old paths that still
        route incoming episodes here after a rename or merge)."""
        return {"categories": store.list_categories(server.conn, current_caller().persona_id)}

    @server.scoped_tool(owner)
    async def upsert_category(
        path: Annotated[Path, Field(description="e.g. work/meetings; at most 3 levels.")],
        description: Description | None = None,
        rename_to: Annotated[
            Path | None, Field(description="New path; moves the whole subtree.")
        ] = None,
    ) -> dict[str, Any]:
        """Create a category (its parent must exist) or update its description or path.
        A rename leaves the old paths as aliases; undo it with undo_change."""
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
            Paths, Field(alias="from", description="Paths to merge (with sub-categories).")
        ],
        into: Path,
    ) -> dict[str, Any]:
        """Move all claims and analyzers of the given categories into another one and delete
        the sources; their paths become aliases. Recorded in the change log (undo_change)."""
        return store.merge_categories(server.conn, current_caller().persona_id, from_, into)
