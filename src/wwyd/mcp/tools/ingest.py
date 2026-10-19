# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest tools: ingest_raw, job_status, list_sources, reset_source (SPEC §6.1).

Episodes are never logged or stored."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from wwyd.ingest import jobs
from wwyd.ingest.jobs import MAX_EPISODES
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
        episodes: Annotated[
            list[dict[str, Any]],
            Field(
                max_length=MAX_EPISODES,
                description=(
                    "1-1000 episodes, <= 2 MB serialized. Each episode is {when?: datetime | "
                    "{start, end?} (when the owner acted: a moment such as a reply sent, or a "
                    "span such as a meeting attended), situation_at? (datetime the situation "
                    "reached the owner, e.g. message received; needs situation), where? "
                    "(coarse context such as home, online, work Slack; <= 100, never an "
                    "address), audience? (audience name or default), category? (a path such "
                    "as hobbies/films, created if unknown; omit it and the server places the "
                    "episode), situation? (what happened or was said to the owner, <= 8000), "
                    "decision? (what the owner did, described by you, <= 4000), said? (the "
                    "owner's verbatim words, <= 20000), corrects? (a prediction_id from "
                    "predict: this episode is what the owner actually says or does instead; "
                    "needs situation)}, with decision or said. Shapes: statement about self "
                    "= said only; chat reply = situation + said; choice = situation + "
                    "decision; call = situation (others) + said (owner). Response latency is "
                    "when minus situation_at. No other fields are accepted: never put names, "
                    "emails, phones or addresses in any field or category path. Datetimes "
                    "are ISO 8601; without an offset they are read in the persona's timezone."
                ),
            ),
        ],
        llm_for_ingest: Annotated[
            bool | None,
            Field(description="false: no LLM step for this batch (analyzers only)."),
        ] = None,
        source: Annotated[
            Name | None,
            Field(
                description=(
                    "Owner-chosen slug for where the episodes come from ([a-z0-9][a-z0-9_-]"
                    "{0,39}, e.g. telegram, gcal). Episodes with a timestamp at or below the "
                    "source's watermark are skipped (counts.skipped_seen); see list_sources."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Derive claims from episodes in the background, then drop the episodes.

        Returns {job_id, accepted, estimated_tokens, remaining_tokens} at once; poll
        job_status. accepted = valid episodes received; estimated_tokens = conservative
        estimate of the LLM tokens the job will use (0 without an LLM step);
        remaining_tokens = today's remaining LLM token budget (null: unlimited, 0: spend cap
        reached). Size batches so estimated_tokens fits remaining_tokens; a job that runs
        out ends partial with remaining_records to resend. Unknown category paths are
        created (limits: max_categories, new_categories_per_job; beyond them an episode
        goes to the closest existing ancestor or other, counted in unmapped_categories).
        Episodes without a category are placed by the server's LLM (to other without an
        LLM step; counted in unplaced_episodes when not placed). A corrects ID that is
        unknown or older than 90 days is ignored (unknown_predictions).
        Raw episodes are never stored. Derived claims land as pending for review_claims;
        analyzers update their keyed claims instead of duplicating them.
        """
        caller = current_caller()
        return jobs.submit(
            server.conn,
            caller.user_id,
            caller.persona_id,
            episodes,
            llm_for_ingest=llm_for_ingest,
            source=source,
        )

    @server.scoped_tool(owner, read_only=True)
    async def job_status(job_id: Id) -> dict[str, Any]:
        """Status of an ingest job: queued | running | done | partial | failed | interrupted,
        progress 0..1, counts (episodes by class: total, statement, reply, decision;
        claims_created, claims_updated, absorbed, dropped_by_guard, unknown_audiences,
        categories_created, unmapped_categories, unplaced_episodes, corrections,
        unknown_predictions, skipped_seen, skipped_saturated,
        saturated_audiences, llm_calls, analyzer counters, ...) and error_code when it did
        not finish.
        partial: stopped by the daily token limit (rate_limited), the spend cap (spend_cap)
        or an unavailable LLM (llm_unavailable) after storing what it derived; resend
        exactly the episodes at remaining_records (indices into the original batch), after
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
        episode timestamp, UTC), last_job_at, records_total}]}."""
        return {"sources": source_store.list_sources(server.conn, current_caller().persona_id)}

    @server.scoped_tool(owner, destructive=True)
    async def reset_source(
        source: Annotated[Name, Field(description="Source slug, as given to ingest_raw.")],
    ) -> dict[str, Any]:
        """Forget one source's watermark so the next batch with it is processed in full
        (e.g. a re-export from scratch). Claims are not touched. Returns {deleted}."""
        deleted = source_store.reset_source(server.conn, current_caller().persona_id, source)
        return {"deleted": deleted}
