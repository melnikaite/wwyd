# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest tools: ingest_raw and job_status (SPEC §6.1). Records are never logged or stored."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from wwyd.ingest import jobs
from wwyd.ingest.jobs import MAX_RECORDS
from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import Id
from wwyd.store import jobs as job_store

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


def register(server: WwydServer) -> None:
    owner = ("owner",)

    @server.scoped_tool(owner)
    async def ingest_raw(
        records: Annotated[
            list[dict[str, Any]],
            Field(
                max_length=MAX_RECORDS,
                description=(
                    "1-1000 records, <= 2 MB serialized. Each is one of: "
                    '{type: "message", from_owner, audience?, text (<= 4000), ts, thread '
                    "(opaque batch-local label, <= 64), position?}; "
                    '{type: "call_transcript", audience?, ts, turns: [{speaker: "owner"|'
                    '"other", text}]}; {type: "calendar_event", start, end, attendees (count), '
                    "response: accepted|declined|tentative|organizer|none, recurring?, online?}; "
                    '{type: "decision", situation, choice, audience?, ts?}; '
                    '{type: "note", text, ts?}. No other fields are accepted: never send '
                    "names, emails, phones, titles or locations. Datetimes are ISO 8601; "
                    "without an offset they are read in the persona's timezone."
                ),
            ),
        ],
        llm_for_ingest: Annotated[
            bool | None,
            Field(description="false: derive with code detectors only for this batch."),
        ] = None,
    ) -> dict[str, Any]:
        """Derive claims from raw records in the background, then drop the records.

        Returns {job_id, accepted} at once; poll job_status. Raw records are never stored.
        Derived claims land as pending for review_claims; habit detectors update their
        keyed claims instead of duplicating them.
        """
        caller = current_caller()
        return jobs.submit(
            server.conn,
            caller.user_id,
            caller.persona_id,
            records,
            llm_for_ingest=llm_for_ingest,
        )

    @server.scoped_tool(owner, read_only=True)
    async def job_status(job_id: Id) -> dict[str, Any]:
        """Status of an ingest job: queued | running | done | failed | interrupted, progress
        0..1, counts (records by type, claims_created, claims_updated, absorbed,
        dropped_by_guard, unknown_audiences, ...) and error_code when it failed. An
        interrupted job (server restart) must be sent again."""
        job = job_store.get_job(server.conn, current_caller().persona_id, job_id)
        out = {"status": job["status"], "progress": job["progress"], "counts": job["counts"]}
        if "error_code" in job:
            out["error_code"] = job["error_code"]
        return out
