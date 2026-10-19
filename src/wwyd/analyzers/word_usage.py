# SPDX-License-Identifier: AGPL-3.0-or-later
"""Word usage notes (SPEC §7.2): how the owner uses an audience's favourite words.

After the analyzers ran, for every audience with processed episodes whose favourite-words
set (``stylometry.favourite_words``) differs from the set noted last time, the host extracts
at most 5 snippets per word (±5 words around it, from ``said`` only, in memory) and one LLM
call writes a short usage note: a ``style`` claim keyed ``builtin:word_usage:<audience_id>``,
applying to that audience, in the stylometry claim category. The note is checked by the
verbatim guard (§7.7), by a 5-gram check against the snippets themselves (they are the
owner's verbatim words) and by the concentrated-word check, and budgeted like every call.

**What is remembered.** A builtin row ``word_usage`` per audience (audience scope, shown by
``list_analyzers``; disabling it turns notes off for that audience) keeps the noted set as
state metric ``noted``: one key per word, value 1, under the month of the note. The words
are the stylometry state keys that already passed the host key filter (§7.2), so nothing new
is stored; no snippet or raw text is. The set is replaced whenever a note was attempted
(also when the note was dropped by the guard), so an unchanged set costs no tokens.

Budget: the call covers no episodes; when it does not fit or the backend fails, notes are
skipped for the rest of the job (``skipped_llm_budget`` / ``word_usage_failed``) and the job
is not made ``partial``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from wwyd.analyzers.builtin.base import NO_AUDIENCE, ClaimSpec
from wwyd.analyzers.claims import upsert_keyed
from wwyd.db import transaction
from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.ingest.episodes import Episode
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.rare import RareWords
from wwyd.store import analyzers as store
from wwyd.store.categories import category_ids_by_path

NAME = "word_usage"
TEMPLATE = "How the owner uses their favourite words with this audience."
MAX_SNIPPETS = 5
CONTEXT_WORDS = 5
MAX_NOTE = 400
CHARS_PER_TOKEN = 3
OUTPUT_TOKENS = 300
DEFAULT_CATEGORY = "communication/style"

_WORD = re.compile(r"\S+")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["note"],
    "properties": {"note": {"type": "string"}},
}

SYSTEM = """The owner often uses the listed words when writing to one audience. For each word
you see a few short excerpts of the owner's own messages. Write ONE note (at most 300
characters) on how the owner uses these words: meaning, tone, typical place in a message.
Rules: do not quote the excerpts; never mention people's names, companies, places, links,
emails or phone numbers; write in the language of the excerpts.
Return JSON {"note": "..."}."""


def _favourites(conn: sqlite3.Connection, persona_id: str, audience_id: str) -> list[str]:
    try:
        from wwyd.analyzers.builtin.stylometry import favourite_words
    except ImportError:
        return []
    return list(favourite_words(conn, persona_id, audience_id))


def _claim_category(conn: sqlite3.Connection, persona_id: str) -> str | None:
    try:
        from wwyd.analyzers.builtin.stylometry import CLAIM_CATEGORY as path
    except ImportError:
        path = DEFAULT_CATEGORY
    ids = category_ids_by_path(conn, persona_id)
    return ids.get(path) or ids.get(DEFAULT_CATEGORY)


def snippets(texts: Sequence[str], word: str) -> list[str]:
    """At most 5 snippets (±5 words) around ``word`` (case-insensitive) in ``texts``."""
    out: list[str] = []
    target = word.casefold()
    for text in texts:
        tokens = _WORD.findall(text)
        for i, tok in enumerate(tokens):
            if re.sub(r"^\W+|\W+$", "", tok).casefold() == target:
                lo, hi = max(0, i - CONTEXT_WORDS), i + CONTEXT_WORDS + 1
                out.append(" ".join(tokens[lo:hi]))
                break
        if len(out) >= MAX_SNIPPETS:
            break
    return out


def _noted(conn: sqlite3.Connection, analyzer_id: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT key FROM analyzer_state WHERE analyzer_id = ? AND metric = 'noted'",
            (analyzer_id,),
        )
    }


def _remember(conn: sqlite3.Connection, analyzer_id: str, words: Sequence[str]) -> None:
    month = datetime.now(UTC).strftime("%Y-%m")
    conn.execute(
        "DELETE FROM analyzer_state WHERE analyzer_id = ? AND metric = 'noted'", (analyzer_id,)
    )
    conn.executemany(
        "INSERT INTO analyzer_state(analyzer_id, month, metric, key, value)"
        " VALUES (?, ?, 'noted', ?, 1)",
        [(analyzer_id, month, w) for w in words],
    )


async def write_notes(
    conn: sqlite3.Connection,
    *,
    persona_id: str,
    user_id: str,
    episodes: Sequence[Episode],
    counts: dict[str, Any],
    guard: BatchGuard,
    rare: RareWords,
    llm: Any,
    limiter: Any,
    job_id: str | None = None,
) -> set[str]:
    """Write the usage notes of this job; returns the touched claim IDs."""
    counts.setdefault("word_usage_notes", 0)
    audiences = {
        r["name"]: r["id"]
        for r in conn.execute("SELECT id, name FROM audiences WHERE persona_id = ?", (persona_id,))
    }
    default = conn.execute(
        "SELECT name FROM audiences WHERE persona_id = ? AND is_default = 1", (persona_id,)
    ).fetchone()
    said: dict[str, list[str]] = {}
    for ep in episodes:
        name = ep.audience or (default["name"] if default else None)
        aud_id = audiences.get(name) if name else None
        if aud_id and aud_id != NO_AUDIENCE and ep.said:
            said.setdefault(aud_id, []).append(ep.said)
    category_id = _claim_category(conn, persona_id) if said else None
    touched: set[str] = set()
    if category_id is None:
        return touched
    try:
        for aud_id in sorted(said):
            words = _favourites(conn, persona_id, aud_id)
            if not words:
                continue
            with transaction(conn):
                row = store.ensure_builtin(
                    conn,
                    persona_id,
                    name=NAME,
                    scope_kind="audience",
                    scope_id=aud_id,
                    kind="style",
                    statement_template=TEMPLATE,
                )
            if row["status"] != "active" or set(words) == _noted(conn, row["id"]):
                continue
            quotes = {w: snippets(said[aud_id], w) for w in words}
            user = "\n".join(f"WORD: {w}\n" + "\n".join(f"- {s}" for s in quotes[w]) for w in words)
            estimate = -(-(len(SYSTEM) + len(user)) // CHARS_PER_TOKEN) + OUTPUT_TOKENS
            try:
                limiter.check_llm_budget(user_id, persona_id)
                left = limiter.remaining_tokens(user_id, persona_id)
                if left is not None and estimate > left:
                    counts["skipped_llm_budget"] = counts.get("skipped_llm_budget", 0) + 1
                    return touched
                counts["llm_calls"] = counts.get("llm_calls", 0) + 1
                result = await llm.complete_json(
                    purpose="word_usage", system=SYSTEM, user=user, schema=SCHEMA
                )
            except (RateLimited, SpendCap):
                counts["skipped_llm_budget"] = counts.get("skipped_llm_budget", 0) + 1
                return touched
            except LLMUnavailable:
                counts["word_usage_failed"] = counts.get("word_usage_failed", 0) + 1
                return touched
            finally:
                del user
            note = result.data.get("note") if isinstance(result.data, dict) else None
            del result
            own = BatchGuard(
                other_texts=[s for q in quotes.values() for s in q], all_texts=[], vocabulary=set()
            )
            n_snippets = sum(len(q) for q in quotes.values())
            quotes.clear()
            text = " ".join(note.split()) if isinstance(note, str) else ""
            ok = bool(text) and len(text) <= MAX_NOTE
            if ok and (
                guard.check(text) is not None
                or own.check(text) is not None
                or rare.find(text, ignore=words)
            ):
                counts["dropped_by_guard"] = counts.get("dropped_by_guard", 0) + 1
                ok = False
            elif not ok:
                counts["dropped_invalid"] = counts.get("dropped_invalid", 0) + 1
            own.clear()
            with transaction(conn):
                if ok:
                    spec = ClaimSpec(
                        "",
                        "style",
                        text,
                        0.5,
                        n_snippets,
                        None,
                        {"words": float(len(words))},
                        audience_id=aud_id,
                    )
                    _, claim_id = upsert_keyed(
                        conn,
                        persona_id,
                        key=f"builtin:{NAME}:{aud_id}",
                        spec=spec,
                        category_id=category_id,
                        job_id=job_id,
                        with_share=False,
                    )
                    if claim_id is not None:
                        touched.add(claim_id)
                        counts["word_usage_notes"] += 1
                    store.record_run(conn, row["id"], failed=False, claim_id=claim_id)
                _remember(conn, row["id"], words)
    finally:
        said.clear()
    return touched
