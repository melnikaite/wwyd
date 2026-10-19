# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest tools: ingest_raw, job_status, list_sources, reset_source (SPEC §6.1).

Records are never logged or stored."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from wwyd.ingest import jobs
from wwyd.ingest.jobs import MAX_RECORDS
from wwyd.limits import get_limiter
from wwyd.mcp.caller import current_caller
from wwyd.mcp.tools._types import Id, Name
from wwyd.store import jobs as job_store
from wwyd.store import sources as source_store

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
        source: Annotated[
            Name | None,
            Field(
                description=(
                    "Owner-chosen slug for where the records come from ([a-z0-9][a-z0-9_-]"
                    "{0,39}, e.g. telegram, gcal). Records with a timestamp at or below the "
                    "source's watermark are skipped (counts.skipped_seen); see list_sources."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Derive claims from raw records in the background, then drop the records.

        Returns {job_id, accepted, estimated_tokens, remaining_tokens} at once; poll
        job_status. accepted = valid records received; estimated_tokens = conservative estimate of
        the LLM tokens the job will use (0 without an LLM step); remaining_tokens = today's
        remaining LLM token budget (null: unlimited, 0: spend cap reached). Size batches so
        estimated_tokens fits remaining_tokens; a job that runs out ends partial with
        remaining_records to resend. Raw records are never stored. Derived claims land as
        pending for review_claims; habit detectors update their keyed claims instead of
        duplicating them.
        """
        caller = current_caller()
        return jobs.submit(
            server.conn,
            caller.user_id,
            caller.persona_id,
            records,
            llm_for_ingest=llm_for_ingest,
            source=source,
        )

    @server.scoped_tool(owner, read_only=True)
    async def job_status(job_id: Id) -> dict[str, Any]:
        """Status of an ingest job: queued | running | done | partial | failed | interrupted,
        progress 0..1, counts (records by type, claims_created, claims_updated, absorbed,
        dropped_by_guard, unknown_audiences, skipped_seen, skipped_saturated,
        saturated_audiences, llm_calls, ...) and error_code when it did not finish.
        partial: stopped by the daily token limit (rate_limited), the spend cap (spend_cap)
        or an unavailable LLM (llm_unavailable) after storing what it derived; resend
        exactly the records at remaining_records (indices into the original batch), after
        retry_after seconds when given. An interrupted job (server restart) must be sent
        again in full."""
        conn = server.conn
        job = job_store.get_job(conn, current_caller().persona_id, job_id)
        out = {"status": job["status"], "progress": job["progress"], "counts": job["counts"]}
        if "error_code" in job:
            out["error_code"] = job["error_code"]
        if "remaining_records" in job:
            out["remaining_records"] = job["remaining_records"]
        if job["status"] == "partial":
            retry_after = get_limiter(conn).secs_to_reset(job.get("error_code") or "")
            if retry_after is not None:
                out["retry_after"] = retry_after
        return out

    @server.scoped_tool(owner, read_only=True)
    async def list_sources() -> dict[str, Any]:
        """Per-source ingest watermarks: {sources: [{source, last_ts (largest processed
        record timestamp, UTC), last_job_at, records_total}]}."""
        return {"sources": source_store.list_sources(server.conn, current_caller().persona_id)}

    @server.scoped_tool(owner, destructive=True)
    async def reset_source(
        source: Annotated[Name, Field(description="Source slug, as given to ingest_raw.")],
    ) -> dict[str, Any]:
        """Forget one source's watermark so the next batch with it is processed in full
        (e.g. a re-export from scratch). Claims are not touched. Returns {deleted}."""
        deleted = source_store.reset_source(server.conn, current_caller().persona_id, source)
        return {"deleted": deleted}
