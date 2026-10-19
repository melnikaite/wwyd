# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest jobs (SPEC §7.1): validate, enqueue in memory, derive in the background.

``submit`` validates and size-checks a batch, writes a ``jobs`` row with counts only and
queues the parsed records on a per-persona in-memory queue; one worker task per persona
drains it. The records are held only by the :class:`Job` object and are released in
``finally`` whatever happens (P1). Nothing here logs record content, prompts or replies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from wwyd import compact
from wwyd.db import transaction
from wwyd.errors import Invalid, LLMUnavailable, RateLimited, SpendCap, TooLarge, WwydError
from wwyd.ingest import detectors, style
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.keyed import upsert_finding
from wwyd.ingest.records import Batch, parse_batch
from wwyd.limits import get_limiter
from wwyd.llm import get_llm
from wwyd.store import claims as claim_store
from wwyd.store import jobs as job_store

log = logging.getLogger(__name__)

MAX_RECORDS = 1000
MAX_BYTES = 2 * 1024 * 1024
ADD_CHUNK = 200  # store.claims.add_claims accepts at most 200 items per call

__all__ = ["mark_interrupted", "submit", "wait"]


@dataclass
class Job:
    job_id: str
    user_id: str
    persona_id: str
    conn: sqlite3.Connection
    use_llm: bool
    batch: Batch | None
    counts: dict[str, Any] = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)


_queues: dict[str, deque[Job]] = {}
_workers: dict[str, asyncio.Task[None]] = {}
_events: dict[str, asyncio.Event] = {}


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """Call once at startup: queued/running jobs of a previous process become interrupted."""
    return job_store.mark_interrupted(conn)


def _persona(conn: sqlite3.Connection, persona_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT display_name, timezone, settings FROM personas WHERE id = ?", (persona_id,)
    ).fetchone()
    settings = json.loads(row["settings"] or "{}")
    default = conn.execute(
        "SELECT name FROM audiences WHERE persona_id = ? AND is_default = 1", (persona_id,)
    ).fetchone()
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM audiences WHERE persona_id = ?", (persona_id,))
    }
    return {
        "display_name": row["display_name"],
        "tz": ZoneInfo(row["timezone"] or "UTC"),
        "llm_for_ingest": settings.get("llm_for_ingest", True),
        "default_audience": default["name"] if default else "everyone",
        "audiences": names,
    }


def _batch_bytes(records: list[Any]) -> int:
    try:
        return len(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        raise Invalid("records must be JSON objects") from None


def submit(
    conn: sqlite3.Connection,
    user_id: str,
    persona_id: str,
    records: list[Any],
    *,
    llm_for_ingest: bool | None = None,
) -> dict[str, Any]:
    """Validate, record a job row and queue the batch; returns ``{job_id, accepted}``.

    ``llm_for_ingest`` can only narrow the persona setting: when the persona has LLM
    derivation off, a call cannot turn it on.
    """
    if not isinstance(records, list) or not records:
        raise Invalid("records must be a non-empty list", field="records")
    nbytes = _batch_bytes(records)
    if len(records) > MAX_RECORDS:
        raise TooLarge("too many records in one call", limit="batch_records", maximum=MAX_RECORDS)
    if nbytes > MAX_BYTES:
        raise TooLarge("batch too large", limit="batch_bytes", maximum=MAX_BYTES)
    limiter = get_limiter(conn)
    limiter.check_ingest(user_id, persona_id, len(records), nbytes)
    persona = _persona(conn, persona_id)
    batch = parse_batch(
        records,
        tz=persona["tz"],
        audiences=persona["audiences"],
        default_audience=persona["default_audience"],
    )
    counts = {
        "records": dict(batch.counts),
        "unknown_audiences": batch.unknown_audiences,
        "claims_created": 0,
        "claims_updated": 0,
        "absorbed": 0,
        "dropped_by_guard": 0,
        "dropped_previously_rejected": 0,
        "dropped_invalid": 0,
        "llm_calls": 0,
        "llm_calls_failed": 0,
    }
    with transaction(conn):
        job_id = job_store.create_job(conn, persona_id, counts)
    limiter.record_ingest(user_id, persona_id, len(records), nbytes)
    use_llm = bool(persona["llm_for_ingest"]) and llm_for_ingest is not False
    job = Job(job_id, user_id, persona_id, conn, use_llm, batch, counts)
    _events[job_id] = job.done
    _queues.setdefault(persona_id, deque()).append(job)
    worker = _workers.get(persona_id)
    if worker is None or worker.done():
        _workers[persona_id] = asyncio.get_running_loop().create_task(
            _worker(persona_id), name=f"wwyd-ingest-{persona_id}"
        )
    return {"job_id": job_id, "accepted": len(records)}


async def wait(job_id: str, timeout: float | None = 30) -> None:
    """Wait until a job submitted by this process has finished (tests, CLI)."""
    event = _events.get(job_id)
    if event is not None:
        await asyncio.wait_for(event.wait(), timeout)


async def _worker(persona_id: str) -> None:
    queue = _queues[persona_id]
    try:
        while queue:
            job = queue.popleft()
            await _run(job)
    finally:
        if not queue:
            _queues.pop(persona_id, None)
        if _workers.get(persona_id) is asyncio.current_task():
            del _workers[persona_id]


async def _run(job: Job) -> None:
    conn = job.conn
    status, error_code = "failed", "internal"
    try:
        limiter = get_limiter(conn)
        async with limiter.job_slot(job.user_id, job.persona_id):
            job_store.update_job(conn, job.job_id, status="running")
            await _derive(job)
        status, error_code = "done", None
    except SpendCap:
        error_code = "spend_cap"
    except RateLimited:
        error_code = "rate_limited"
    except LLMUnavailable:
        error_code = "llm_unavailable"
    except WwydError as exc:
        error_code = exc.code
    except Exception as exc:  # noqa: BLE001
        # Type only: messages and tracebacks could carry record text.
        log.error("ingest job %s failed: %s", job.job_id, type(exc).__name__)
    finally:
        if job.batch is not None:
            job.batch.clear()
        job.batch = None
        try:
            job_store.update_job(
                conn,
                job.job_id,
                status=status,
                progress=1.0 if status == "done" else None,
                counts=job.counts,
                error_code=error_code,
            )
        finally:
            job.done.set()
            _events.pop(job.job_id, None)


async def _derive(job: Job) -> None:
    conn, pid, counts = job.conn, job.persona_id, job.counts
    batch = job.batch
    assert batch is not None
    persona = _persona(conn, pid)
    touched: list[str] = []

    # 1. Code detectors (always).
    for finding in detectors.collect_all(batch, persona["tz"]):
        outcome, claim_id = upsert_finding(conn, pid, finding)
        if outcome == "created":
            counts["claims_created"] += 1
        elif outcome == "updated":
            counts["claims_updated"] += 1
        elif outcome == "rejected":
            counts["dropped_previously_rejected"] += 1
        if claim_id:
            touched.append(claim_id)

    # 2. LLM derivation (style, notes, decisions).
    calls: list[style.Call] = []
    categories: dict[str, str] = {}
    if job.use_llm:
        categories = {
            r["path"]: r["description"]
            for r in conn.execute(
                "SELECT path, description FROM categories WHERE persona_id = ? ORDER BY path",
                (pid,),
            )
        }
        calls = style.build_calls(batch, categories)
    total_steps = 1 + len(calls)
    done_steps = 1
    job_store.update_job(conn, job.job_id, progress=done_steps / (total_steps + 1), counts=counts)

    if calls:
        guard = _guard(batch, persona, categories)
        llm = get_llm(conn, pid)
        derive_counts = style.DeriveCounts()

        async def complete(call: style.Call) -> Any:
            counts["llm_calls"] += 1
            result = await llm.complete_json(
                purpose=call.purpose,  # type: ignore[arg-type]
                system=call.system,
                user=call.user,
                schema=call.schema,
            )
            return result.data

        def progress() -> None:
            nonlocal done_steps
            done_steps += 1
            job_store.update_job(conn, job.job_id, progress=done_steps / (total_steps + 1))

        try:
            candidates = await style.derive(
                calls, complete, categories, guard, derive_counts, progress
            )
        finally:
            guard.clear()
            counts["dropped_by_guard"] += derive_counts.dropped_by_guard
            counts["dropped_invalid"] += derive_counts.invalid
            counts["llm_calls_failed"] += derive_counts.failed_calls
        items = [c.as_item() for c in candidates]
        del candidates
        for start in range(0, len(items), ADD_CHUNK):
            result = claim_store.add_claims(
                conn, pid, items[start : start + ADD_CHUNK], absorb=True, origin="derived"
            )
            counts["claims_created"] += len(result["created"])
            counts["absorbed"] += len(result["absorbed"])
            touched.extend(result["created"])
            touched.extend(a["into"] for a in result["absorbed"])
            for d in result["dropped"]:
                reason = d["reason"]
                if reason == "guard":
                    counts["dropped_by_guard"] += 1
                elif reason == "previously_rejected":
                    counts["dropped_previously_rejected"] += 1
                else:
                    counts["dropped_invalid"] += 1

    if touched:
        # The claims are stored; a compaction problem must not fail the job (compaction
        # also runs on demand). Type only in the log: messages could quote claim text.
        try:
            await compact.run_after_job(conn, pid, list(dict.fromkeys(touched)), job_id=job.job_id)
        except Exception as exc:  # noqa: BLE001
            counts["compaction_failed"] = 1
            log.warning("compaction after job %s failed: %s", job.job_id, type(exc).__name__)


def _guard(batch: Batch, persona: dict[str, Any], categories: dict[str, str]) -> BatchGuard:
    other: list[str] = [m.text for m in batch.messages if not m.from_owner]
    owner: list[str] = [m.text for m in batch.messages if m.from_owner]
    for t in batch.transcripts:
        for speaker, text in t.turns:
            (owner if speaker == "owner" else other).append(text)
    owner.extend(d.situation for d in batch.decisions)
    owner.extend(d.choice for d in batch.decisions)
    owner.extend(n.text for n in batch.notes)
    vocabulary = {persona["display_name"], *persona["audiences"]}
    for path in categories:
        vocabulary.update(path.split("/"))
    return BatchGuard(other_texts=other, all_texts=other + owner, vocabulary=vocabulary)
