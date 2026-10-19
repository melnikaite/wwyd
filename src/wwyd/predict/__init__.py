# SPDX-License-Identifier: AGPL-3.0-or-later
"""``predict``: what would the owner do here? (SPEC §8).

Nothing in this package logs or stores the situation, facts or answer. The audit row
(§4.6) carries only codes and numbers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.limits import get_limiter
from wwyd.llm import get_llm
from wwyd.mcp.caller import Caller
from wwyd.predict import answer as answer_step
from wwyd.predict import style as style_step
from wwyd.predict.context import budgeted, fts_match, in_categories, visible_claims
from wwyd.predict.disclosure import build_disclosure
from wwyd.predict.prompts import answer_user, claim_line
from wwyd.predict.route import MAX_ROUTES, route
from wwyd.store import audit as audit_store
from wwyd.store.audiences import DEFAULT_ALIAS, find_audience
from wwyd.store.personas import DEFAULT_SETTINGS

log = logging.getLogger(__name__)

SMALL_SET_TOKENS = 1500  # below this the whole visible set is the context (no routing)
CONTEXT_BUDGET_TOKENS = 2500
CORE_KINDS = ("style", "boundary")


class _Run:
    """Per-call bookkeeping for the audit row."""

    def __init__(self, conn: sqlite3.Connection, caller: Caller, audit: bool) -> None:
        self.conn = conn
        self.caller = caller
        self.audit = audit
        self.started = time.monotonic()
        self.tokens = 0
        self.llm_called = False
        self.audience_id: str | None = None
        self.audience_fallback = False
        self.hints: list[str] = []

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        result["hints"] = self.hints
        if result["type"] == "escalate":
            outcome = f"escalate:{result['escalate']}"
        else:
            outcome = result["type"]
        if self.llm_called and self.audience_id is not None:
            get_limiter(self.conn).record_predict(
                self.caller.user_id, self.caller.persona_id, self.audience_id
            )
        if self.audit:
            audit_store.record(
                self.conn,
                self.caller.persona_id,
                scope=self.caller.scope,
                outcome=outcome,
                client_name=self.caller.client_name,
                subject_id=getattr(self.caller, "subject_id", None),
                audience_id=self.audience_id,
                audience_fallback=self.audience_fallback,
                confidence=result.get("confidence", result.get("_confidence")),
                llm_tokens=self.tokens,
                latency_ms=int((time.monotonic() - self.started) * 1000),
            )
        result.pop("_confidence", None)
        log.info("predict finished: %s", outcome)
        return result


def _escalate(code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"type": "escalate", "escalate": code, "reason": reason, **extra}


def _llm_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, SpendCap):
        return _escalate("spend_cap", "the monthly LLM spend cap is reached")
    if isinstance(exc, RateLimited):
        return _escalate("rate_limited", "LLM usage limit reached; try again later")
    return _escalate("llm_unavailable", "the LLM backend is not available")


async def predict(
    conn: sqlite3.Connection,
    caller: Caller,
    *,
    situation: str,
    audience: str,
    facts: list[dict[str, str | None]] | None = None,
    kind: str = "reply",
    include_private: bool = False,
    audit: bool = True,
) -> dict[str, Any]:
    run = _Run(conn, caller, audit)
    persona_id = caller.persona_id

    # Resolve the audience (unknown name -> default) and check the caller may use it.
    # The "default" alias resolves to the default audience without a fallback (SPEC §4.3).
    row = find_audience(conn, persona_id, audience)
    if row is None:
        row = find_audience(conn, persona_id, DEFAULT_ALIAS)
        run.audience_fallback = True
        if row is None:
            # The store keeps exactly one default audience; this guards a hand-edited DB.
            return run.finish(_escalate("out_of_persona", "the persona has no default audience"))
        if caller.scope == "owner":
            run.hints.append(f"unknown audience; used the default audience {row['name']!r}")
        else:  # do not name an audience this token may not be permitted to see
            run.hints.append("unknown audience; used the default audience")
    run.audience_id = row["id"]
    if caller.scope != "owner" and row["name"] not in (caller.audiences or ()):
        return run.finish(
            _escalate("audience_not_permitted", "this access may not answer for this audience")
        )
    private = bool(include_private and caller.scope == "owner")
    if include_private and not private:
        run.hints.append("include_private is only honoured for the owner; ignored")

    # Limits: no LLM call when over.
    limiter = get_limiter(conn)
    try:
        limiter.check_predict(caller.user_id, persona_id, row["id"])
    except RateLimited:
        return run.finish(_escalate("rate_limited", "daily predict limit reached"))

    visible = visible_claims(conn, persona_id, row["id"], include_private=private)
    if not visible:
        return run.finish(
            _escalate("out_of_persona", "no approved claims are visible for this audience")
        )

    llm = get_llm(conn, persona_id)
    try:
        # Routing picks up to MAX_ROUTES categories; with that few it cannot narrow anything.
        if (
            sum(c.tokens for c in visible) < SMALL_SET_TOKENS
            or len({c.category for c in visible}) <= MAX_ROUTES
        ):
            selected = visible
        else:
            categories = _categories(conn, persona_id, {c.category for c in visible})
            run.llm_called = True
            paths, tokens = await route(llm, categories, situation)
            run.tokens += tokens
            if paths:
                selected = [c for c in visible if in_categories(c, paths)]
            else:
                hits = set(
                    fts_match(conn, persona_id, row["id"], situation, include_private=private)
                )
                selected = [c for c in visible if c.id in hits]
            core = [c for c in visible if c.kind in CORE_KINDS]
            selected = list({c.id: c for c in [*core, *selected]}.values())
        context = budgeted(selected, CONTEXT_BUDGET_TOKENS)
        settings = json.loads(
            conn.execute("SELECT settings FROM personas WHERE id = ?", (persona_id,)).fetchone()[0]
            or "{}"
        )
        owner = conn.execute(
            "SELECT display_name FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()[0]
        # Style parameters (D22): replies only, from the audience's approved, visible
        # stylometry claim (same SQL filter as the context).
        profile = (
            style_step.load_profile(conn, persona_id, row["id"], include_private=private)
            if kind == "reply"
            else None
        )
        labels = {f"c{i}": c.id for i, c in enumerate(context, 1)}
        user = answer_user(
            owner=owner,
            audience=row["name"],
            audience_description=row["description"],
            claims=[claim_line(label, c) for label, c in zip(labels, context, strict=True)],
            facts=facts or [],
            kind=kind,
            situation=situation,
            style=style_step.parameters(profile) if profile is not None else None,
        )
        run.llm_called = True
        data, tokens = await answer_step.ask(llm, user, kind=kind)
        run.tokens += tokens
    except (SpendCap, RateLimited) as exc:
        run.llm_called = run.tokens > 0
        return run.finish(_llm_error(exc))
    except LLMUnavailable as exc:
        return run.finish(_llm_error(exc))

    result = answer_step.interpret(
        data,
        labels,
        has_boundary=any(c.kind == "boundary" for c in context),
        unavailable=[f for f in facts or [] if f.get("value") is None],
    )
    rules = {c.id for c in context if c.kind == "decision_rule"}
    if (
        result["type"] == "answer"
        and any(f.get("value") is None for f in facts or [])
        and rules.intersection(result["used_claims"])
    ):
        # Only decision rules ask for facts; answering through one while the harness said
        # a fact is not available is a guess (small models confidently decline instead).
        return run.finish(
            _escalate(
                "low_confidence",
                "a fact the decision rule needs is not available",
                _confidence=result["confidence"],
            )
        )
    if result["type"] == "answer":
        threshold = float(settings.get("escalate_below", DEFAULT_SETTINGS["escalate_below"]))
        if result["confidence"] < threshold:
            return run.finish(
                _escalate(
                    "low_confidence",
                    f"confidence {result['confidence']:.2f} is below {threshold:.2f}",
                    _confidence=result["confidence"],
                )
            )
        language = result.pop("language")
        if profile is not None:
            result["answer"] = await _styled(llm, run, result["answer"], profile)
        result["disclosure"] = build_disclosure(
            owner, language, settings.get("disclosure_template")
        )
    return run.finish(result)


async def _styled(llm, run: _Run, text: str, profile: style_step.StyleProfile) -> str:  # noqa: ANN001
    """Step 8: one shorten re-ask if far too long, then the deterministic rules."""
    if style_step.too_long(text, profile):
        shorter, tokens = await style_step.shorten(llm, text, profile)
        run.tokens += tokens
        text = shorter or text
    return style_step.apply_rules(text, profile)


def _categories(
    conn: sqlite3.Connection, persona_id: str, paths: set[str]
) -> list[tuple[str, str]]:
    """Paths (and descriptions) that hold visible claims, plus their ancestors."""
    wanted = set()
    for path in paths:
        parts = path.split("/")
        wanted.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    rows = conn.execute(
        "SELECT path, description FROM categories WHERE persona_id = ? ORDER BY path",
        (persona_id,),
    ).fetchall()
    return [(r["path"], r["description"]) for r in rows if r["path"] in wanted]


__all__ = ["predict"]
