# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claim tools: add, list, search, update, delete, review."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import ClaimText, Cursor, Id, Ids, Name, Names, Path, Query
from wwyd.store import claims as store
from wwyd.store.claims import MAX_ADD

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer

Kind = Literal["trait", "preference", "habit", "decision_rule", "style", "boundary"]
Status = Literal["pending", "approved", "rejected", "archived"]
Visibility = Literal["owner_only", "shared"]
Audiences = Literal["all"] | Names
Limit = Annotated[int | None, Field(description="Page size, 1-200 (default 50).")]


class ClaimIn(BaseModel):
    """A claim to add. Items are validated one by one; bad items are dropped with a reason."""

    model_config = ConfigDict(extra="forbid")

    kind: Name = Field(description="trait | preference | habit | decision_rule | style | boundary")
    category: Path = Field(description="Existing category path, e.g. communication/style.")
    text: ClaimText = Field(
        description="One statement, <= 500 chars, owner in third person or neutral."
    )
    audiences: Audiences | None = Field(
        default=None, description='"all" (default) or a list of audience names it applies to.'
    )
    visibility: Name | None = Field(default=None, description="owner_only | shared (default).")
    confidence: float | None = Field(default=None, description="0..1, default 0.5.")
    evidence: dict[str, Any] | None = Field(
        default=None,
        description="Aggregates only: {sources: {type: count}, share, period: "
        '"2026-04..2026-10", stats: {name: number | [number]}}.',
    )
    status: Name | None = Field(default=None, description="pending (default) | approved.")
    proposed_category: Path | None = Field(
        default=None, description="Path of a category that does not exist yet (see review_claims)."
    )


def register(server: WwydServer) -> None:
    owner = ("owner",)

    def persona_id() -> str:
        return current_caller().persona_id

    @server.scoped_tool(owner)
    async def add_claims(
        claims: Annotated[
            list[ClaimIn], Field(max_length=MAX_ADD, description="Up to 200 claims.")
        ],
        absorb: Annotated[
            bool,
            Field(description="Strengthen an existing claim that says the same instead of adding."),
        ] = True,
    ) -> dict[str, Any]:
        """Add claims derived elsewhere. They land as pending unless status is approved.

        `category` must be an existing path: see list_categories (create new ones with
        upsert_category). `audiences` takes names from list_audiences; "default" means the
        default audience. Returns created claim IDs, absorbed items ({index, into}) and dropped items
        ({index, reason}); reasons: invalid, unknown_audience, guard (contains an email,
        phone, URL or @handle), previously_rejected.
        """
        caller = current_caller()
        items = [c.model_dump(exclude_none=True) for c in claims]
        return store.add_claims(
            server.conn,
            caller.persona_id,
            items,
            absorb=absorb,
            allow_approved=caller.scope == "owner",
        )

    @server.scoped_tool(owner, read_only=True)
    async def list_claims(
        category: Annotated[
            Path | None, Field(description="Path; includes sub-categories.")
        ] = None,
        kind: Kind | None = None,
        audience: Annotated[Name | None, Field(description="Claims that apply to it.")] = None,
        status: Status | Literal["any"] = "approved",
        include_archived: Annotated[
            bool, Field(description='With status "any", also return archived claims.')
        ] = False,
        limit: Limit = None,
        cursor: Cursor | None = None,
    ) -> dict[str, Any]:
        """List claims (default: approved), oldest first, paginated with next_cursor."""
        return store.list_claims(
            server.conn,
            persona_id(),
            category=category,
            kind=kind,
            audience=audience,
            status=status,
            include_archived=include_archived,
            limit=limit,
            cursor=cursor,
        )

    @server.scoped_tool(owner, read_only=True)
    async def search_claims(
        query: Annotated[Query, Field(description="Free text; any word may match.")],
        status: Status | Literal["any"] = "approved",
        kind: Kind | None = None,
        audience: Name | None = None,
        limit: Limit = None,
    ) -> dict[str, Any]:
        """Full-text search over claim texts, best matches first; score is relative to the
        best hit (1.0 = best, 0..1), not comparable across queries."""
        return store.search_claims(
            server.conn,
            persona_id(),
            query,
            status=status,
            kind=kind,
            audience=audience,
            limit=limit,
        )

    @server.scoped_tool(owner)
    async def update_claim(
        id: Id,
        text: ClaimText | None = None,
        kind: Kind | None = None,
        category: Path | None = None,
        audiences: Audiences | None = None,
        visibility: Visibility | None = None,
        confidence: float | None = None,
        status: Status | None = None,
    ) -> dict[str, Any]:
        """Edit a claim. Editing an approved claim's text keeps it approved (origin becomes
        owner). Archived claims can be restored with status approved or pending within 30 days."""
        claim = store.update_claim(
            server.conn,
            persona_id(),
            id,
            text=text,
            kind=kind,
            category=category,
            audiences=audiences,
            visibility=visibility,
            confidence=confidence,
            status=status,
        )
        return {"claim": claim}

    @server.scoped_tool(owner, destructive=True)
    async def delete_claims(ids: Ids) -> dict[str, Any]:
        """Permanently delete claims (owner intent; only the count is kept)."""
        return store.delete_claims(server.conn, persona_id(), ids)

    @server.scoped_tool(owner)
    async def review_claims(
        approve: Ids | None = None,
        reject: Ids | None = None,
        create_category: Annotated[
            bool, Field(description="Create a claim's proposed_category when approving it.")
        ] = False,
    ) -> dict[str, Any]:
        """Approve or reject claims in bulk. Rejected claims are kept as negative memory, so
        the same claim is not proposed again. Proposed change IDs (chg_..., see list_changes)
        are accepted too: approving applies the change, rejecting dismisses it."""
        return store.review_claims(
            server.conn,
            persona_id(),
            approve=approve or [],
            reject=reject or [],
            create_category=create_category,
        )
