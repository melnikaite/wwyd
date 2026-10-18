# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audience tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from wwyd.mcp.caller import current_caller
from wwyd.store import audiences as store

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


def register(server: WwydServer) -> None:
    owner = ("owner",)

    @server.scoped_tool(owner, read_only=True)
    async def list_audiences() -> dict[str, Any]:
        """List audiences. claim_count counts pending/approved claims assigned to the audience
        by name (claims for "all" are not counted)."""
        return {"audiences": store.list_audiences(server.conn, current_caller().persona_id)}

    @server.scoped_tool(owner)
    async def upsert_audience(
        name: Annotated[str, Field(description="Slug: [a-z0-9][a-z0-9_-]{0,39}.")],
        description: Annotated[
            str | None, Field(description="Who belongs here and how to talk to them.")
        ] = None,
        is_default: Annotated[
            bool | None,
            Field(description="Make this the default audience (exactly one exists)."),
        ] = None,
        rename_to: str | None = None,
    ) -> dict[str, Any]:
        """Create an audience or update its description, default flag or name."""
        return store.upsert_audience(
            server.conn,
            current_caller().persona_id,
            name,
            description=description,
            is_default=is_default,
            rename_to=rename_to,
        )

    @server.scoped_tool(owner, destructive=True)
    async def delete_audience(
        name: str,
        reassign_to: Annotated[
            str | None, Field(description="Audience for claims that applied only to this one.")
        ] = None,
    ) -> dict[str, Any]:
        """Delete a non-default audience. Fails with conflict (and the count) if claims apply
        only to it and reassign_to is not given."""
        return store.delete_audience(
            server.conn, current_caller().persona_id, name, reassign_to=reassign_to
        )
