# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest jobs (SPEC §7.1): validate, enqueue in memory, derive in the background.

``submit`` validates and size-checks a batch of episodes, writes a ``jobs`` row with counts
only, resolves (and organically creates) the episodes' categories and queues the parsed
episodes on a per-persona in-memory queue; one worker task per persona drains it. The
episodes are held only by the :class:`Job` object and are released in ``finally`` whatever
happens (P1). Nothing here logs episode content, prompts or replies.

Order inside a job: preparation (in ``submit``: times, audiences, categories, classes;
then the category description call) -> annotation (§7.3) -> the LLM passes (rebuilt with
the labels) -> analyzer authoring and the hypothesis test (§7.4) -> the analyzers over the
processed episodes (always; builtin, then active LLM analyzers in the sandbox) -> word usage
notes (§7.2) -> watermark. Authoring and usage-note calls cover no episodes: when they do
not fit the budget they are skipped with a counter instead of making the job ``partial``.

Derived candidates are flushed to the store every ``FLUSH_EVERY`` LLM calls and when the
job ends, so a job stopped by the token budget, the spend cap or an unavailable backend
keeps what it derived and ends ``partial`` with the batch indices still to be resent.
Only indices, timestamps and counts are persisted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from wwyd import compact
from wwyd.analyzers import authoring, run_analyzers
from wwyd.analyzers import llm as llm_analyzers
from wwyd.analyzers.builtin.base import host_fields
from wwyd.analyzers.word_usage import write_notes
from wwyd.config import thresholds
from wwyd.db import transaction
from wwyd.errors import Invalid, LLMUnavailable, RateLimited, SpendCap, TooLarge, WwydError
from wwyd.ingest import annotate, passes
from wwyd.ingest.episodes import Batch, parse_batch
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.rare import RareWords
from wwyd.limits import get_limiter
from wwyd.llm import get_llm
from wwyd.store import claims as claim_store
from wwyd.store import jobs as job_store
from wwyd.store import sources as source_store
from wwyd.store.categories import (
    CategoryResolver,
    category_ids_by_path,
    guard_vocabulary,
    normalize_category_path,
    set_descriptions,
)

log = logging.getLogger(__name__)

MAX_EPISODES = 1000
MAX_BYTES = 2 * 1024 * 1024
ADD_CHUNK = 200  # store.claims.add_claims accepts at most 200 items per call
FLUSH_EVERY = 20  # LLM calls between two flushes of derived candidates (SPEC §7.1)
AUTHORING_ESTIMATE = 8000  # tokens per authoring call in ``estimated_tokens`` (rough)

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
    source: str | None = None
    calls: list[passes.Call] = field(default_factory=list)
    # Batch index -> timestamp of every episode the job derives from (indices only, P1).
    stamps: dict[int, datetime | None] = field(default_factory=dict)
    # Batch index -> number of LLM calls containing it that have not completed yet.
    pending: dict[int, int] = field(default_factory=dict)
    # The LLM error that stopped the LLM phase; the job then ends ``partial``.
    stopped_by: WwydError | None = None

    def remaining(self) -> list[int]:
        return sorted(i for i, n in self.pending.items() if n > 0)


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


def _batch_bytes(episodes: list[Any]) -> int:
    try:
        return len(json.dumps(episodes, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        raise Invalid("episodes must be JSON objects") from None


def _categories(conn: sqlite3.Connection, persona_id: str) -> dict[str, str]:
    return {
        r["path"]: r["description"]
        for r in conn.execute(
            "SELECT path, description FROM categories WHERE persona_id = ? ORDER BY path",
            (persona_id,),
        )
    }


def _annotate_call(
    system: str, user: str, schema: dict[str, Any], records: set[int], meta: Any
) -> passes.Call:
    return passes.Call(
        "annotate", system, user, schema, "all", {}, None, frozenset(records), meta=meta
    )


def submit(
    conn: sqlite3.Connection,
    user_id: str,
    persona_id: str,
    episodes: list[Any],
    *,
    llm_for_ingest: bool | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Validate, record a job row, resolve categories and queue the batch.

    Returns ``{job_id, accepted, estimated_tokens, remaining_tokens}``: ``accepted`` is the
    number of valid episodes received, ``estimated_tokens`` a conservative estimate of the
    job's LLM tokens (0 without an LLM step) and ``remaining_tokens`` today's remaining token
    budget (None: unlimited). ``llm_for_ingest`` can only narrow the persona setting: when
    the persona has LLM derivation off, a call cannot turn it on. With ``source``, episodes
    at or below that source's watermark are skipped (SPEC §7.1).
    """
    if not isinstance(episodes, list) or not episodes:
        raise Invalid("episodes must be a non-empty list", field="episodes")
    if source is not None:
        source_store.check_source(source)
    nbytes = _batch_bytes(episodes)
    if len(episodes) > MAX_EPISODES:
        raise TooLarge("too many episodes in one call", limit="batch_records", maximum=MAX_EPISODES)
    if nbytes > MAX_BYTES:
        raise TooLarge("batch too large", limit="batch_bytes", maximum=MAX_BYTES)
    limiter = get_limiter(conn)
    limiter.check_ingest(user_id, persona_id, len(episodes), nbytes)
    persona = _persona(conn, persona_id)
    batch = parse_batch(
        episodes,
        tz=persona["tz"],
        audiences=persona["audiences"],
        default_audience=persona["default_audience"],
        normalize_category=normalize_category_path,
    )
    received = batch.counts()
    skipped_seen = 0
    if source is not None:
        last_ts = source_store.get_last_ts(conn, persona_id, source)
        if last_ts is not None:
            skipped_seen = batch.drop_seen(last_ts)
    use_llm = bool(persona["llm_for_ingest"]) and llm_for_ingest is not False
    counts: dict[str, Any] = {
        "episodes": received,
        "unknown_audiences": batch.unknown_audiences,
        "categories_created": 0,
        "unmapped_categories": 0,
        "skipped_seen": skipped_seen,
        "skipped_saturated": 0,
        "saturated_audiences": 0,
        "claims_created": 0,
        "claims_updated": 0,
        "absorbed": 0,
        "dropped_by_guard": 0,
        "dropped_previously_rejected": 0,
        "dropped_invalid": 0,
        "llm_calls": 0,
        "llm_calls_failed": 0,
        "annotation_calls": 0,
        "labels_created": 0,
        "analyzers_proposed": 0,
        "analyzers_accepted": 0,
        "analyzers_rejected": 0,
        "sandbox_errors": 0,
        "skipped_sandbox_budget": 0,
    }
    limits = limiter.effective_limits(user_id)
    with transaction(conn):
        job_id = job_store.create_job(conn, persona_id, counts)
        # Preparation (SPEC §7.1, §4.4): resolve every category, creating missing paths
        # within the limits; one category_create change for the job.
        resolver = CategoryResolver(
            conn,
            persona_id,
            max_categories=limits.get("max_categories"),  # type: ignore[arg-type]
            new_per_job=limits.get("new_categories_per_job"),  # type: ignore[arg-type]
        )
        for ep in batch.episodes:
            ep.category_id, ep.category = resolver.resolve(ep.category)
        resolver.record(job_id)
        counts["categories_created"] = len(resolver.created)
        counts["unmapped_categories"] = resolver.unmapped
        job_store.update_job(conn, job_id, counts=counts)
    calls: list[passes.Call] = []
    authoring_estimate = 0
    if use_llm:
        describe = passes.describe_call(resolver.created)
        min_eps = thresholds().analyzer_min_episodes
        eligible = annotate.eligible_categories(conn, persona_id, batch, min_eps)
        annotation = annotate.plan(
            batch, eligible, annotate.load_vocabulary(conn, persona_id), _annotate_call
        )
        calls = (
            ([describe] if describe else [])
            + annotation
            + passes.build_calls(
                batch,
                _categories(conn, persona_id),
                default_audience=persona["default_audience"],
                decision_min_episodes=min_eps,
            )
        )
        # One authoring call per category large enough (§7.4); a rough allowance.
        per_cat: dict[str, int] = {}
        for ep in batch.episodes:
            per_cat[ep.category_id] = per_cat.get(ep.category_id, 0) + 1
        authoring_estimate = AUTHORING_ESTIMATE * sum(n >= min_eps for n in per_cat.values())
    limiter.record_ingest(user_id, persona_id, len(episodes), nbytes)
    job = Job(job_id, user_id, persona_id, conn, use_llm, batch, counts, source=source)
    job.calls, job.stamps = calls, batch.stamps()
    _events[job_id] = job.done
    _queues.setdefault(persona_id, deque()).append(job)
    worker = _workers.get(persona_id)
    if worker is None or worker.done():
        _workers[persona_id] = asyncio.get_running_loop().create_task(
            _worker(persona_id), name=f"wwyd-ingest-{persona_id}"
        )
    return {
        "job_id": job_id,
        "accepted": len(episodes),
        "estimated_tokens": sum(c.estimated_tokens for c in calls) + authoring_estimate,
        "remaining_tokens": limiter.remaining_tokens(user_id, persona_id),
    }


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
    except (SpendCap, RateLimited, LLMUnavailable) as exc:
        # Raised by the LLM phase (after storing what it derived and running the
        # analyzers on the processed episodes): partial. Otherwise (job slot): failed.
        status = "partial" if exc is job.stopped_by else "failed"
        error_code = exc.code
    except WwydError as exc:
        error_code = exc.code
    except Exception as exc:  # noqa: BLE001
        # Type only: messages and tracebacks could carry episode text.
        log.error("ingest job %s failed: %s", job.job_id, type(exc).__name__)
    finally:
        if job.batch is not None:
            job.batch.clear()
        job.batch = None
        job.calls = []
        try:
            remaining = job.remaining() if status == "partial" else None
            if job.source is not None and status in ("done", "partial"):
                _advance_watermark(job, remaining or [])
            job_store.update_job(
                conn,
                job.job_id,
                status=status,
                progress=1.0 if status == "done" else None,
                counts=job.counts,
                error_code=error_code,
                remaining_records=remaining if status != "done" else [],
            )
        finally:
            job.stamps.clear()
            job.pending.clear()
            job.done.set()
            _events.pop(job.job_id, None)


def _advance_watermark(job: Job, remaining: list[int]) -> None:
    """``done``: the largest processed timestamp. ``partial``: the largest processed one
    strictly below the earliest remaining episode's, so a resend is not skipped. The store
    never moves it backwards."""
    left = set(remaining)
    processed = [ts for i, ts in job.stamps.items() if i not in left and ts is not None]
    later = [job.stamps[i] for i in left if job.stamps.get(i) is not None]
    if later:
        first = min(later)  # type: ignore[type-var]
        processed = [ts for ts in processed if ts < first]
    assert job.source is not None
    source_store.record_job(
        job.conn,
        job.persona_id,
        job.source,
        max(processed) if processed else None,
        len(job.stamps) - len(left),
    )


async def _derive(job: Job) -> None:
    conn, pid, counts = job.conn, job.persona_id, job.counts
    batch = job.batch
    assert batch is not None
    persona = _persona(conn, pid)
    touched: list[str] = []
    guard: BatchGuard | None = None
    rare: RareWords | None = None
    try:
        calls = job.calls
        for call in calls:
            for i in call.records:
                job.pending[i] = job.pending.get(i, 0) + 1
        job_store.update_job(conn, job.job_id, progress=1 / (len(calls) + 2), counts=counts)
        if job.use_llm:
            vocabulary = {
                persona["display_name"],
                *persona["audiences"],
                *guard_vocabulary(conn, pid),
            }
            guard = passes.guard_for(batch, vocabulary)
            rare = RareWords(batch.episodes)
        # LLM phase: the description call and annotation (§7.3), then the passes (§7.6),
        # rebuilt so that their prompts carry the labels. An LLM error stops this phase.
        first = [c for c in calls if c.purpose in ("describe_categories", "annotate")]
        old_passes = [c for c in calls if c.purpose not in ("describe_categories", "annotate")]
        if calls:
            assert guard is not None and rare is not None
            labeler = annotate.Labeler(conn, pid, guard, rare, annotate.load_vocabulary(conn, pid))
            try:
                if first:
                    await _llm_calls(job, persona, touched, first, guard, labeler)
                new_passes = old_passes
                if any(c.purpose == "annotate" for c in first):
                    new_passes = passes.build_calls(
                        batch,
                        _categories(conn, pid),
                        default_audience=persona["default_audience"],
                        decision_min_episodes=thresholds().analyzer_min_episodes,
                    )
                    for call in old_passes:
                        for i in call.records:
                            job.pending[i] -= 1
                    for call in new_passes:
                        for i in call.records:
                            job.pending[i] = job.pending.get(i, 0) + 1
                    job.calls = first + new_passes
                if new_passes:
                    await _llm_calls(job, persona, touched, new_passes, guard, labeler)
            except (SpendCap, RateLimited, LLMUnavailable) as exc:
                job.stopped_by = exc
            finally:
                lc = labeler.counts
                counts["labels_created"] += lc.labels_created
                counts["labels_dropped"] = counts.get("labels_dropped", 0) + lc.labels_dropped
                counts["dropped_by_guard"] += lc.dropped_by_guard
                counts["dropped_invalid"] += lc.invalid
                counts["llm_calls_failed"] += lc.failed_calls

        processed = batch.subset(set(job.remaining())) if job.stopped_by else batch
        # Analyzer authoring and the hypothesis loop (§7.4), on processed episodes only.
        if job.use_llm and job.stopped_by is None:
            assert guard is not None and rare is not None
            await _author(job, persona, processed, guard, rare)

        # The analyzers (always), over the processed episodes only: remaining episodes
        # reach analyzer state through their resend, so each is counted once.
        touched.extend(
            run_analyzers(
                conn,
                persona_id=pid,
                episodes=processed.episodes,
                tz=persona["tz"],
                counts=counts,
                job_id=job.job_id,
            )
        )
        touched.extend(
            await llm_analyzers.run_active(
                conn,
                persona_id=pid,
                user_id=job.user_id,
                episodes=processed.episodes,
                tz=persona["tz"],
                counts=counts,
                job_id=job.job_id,
                guard=guard,
            )
        )
        if job.use_llm and job.stopped_by is None:
            assert guard is not None and rare is not None
            touched.extend(
                await write_notes(
                    conn,
                    persona_id=pid,
                    user_id=job.user_id,
                    episodes=processed.episodes,
                    counts=counts,
                    guard=guard,
                    rare=rare,
                    llm=get_llm(conn, pid),
                    limiter=get_limiter(conn),
                    job_id=job.job_id,
                )
            )
        job_store.update_job(conn, job.job_id, counts=counts)
        if job.stopped_by is not None:
            raise job.stopped_by
    finally:
        if guard is not None:
            guard.clear()
        if rare is not None:
            rare.clear()
        if touched:
            # The claims are stored; a compaction problem must not fail the job (compaction
            # also runs on demand). Type only in the log: messages could quote claim text.
            try:
                await compact.run_after_job(
                    conn, pid, list(dict.fromkeys(touched)), job_id=job.job_id
                )
            except Exception as exc:  # noqa: BLE001
                counts["compaction_failed"] = 1
                log.warning("compaction after job %s failed: %s", job.job_id, type(exc).__name__)


async def _author(
    job: Job, persona: dict[str, Any], processed: Batch, guard: BatchGuard, rare: RareWords
) -> None:
    """One authoring round per triggered category (§7.4); needs the sandbox."""
    conn, pid, counts = job.conn, job.persona_id, job.counts
    items = [host_fields(ep, persona["tz"], {}) for ep in processed.episodes]
    cats = authoring.triggered(items, thresholds().analyzer_min_episodes)
    if not cats:
        return
    runner = llm_analyzers.SandboxRunner(conn, job.user_id, counts)
    if not runner.available():
        counts["sandbox_unavailable"] = counts.get("sandbox_unavailable", 0) + len(cats)
        return
    limiter = get_limiter(conn)
    ctx = authoring.RoundContext(
        conn, pid, job.user_id, counts, guard, rare, get_llm(conn, pid), limiter, runner
    )
    paths = {ep.category_id: ep.category for ep in processed.episodes}
    max_analyzers = limiter.effective_limits(job.user_id).get("max_analyzers")
    for cat_id in cats:
        await authoring.author_round(ctx, cat_id, paths[cat_id], items, max_analyzers)
    items.clear()


async def _llm_calls(
    job: Job,
    persona: dict[str, Any],
    touched: list[str],
    calls: list[passes.Call],
    guard: BatchGuard,
    labeler: annotate.Labeler,
) -> None:
    """Run the calls in order with budget checks, saturation and periodic flushes."""
    conn, pid, counts = job.conn, job.persona_id, job.counts
    assert job.batch is not None
    category_ids = category_ids_by_path(conn, pid)
    llm = get_llm(conn, pid)
    limiter = get_limiter(conn)
    derive_counts = passes.DeriveCounts()
    window: list[passes.Candidate] = []
    streak: dict[str, int] = {}
    saturated: set[str] = set()
    skipped: set[int] = set()
    total = len(job.calls)
    offset = len(job.calls) - len(calls)

    def finish(call: passes.Call) -> None:
        for i in call.records:
            job.pending[i] -= 1

    def is_new(cand: passes.Candidate) -> bool:
        if any(passes.same_claim(cand, other) for other in window):
            return False
        return not claim_store.known_claim(
            conn, pid, cand.kind, category_ids.get(cand.category, ""), cand.text
        )

    def flush() -> None:
        if window:
            items = [c.as_item() for c in passes.dedupe(window, derive_counts)]
            window.clear()
            _store(job, items, touched)
        job_store.update_job(conn, job.job_id, counts=counts)

    try:
        for n, call in enumerate(calls, start=1):
            audience = call.audiences[0] if call.purpose == "replies" else None
            if audience is not None and audience in saturated:
                skipped.update(call.records)
                finish(call)
                continue
            # Budget: the limiter's own check, then the estimate against what is left.
            limiter.check_llm_budget(job.user_id, pid)
            left = limiter.remaining_tokens(job.user_id, pid)
            if left is not None and call.estimated_tokens > left:
                raise RateLimited(
                    "daily LLM token limit reached",
                    retry_after=limiter.secs_to_reset("rate_limited"),
                    limit="llm_tokens_per_day",
                )
            counts["llm_calls"] += 1
            result = await llm.complete_json(
                purpose=call.purpose,  # type: ignore[arg-type]
                system=call.system,
                user=call.user,
                schema=call.schema,
            )
            if call.purpose == "annotate":
                counts["annotation_calls"] += 1
                with transaction(conn):
                    labeler.apply(call.meta, result.data)
                del result
                finish(call)
                job_store.update_job(conn, job.job_id, progress=(offset + n + 1) / (total + 2))
                continue
            if call.purpose == "describe_categories":
                described = passes.to_descriptions(call, result.data, guard, derive_counts)
                del result
                if described:
                    set_descriptions(conn, pid, described)
                finish(call)
                continue
            found = passes.to_candidates(call, result.data, guard, derive_counts)
            del result
            if audience is not None:
                fresh = any(is_new(c) for c in found)
                streak[audience] = 0 if fresh else streak.get(audience, 0) + 1
                if streak[audience] >= passes.SATURATION_CALLS:
                    saturated.add(audience)
                    counts["saturated_audiences"] += 1
            window.extend(found)
            finish(call)
            if n % FLUSH_EVERY == 0:
                flush()
            job_store.update_job(conn, job.job_id, progress=(offset + n + 1) / (total + 2))
    finally:
        try:
            counts["skipped_saturated"] += len(skipped)
            flush()
        finally:
            counts["dropped_by_guard"] += derive_counts.dropped_by_guard
            counts["dropped_invalid"] += derive_counts.invalid
            counts["llm_calls_failed"] += derive_counts.failed_calls


def _store(job: Job, items: list[dict[str, Any]], touched: list[str]) -> None:
    counts = job.counts
    for start in range(0, len(items), ADD_CHUNK):
        result = claim_store.add_claims(
            job.conn,
            job.persona_id,
            items[start : start + ADD_CHUNK],
            absorb=True,
            origin="derived",
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
