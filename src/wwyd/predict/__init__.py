# SPDX-License-Identifier: AGPL-3.0-or-later
"""``predict``: what would the owner do here? (SPEC §8).

Nothing in this package logs or stores the situation, context or answer. The audit row
(§4.6) carries only codes, numbers and claim IDs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.limits import get_limiter
from wwyd.llm import get_llm
from wwyd.mcp.caller import Caller
from wwyd.predict import answer as answer_step
from wwyd.predict import style as style_step
from wwyd.predict.context import budgeted, fts_match, in_categories, visible_claims
from wwyd.predict.disclosure import build_disclosure
from wwyd.predict.language import answer_line, normalise, script
from wwyd.predict.prompts import (
    COMMIT_RETRY_NOTE,
    COMMITS_KEY,
    INSTRUCTIONS,
    LANGUAGE_RETRY_NOTE,
    RULED_OUT_RETRY_NOTE,
    analyze_user,
    answer_user,
    claim_line,
)
from wwyd.predict.route import MAX_ROUTES, extract_when, route
from wwyd.predict.when import resolve, when_line
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
            result["prediction_id"] = audit_store.record(
                self.conn,
                self.caller.persona_id,
                scope=self.caller.scope,
                outcome=outcome,
                client_name=self.caller.client_name,
                subject_id=getattr(self.caller, "subject_id", None),
                audience_id=self.audience_id,
                audience_fallback=self.audience_fallback,
                confidence=result.get("confidence", result.get("_confidence")),
                used_claims=result.get("used_claims") or (),
                llm_tokens=self.tokens,
                latency_ms=int((time.monotonic() - self.started) * 1000),
            )
        result.pop("_confidence", None)
        log.info("predict finished: %s", outcome)
        return result


def _now() -> datetime:
    """The current time (tests monkeypatch this)."""
    return datetime.now(UTC)


def _local(timezone: str | None) -> tuple[datetime, str]:
    try:
        tz = ZoneInfo(timezone or "UTC")
        name = timezone or "UTC"
    except (ZoneInfoNotFoundError, ValueError):
        tz, name = ZoneInfo("UTC"), "UTC"
    return _now().astimezone(tz), name


def now_line(timezone: str | None) -> str:
    """``Monday 2026-10-19 18:40 (Europe/Berlin)`` in the persona timezone; a missing or
    unknown zone falls back to UTC."""
    local, name = _local(timezone)
    return f"{local.strftime('%A %Y-%m-%d %H:%M')} ({name})"


def resolved_when(text: str, timezone: str | None, weekend: list[str] | None) -> tuple[str, bool]:
    """The When line body for the model's canonical time (``predict.when``), "" when there
    is none or it does not parse, and whether that time is a range ("15:00-17:00")."""
    if not text:
        return "", False
    when = resolve(text, _local(timezone)[0].date(), weekend)
    span = when is not None and "-" in when.time
    return when_line(when, weekend), span


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
    context: str | None = None,
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
    route_when = ""  # the situation's time in canonical English (routing or the when call)
    try:
        # Routing picks up to MAX_ROUTES categories; with that few it cannot narrow anything.
        if (
            sum(c.tokens for c in visible) < SMALL_SET_TOKENS
            or len({c.category for c in visible}) <= MAX_ROUTES
        ):
            selected = visible
            # No routing call to carry the time reference: one small call does (§8.1).
            run.llm_called = True
            route_when, tokens = await extract_when(llm, situation)
            run.tokens += tokens
        else:
            categories = _categories(conn, persona_id, {c.category for c in visible})
            run.llm_called = True
            paths, tokens, route_when = await route(llm, categories, situation)
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
        claims = budgeted(selected, CONTEXT_BUDGET_TOKENS)
        settings = json.loads(
            conn.execute("SELECT settings FROM personas WHERE id = ?", (persona_id,)).fetchone()[0]
            or "{}"
        )
        owner, timezone = conn.execute(
            "SELECT display_name, timezone FROM personas WHERE id = ?", (persona_id,)
        ).fetchone()
        # Style parameters (D22): replies only, from the audience's approved, visible
        # stylometry claim (same SQL filter as the context).
        profile = (
            style_step.load_profile(conn, persona_id, row["id"], include_private=private)
            if kind == "reply"
            else None
        )
        labels = {f"c{i}": c.id for i, c in enumerate(claims, 1)}
        kinds = {label: c.kind for label, c in zip(labels, claims, strict=True)}
        lines = {label: claim_line(label, c) for label, c in zip(labels, claims, strict=True)}
        now = now_line(timezone)
        weekend = settings.get("weekend_days", DEFAULT_SETTINGS["weekend_days"])
        when, span = resolved_when(route_when, timezone, weekend)
        # Call 1, analyze: which claims apply and what they decide (internal only).
        run.llm_called = True
        data, tokens = await answer_step.analyze_call(
            llm,
            analyze_user(
                claims=list(lines.values()),
                context=context,
                situation=situation,
                now=now,
                when=when,
                span=span,
            ),
        )
        run.tokens += tokens
        cats = {label: c.category for label, c in zip(labels, claims, strict=True)}
        found = answer_step.read_analysis(data, kinds, cats)
        if found.verdict == "none":
            return run.finish(
                _escalate("out_of_persona", "no claim of the persona covers this situation")
            )
        if found.verdict == "boundary":
            return run.finish(_escalate("boundary", "a boundary claim covers this topic"))
        # Call 2, answer: the message in the owner's voice, doing what the verdict says.
        lang = found.language
        shown = [x for x in labels if kinds[x] == "style" or x in found.applicable]
        user = answer_user(
            owner=owner,
            audience=row["name"],
            audience_description=row["description"],
            claims=[lines[x] for x in shown],
            context=context,
            kind=kind,
            situation=situation,
            now=now,
            when=when,
            instruction=INSTRUCTIONS[found.verdict].replace("{label}", found.label or ""),
            depends_on=found.depends_on,
            analysis=found.check,
            case=style_step.case_line(profile),
            style=style_step.parameters(profile) if profile is not None else None,
            language=answer_line(lang) if lang else None,
        )
        data, tokens = await answer_step.ask(llm, user, kind=kind)
        run.tokens += tokens
        # Step 7 guards (SPEC §8.1): at most one re-ask, carrying the note of each guard
        # that fired. An "open" answer that still commits escalates; a remaining language
        # mismatch is accepted (a claim may name the language).
        notes = []
        if found.verdict == "open" and data.get(COMMITS_KEY) is True:
            notes.append(COMMIT_RETRY_NOTE)
        if found.verdict == "ruled_out" and data.get(COMMITS_KEY) is False:
            notes.append(RULED_OUT_RETRY_NOTE)
        if _language_mismatch(situation, lang, data):
            notes.append(LANGUAGE_RETRY_NOTE)
        if notes:
            data, tokens = await answer_step.ask(llm, user + "".join(notes), kind=kind)
            run.tokens += tokens
            if found.verdict == "open" and data.get(COMMITS_KEY) is True:
                return run.finish(
                    _escalate(
                        "low_confidence",
                        "the answer committed although it depends on something unknown",
                        _confidence=0.0,
                    )
                )
    except (SpendCap, RateLimited) as exc:
        run.llm_called = run.tokens > 0
        return run.finish(_llm_error(exc))
    except LLMUnavailable as exc:
        return run.finish(_llm_error(exc))

    text = answer_step.answer_text(data)
    if not text:
        return run.finish(
            _escalate("low_confidence", "the model returned no answer", _confidence=0.0)
        )
    # used_claims and confidence are decided in code, not by the model (SPEC §8.1 step 7).
    used = [labels[x] for x in found.applicable]
    used += [labels[x] for x in shown if kinds[x] == "style"]
    if profile is not None:
        used.append(profile.claim_id)
    weights = {x: c.weight for x, c in zip(labels, claims, strict=True)}
    result: dict[str, Any] = {
        "type": "answer",
        "answer": text,
        "depends_on": found.depends_on,
        "confidence": answer_step.score(found, weights, reasked=bool(notes)),
        "used_claims": list(dict.fromkeys(used)),
    }
    threshold = float(settings.get("escalate_below", DEFAULT_SETTINGS["escalate_below"]))
    if result["confidence"] < threshold:
        return run.finish(
            _escalate(
                "low_confidence",
                f"confidence {result['confidence']:.2f} is below {threshold:.2f}",
                _confidence=result["confidence"],
            )
        )
    if profile is not None:
        result["answer"] = await _styled(llm, run, result["answer"], profile)
    result["disclosure"] = build_disclosure(
        owner, answer_step.language(data) or lang, settings.get("disclosure_template")
    )
    return run.finish(result)


def _language_mismatch(situation: str, want: str | None, data: dict[str, Any]) -> bool:
    """The answer is not in the situation's language, judged only by universal signals:
    the Unicode scripts of situation and answer differ, or the answer's own ``language``
    field differs from the language the analyze call named."""
    text = answer_step.answer_text(data)
    if not text:
        return False
    asked, got = script(situation), script(text)
    if asked and got and asked != got:
        return True
    said = normalise(data.get("language"))
    return want is not None and said is not None and said != want


async def _styled(llm, run: _Run, text: str, profile: style_step.StyleProfile) -> str:  # noqa: ANN001
    """Step 8: cut in code, a shorten re-ask only if still far too long, then the rules."""
    text = style_step.compact(text, profile)
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
