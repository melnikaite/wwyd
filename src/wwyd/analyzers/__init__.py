# SPDX-License-Identifier: AGPL-3.0-or-later
"""Analyzers (SPEC §7.2): stored programs over a job's processed episodes that produce
mergeable counters only.

Scope design (docs/habits.md): builtin rows are created lazily, one per (name, exact scope).
A category-scoped builtin sees the job's processed episodes whose category is exactly its
category (not the subtree), so every episode is counted by exactly one row per builtin and
the claim lands in the episode's own category. Audience-scoped builtins (stylometry, M7b)
get the episodes of one audience. LLM analyzers (M7c) are bound to a subtree and run in the
sandbox; :func:`_run_llm_analyzers` is their hook and shares the validate → filter → merge
path of :func:`_apply_output`.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import tzinfo

from wwyd.analyzers.builtin import BUILTINS, Builtin, Context, Item, host_fields
from wwyd.analyzers.claims import claim_key, upsert_keyed
from wwyd.analyzers.contract import ContractError, Output, validate_output
from wwyd.analyzers.keyfilter import TEXT, KeyFilter, TextStats
from wwyd.analyzers.state import merge_state, read_state
from wwyd.db import transaction
from wwyd.ingest.episodes import Episode

__all__ = ["run_analyzers"]

_COUNTERS = (
    "analyzers_run",
    "analyzer_errors",
    "analyzer_keys_dropped",
    "analyzers_skipped_disabled",
    "analyzer_claims_created",
    "analyzer_claims_updated",
    "analyzer_claims_weakened",
    "analyzer_claims_proposed",
)


def _labels(conn: sqlite3.Connection, persona_id: str, category_id: str) -> list[str]:
    """The category's guarded label vocabulary (§7.3); empty before that table exists."""
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT label FROM category_labels WHERE persona_id = ? AND category_id = ?",
                (persona_id, category_id),
            )
        ]
    except sqlite3.OperationalError:
        return []


class _Run:
    def __init__(
        self,
        conn: sqlite3.Connection,
        persona_id: str,
        episodes: Sequence[Episode],
        tz: tzinfo,
        counts: dict[str, int],
        job_id: str | None,
    ) -> None:
        self.conn, self.persona_id, self.tz = conn, persona_id, tz
        self.episodes, self.counts, self.job_id = episodes, counts, job_id
        self.audiences: dict[str, str] = {
            r["name"]: r["id"]
            for r in conn.execute(
                "SELECT id, name FROM audiences WHERE persona_id = ?", (persona_id,)
            )
        }
        self._text_stats: dict[tuple[str, ...], TextStats] = {}
        self.touched: set[str] = set()

    def bump(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def text_stats(self, fields: Sequence[str]) -> TextStats:
        key = tuple(fields)
        if key not in self._text_stats:
            self._text_stats[key] = TextStats(self.episodes, key)
        return self._text_stats[key]

    def key_filter(
        self, classes: Mapping[str, Sequence[str]], fields: Sequence[str], category_id: str | None
    ) -> KeyFilter:
        needs_text = any(TEXT in decl for decl in classes.values())
        labels = _labels(self.conn, self.persona_id, category_id) if category_id else []
        return KeyFilter.build(
            audiences=[*self.audiences, *self.audiences.values()],
            labels=labels,
            text_stats=self.text_stats(fields) if needs_text else None,
        )

    def apply_output(
        self,
        analyzer_id: str,
        output: object,
        classes: Mapping[str, Sequence[str]],
        fields: Sequence[str],
        category_id: str | None,
    ) -> bool:
        """Validate, filter and merge one run's output; False (and counted) if invalid."""
        try:
            valid: Output = validate_output(output)
        except ContractError:
            self.bump("analyzer_errors")
            return False
        kept, dropped = self.key_filter(classes, fields, category_id).apply(valid, classes)
        self.bump("analyzer_keys_dropped", dropped)
        merge_state(self.conn, analyzer_id, kept)
        return True

    def run_builtin(
        self,
        builtin: Builtin,
        *,
        scope_id: str,
        scope_path: str,
        claim_category_id: str | None,
        items: Sequence[Item],
    ) -> None:
        from wwyd.store import analyzers as store

        with transaction(self.conn):
            row = store.ensure_builtin(
                self.conn,
                self.persona_id,
                name=builtin.name,
                scope_kind=builtin.scope_kind,
                scope_id=scope_id,
                kind=builtin.kind,
                statement_template=builtin.statement_template,
            )
        if row["status"] != "active":
            self.bump("analyzers_skipped_disabled")
            return
        ctx = Context(row["id"], builtin.scope_kind, scope_id, scope_path, self.tz, self.audiences)
        try:
            output = builtin.run(items, ctx)
        except Exception:  # noqa: BLE001 - shipped code, but never let it end the job
            output = None
        with transaction(self.conn):
            ok = output is not None and self.apply_output(
                row["id"], output, builtin.key_classes, builtin.text_fields, claim_category_id
            )
            if output is None:
                self.bump("analyzer_errors")
            first_claim: str | None = None
            if ok:
                self.bump("analyzers_run")
                if claim_category_id is not None:
                    first_claim = self._render(builtin, ctx, row["id"], claim_category_id)
            store.record_run(self.conn, row["id"], failed=not ok, claim_id=first_claim)

    def _render(self, builtin: Builtin, ctx: Context, analyzer_id: str, category_id: str):  # noqa: ANN202
        first: str | None = None
        for spec in builtin.render(read_state(self.conn, analyzer_id), ctx):
            outcome, claim_id = upsert_keyed(
                self.conn,
                self.persona_id,
                key=claim_key(builtin.name, ctx.scope_id, spec.facet),
                spec=spec,
                category_id=category_id,
                job_id=self.job_id,
            )
            name = {
                "created": "analyzer_claims_created",
                "updated": "analyzer_claims_updated",
                "weakened": "analyzer_claims_weakened",
                "proposed": "analyzer_claims_proposed",
            }.get(outcome)
            if name:
                self.bump(name)
            if claim_id is not None:
                self.touched.add(claim_id)
                first = first or claim_id
        return first


def _run_llm_analyzers(run: _Run, items: Sequence[Item]) -> None:
    """Hook for active LLM analyzers (M7c): for each ``active`` ``origin = 'llm'`` row, feed
    the processed episodes of its category subtree (host fields included) to the sandbox
    and pass the parsed result to :meth:`_Run.apply_output` with the analyzer's declared key
    classes; then render its ``anl:<id>`` claim. Nothing to do until M7c."""


def run_analyzers(
    conn: sqlite3.Connection,
    *,
    persona_id: str,
    episodes: list[Episode],
    tz: tzinfo,
    counts: dict[str, int],
    job_id: str | None = None,
) -> set[str]:
    """Run the builtin (and active LLM) analyzers over the processed episodes of a job,
    merge their state and re-render their keyed claims; returns the touched claim IDs."""
    for name in _COUNTERS:
        counts.setdefault(name, 0)
    if not episodes:
        return set()
    run = _Run(conn, persona_id, episodes, tz, counts, job_id)
    items = [host_fields(ep, tz, run.audiences) for ep in episodes]
    categories = {
        r["id"]: r["path"]
        for r in conn.execute("SELECT id, path FROM categories WHERE persona_id = ?", (persona_id,))
    }
    by_category: dict[str, list[Item]] = defaultdict(list)
    by_audience: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        if it.episode.category_id in categories:
            by_category[it.episode.category_id].append(it)
        if it.audience_id in run.audiences.values():
            by_audience[it.audience_id].append(it)
    names = {v: k for k, v in run.audiences.items()}
    for builtin in BUILTINS.values():
        if builtin.scope_kind == "category":
            for cat_id in sorted(by_category):
                run.run_builtin(
                    builtin,
                    scope_id=cat_id,
                    scope_path=categories[cat_id],
                    claim_category_id=cat_id,
                    items=by_category[cat_id],
                )
        else:
            claim_cat = next(
                (i for i, p in categories.items() if p == builtin.claim_category), None
            )
            for aud_id in sorted(by_audience):
                run.run_builtin(
                    builtin,
                    scope_id=aud_id,
                    scope_path=names[aud_id],
                    claim_category_id=claim_cat,
                    items=by_audience[aud_id],
                )
    _run_llm_analyzers(run, items)
    return run.touched
