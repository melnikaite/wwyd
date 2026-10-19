# SPDX-License-Identifier: AGPL-3.0-or-later
"""Episodes, the one record shape of ``ingest_raw`` (SPEC §6.1, D17).

A parsed episode lives only in process memory (the job object) and is dropped when the job
ends (P1). Times are already converted to the persona timezone; audience and category are
resolved at preparation (a missing category by the job's placement step, SPEC §4.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from jsonschema import Draft202012Validator

from wwyd.errors import Invalid

MAX_WHERE = 100
MAX_SITUATION = 8000
MAX_DECISION = 4000
MAX_SAID = 20000

_DT = {"type": "string", "minLength": 10, "maxLength": 40}
# A prediction_id (SPEC §6.5): an audit row ID, ``adt_`` + 16 base32 characters (wwyd.ids).
PREDICTION_ID = {"type": "string", "maxLength": 20, "pattern": "^adt_[a-z2-7]{16}$"}

EPISODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "anyOf": [{"required": ["decision"]}, {"required": ["said"]}],
    "dependentRequired": {"situation_at": ["situation"], "corrects": ["situation"]},
    "properties": {
        "when": {
            "oneOf": [
                _DT,
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["start"],
                    "properties": {"start": _DT, "end": _DT},
                },
            ]
        },
        "situation_at": _DT,
        "corrects": PREDICTION_ID,
        "where": {"type": "string", "minLength": 1, "maxLength": MAX_WHERE},
        "audience": {"type": "string", "minLength": 1, "maxLength": 64},
        "category": {"type": "string", "minLength": 1, "maxLength": 200},
        "situation": {"type": "string", "minLength": 1, "maxLength": MAX_SITUATION},
        "decision": {"type": "string", "minLength": 1, "maxLength": MAX_DECISION},
        "said": {"type": "string", "minLength": 1, "maxLength": MAX_SAID},
    },
}
_VALIDATOR = Draft202012Validator(EPISODE_SCHEMA)
CLASSES = ("statement", "reply", "decision")


@dataclass
class Episode:
    index: int  # position in the submitted batch (progress tracking, SPEC §7.1)
    category_id: str  # "" until resolved (or placed, SPEC §4.4)
    category: str | None  # resolved path; None until placed when the client sent none
    audience: str | None  # resolved audience name; None when the episode named none
    start: datetime | None = None  # `when` or `when.start` (owner acted), persona timezone
    end: datetime | None = None  # `when.end`
    situation_at: datetime | None = None  # when the situation reached the owner (D23)
    corrects: str | None = None  # the prediction_id the client sent (in memory only)
    # Claim IDs the corrected prediction used; None unless ``corrects`` resolved (§7.1).
    corrected_claims: list[str] | None = None
    where: str | None = None
    situation: str | None = None
    decision: str | None = None
    said: str | None = None  # the only owner text (stylometry, guard)
    labels: dict[str, str] = field(default_factory=dict)  # §7.3, in memory only
    tags: list[str] = field(default_factory=list)  # §7.3, in memory only

    @property
    def is_statement(self) -> bool:
        """``said`` with neither ``situation`` nor an explicit ``audience``: about self."""
        return self.said is not None and self.situation is None and self.audience is None

    @property
    def is_reply(self) -> bool:
        """``said`` with a ``situation`` (a reply), or with an explicit ``audience`` and no
        ``situation``: an outgoing message the owner started (SPEC §6.1). Both feed the
        reply/style side for that audience."""
        return self.said is not None and (self.situation is not None or self.audience is not None)

    @property
    def is_outgoing(self) -> bool:
        """A reply-class episode the owner started: ``said`` + ``audience``, no situation."""
        return self.is_reply and self.situation is None

    @property
    def is_decision(self) -> bool:
        return self.decision is not None and self.situation is not None

    @property
    def is_correction(self) -> bool:
        """``corrects`` resolved against this persona's audit rows (SPEC §6.1, D23)."""
        return self.corrected_claims is not None

    @property
    def classes(self) -> tuple[str, ...]:
        """The pass classes (SPEC §7.1). An episode with ``decision`` but no ``situation``
        (a self-describing choice: "watched to the end, 4/5") goes to the decision pass as
        well: the decision text carries its own context. With ``said`` too it is also a
        statement, or a reply (an outgoing message) when an audience is named."""
        out = []
        if self.is_statement:
            out.append("statement")
        if self.is_reply:
            out.append("reply")
        if self.decision is not None:
            out.append("decision")
        return tuple(out)

    @property
    def duration_s(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        # Aware datetimes in one ZoneInfo subtract as wall-clock time: go through UTC.
        return (self.end.astimezone(UTC) - self.start.astimezone(UTC)).total_seconds()

    @property
    def latency_s(self) -> float | None:
        """Response latency: the owner's action (``when`` / ``when.start``) minus
        ``situation_at`` (SPEC §6.1). None when either is missing or it is negative."""
        if self.start is None or self.situation_at is None:
            return None
        delta = (self.start.astimezone(UTC) - self.situation_at.astimezone(UTC)).total_seconds()
        return delta if delta >= 0 else None


@dataclass
class Batch:
    """A parsed batch; held only by the job object and dropped when the job ends (P1)."""

    episodes: list[Episode] = field(default_factory=list)
    unknown_audiences: int = 0

    def counts(self) -> dict[str, int]:
        """Episodes by class (an episode can be in two classes) and in total."""
        out = {"total": len(self.episodes), **{c: 0 for c in CLASSES}}
        for ep in self.episodes:
            for c in ep.classes:
                out[c] += 1
        return out

    def stamps(self) -> dict[int, datetime | None]:
        """Batch index -> episode timestamp (``when`` or ``when.start``; None when absent)."""
        return {ep.index: ep.start for ep in self.episodes}

    def drop_seen(self, last_ts: datetime) -> int:
        """Drop episodes with a timestamp <= ``last_ts`` (source watermark); returns how
        many. Episodes without a timestamp are kept."""
        kept = [ep for ep in self.episodes if ep.start is None or ep.start > last_ts]
        dropped = len(self.episodes) - len(kept)
        self.episodes[:] = kept
        return dropped

    def subset(self, exclude: set[int]) -> Batch:
        """A view without the episodes at ``exclude`` (shares the episode objects)."""
        return Batch(
            [ep for ep in self.episodes if ep.index not in exclude], self.unknown_audiences
        )

    def clear(self) -> None:
        self.episodes.clear()


def _invalid(index: int, path: str, reason: str) -> Invalid:
    return Invalid(f"episode {index}: {reason} at {path or '(episode)'}", index=index, path=path)


def validate_episode(index: int, episode: Any) -> None:
    """Validate one episode against :data:`EPISODE_SCHEMA`. Errors carry the index and the
    JSON path only, never a value or a caller-chosen key."""
    if not isinstance(episode, dict):
        raise _invalid(index, "", "not an object")
    error = next(iter(_VALIDATOR.iter_errors(episode)), None)
    if error is None:
        return
    # Never use error.message: it quotes the offending value.
    if error.validator == "additionalProperties":
        # The path could lead through caller-chosen keys: report the episode index only.
        raise _invalid(index, "", "unknown field")
    if error.validator == "anyOf":
        raise _invalid(index, "", "needs decision or said")
    if error.validator == "dependentRequired":
        # The schema's own keys only (situation_at, corrects): no caller-chosen text.
        field_name = next(k for k in ("situation_at", "corrects") if k in episode)
        raise _invalid(index, field_name, "needs situation")
    if error.validator == "oneOf":
        path = ".".join(str(p) for p in error.absolute_path)
        # Explain the nested failure without values (an unknown key inside `when`, too).
        nested = [e for e in error.context or () if e.validator != "type"]
        if any(e.validator == "additionalProperties" for e in nested):
            raise _invalid(index, "", "unknown field")
        raise _invalid(index, path, "not a datetime or {start, end?}")
    path = ".".join(str(p) for p in error.absolute_path)
    raise _invalid(index, path, f"failed {error.validator}")


def parse_dt(value: str, tz: tzinfo, index: int, path: str) -> datetime:
    """ISO 8601, converted to the persona timezone; a naive value is read in that zone."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid(index, path, "not an ISO 8601 datetime") from None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def parse_batch(
    raw: list[Any],
    *,
    tz: tzinfo,
    audiences: set[str],
    default_audience: str,
    normalize_category: Any,
) -> Batch:
    """Validate every episode and build a :class:`Batch`; any invalid episode fails the
    call. ``category`` is normalised (``normalize_category``) but not yet resolved:
    ``category_id`` is empty until the job resolves it against the tree; an episode
    without one keeps ``category=None`` until placement (SPEC §4.4)."""
    batch = Batch()

    def audience(value: str | None) -> str | None:
        if value is None:
            return None  # replies use the default audience; statements/decisions apply to all
        if value == "default":  # the reserved alias (SPEC §4.3)
            return default_audience
        if value in audiences:
            return value
        batch.unknown_audiences += 1
        return default_audience

    for i, rec in enumerate(raw):
        validate_episode(i, rec)
        category = None
        if "category" in rec:
            try:
                category = normalize_category(rec["category"])
            except Invalid:
                raise _invalid(i, "category", "not a valid category path") from None
        start = end = None
        when = rec.get("when")
        if isinstance(when, str):
            start = parse_dt(when, tz, i, "when")
        elif isinstance(when, dict):
            start = parse_dt(when["start"], tz, i, "when.start")
            if "end" in when:
                end = parse_dt(when["end"], tz, i, "when.end")
                if end < start:
                    raise _invalid(i, "when.end", "end before start")
        situation_at = None
        if "situation_at" in rec:
            situation_at = parse_dt(rec["situation_at"], tz, i, "situation_at")
        batch.episodes.append(
            Episode(
                index=i,
                category_id="",
                category=category,
                audience=audience(rec.get("audience")),
                start=start,
                end=end,
                where=rec.get("where"),
                situation=rec.get("situation"),
                decision=rec.get("decision"),
                said=rec.get("said"),
                situation_at=situation_at,
                corrects=rec.get("corrects"),
            )
        )
    return batch
