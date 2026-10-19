# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persona lifecycle and settings tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import Name, Path, Short
from wwyd.store import personas, portable

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


class LLMBackendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: Literal["instance", "localai", "ollama", "lmstudio", "openrouter"] = Field(
        description="instance = the server's shared default; the others are presets whose"
        " base_url/model can be overridden."
    )
    base_url: Short | None = Field(default=None, description="OpenAI-compatible base URL.")
    model: Path | None = None
    api_key: Short | None = Field(
        default=None, description="Stored encrypted; omit to keep the current key, null to clear."
    )
    price_in_per_m: float | None = Field(default=None, description="USD per 1M input tokens.")
    price_out_per_m: float | None = Field(default=None, description="USD per 1M output tokens.")


def register(server: WwydServer) -> None:
    owner = ("owner",)

    @server.scoped_tool(owner, read_only=True)
    async def export_persona() -> dict[str, Any]:
        """Export the whole persona as a wwyd.persona v1 document (schema/persona.v1.json)."""
        return {"document": portable.export_persona(server.conn, current_caller().persona_id)}

    @server.scoped_tool(owner, destructive=True)
    async def import_persona(
        document: Annotated[dict[str, Any], Field(description="A wwyd.persona v1 document.")],
        mode: Annotated[
            Literal["merge", "replace"],
            Field(description="merge (default) updates by id/name/path; replace wipes first."),
        ] = "merge",
    ) -> dict[str, Any]:
        """Import a persona document. Returns created/updated counts."""
        return portable.import_persona(
            server.conn, current_caller().persona_id, document, mode=mode
        )

    @server.scoped_tool(owner, destructive=True)
    async def delete_persona(
        confirm: Annotated[Short, Field(description="Must equal the account login.")],
    ) -> dict[str, Any]:
        """Delete all claims, audiences, categories, changes and settings, and start again with
        an empty seeded persona. The account itself stays."""
        return personas.delete_persona(server.conn, current_caller().persona_id, confirm)

    @server.scoped_tool(owner, read_only=True)
    async def get_settings() -> dict[str, Any]:
        """Persona settings, limits (effective, admin ceilings, own), today's usage, the
        spend-cap flag and where raw text goes."""
        return personas.get_settings(server.conn, current_caller().persona_id)

    @server.scoped_tool(owner)
    async def update_settings(
        display_name: Annotated[Name | None, Field(description="Used in the disclosure.")] = None,
        timezone: Annotated[
            Name | None, Field(description="IANA name, e.g. Europe/Berlin.")
        ] = None,
        escalate_below: Annotated[
            float | None, Field(description="predict escalates below this confidence (0..1).")
        ] = None,
        disclosure_template: Annotated[
            Short | None, Field(description="Must contain {owner}.")
        ] = None,
        llm_for_ingest: Annotated[
            bool | None, Field(description="false: derive claims from code detectors only.")
        ] = None,
        limits: Annotated[
            Annotated[dict[Name, int | float | None], Field(max_length=50)] | None,
            Field(
                description='Your own lower limits, e.g. {"predict_per_day": 100}; values above'
                " the default or admin ceiling are rejected; null removes one."
            ),
        ] = None,
        llm: Annotated[
            LLMBackendIn | None,
            Field(
                description="LLM backend. The new backend is checked with GET /models before"
                " it is saved. Remote providers see predict situations and raw ingest text."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Change persona settings; returns the new settings (the API key is shown masked)."""
        return personas.update_settings(
            server.conn,
            current_caller().persona_id,
            display_name=display_name,
            timezone=timezone,
            escalate_below=escalate_below,
            disclosure_template=disclosure_template,
            llm_for_ingest=llm_for_ingest,
            limits=limits,
            llm=llm.model_dump(exclude_unset=True) if llm is not None else None,
        )
