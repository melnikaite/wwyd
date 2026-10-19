# SPDX-License-Identifier: AGPL-3.0-or-later
"""Record schemas for ``ingest_raw`` (SPEC §6.1) and parsing into typed, in-memory objects.

Every record object has ``additionalProperties: false``: there is no field for names,
phones, emails, titles, attendees or locations (P2, P3). Validation errors report the
record index and the JSON path only, never the offending value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from jsonschema import Draft202012Validator

from wwyd.errors import Invalid

MAX_TEXT = 4000
MAX_LONG_TEXT = 20000
MAX_TURNS = 500

_DT = {"type": "string", "minLength": 10, "maxLength": 40}
_AUDIENCE = {"type": "string", "minLength": 1, "maxLength": 64}

RECORD_SCHEMAS: dict[str, dict[str, Any]] = {
    "message": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "from_owner", "text", "ts", "thread"],
        "properties": {
            "type": {"const": "message"},
            "from_owner": {"type": "boolean"},
            "audience": _AUDIENCE,
            "text": {"type": "string", "maxLength": MAX_TEXT},
            "ts": _DT,
            "thread": {"type": "string", "minLength": 1, "maxLength": 64},
            "position": {"type": "integer"},
        },
    },
    "call_transcript": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "ts", "turns"],
        "properties": {
            "type": {"const": "call_transcript"},
            "audience": _AUDIENCE,
            "ts": _DT,
            "turns": {
                "type": "array",
                "maxItems": MAX_TURNS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["speaker", "text"],
                    "properties": {
                        "speaker": {"enum": ["owner", "other"]},
                        "text": {"type": "string", "maxLength": MAX_TEXT},
                    },
                },
            },
        },
    },
    "calendar_event": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "start", "end", "attendees", "response"],
        "properties": {
            "type": {"const": "calendar_event"},
            "start": _DT,
            "end": _DT,
            "attendees": {"type": "integer", "minimum": 0},
            "response": {"enum": ["accepted", "declined", "tentative", "organizer", "none"]},
            "recurring": {"type": "boolean"},
            "online": {"type": "boolean"},
        },
    },
    "decision": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "situation", "choice"],
        "properties": {
            "type": {"const": "decision"},
            "situation": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
            "choice": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
            "audience": _AUDIENCE,
            "ts": _DT,
        },
    },
    "note": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "text"],
        "properties": {
            "type": {"const": "note"},
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_LONG_TEXT},
            "ts": _DT,
        },
    },
}
RECORD_TYPES = tuple(RECORD_SCHEMAS)
_VALIDATORS = {name: Draft202012Validator(schema) for name, schema in RECORD_SCHEMAS.items()}


@dataclass
class Message:
    from_owner: bool
    audience: str
    text: str
    ts: datetime
    thread: str
    position: int | None = None
    index: int = -1  # position in the submitted batch (progress tracking, SPEC §7.1)


@dataclass
class Transcript:
    audience: str
    ts: datetime
    turns: list[tuple[str, str]] = field(default_factory=list)  # (speaker, text)
    index: int = -1


@dataclass
class Event:
    start: datetime
    end: datetime
    attendees: int
    response: str
    recurring: bool = False
    online: bool = False
    index: int = -1


@dataclass
class Decision:
    situation: str
    choice: str
    audience: str | None  # None: no audience given, the rule applies to all
    ts: datetime | None = None
    index: int = -1


@dataclass
class Note:
    text: str
    ts: datetime | None = None
    index: int = -1


@dataclass
class Batch:
    """A parsed batch; held only by the job object and dropped when the job ends (P1)."""

    messages: list[Message] = field(default_factory=list)
    transcripts: list[Transcript] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    unknown_audiences: int = 0

    def _lists(self) -> list[list[Any]]:
        return [self.messages, self.transcripts, self.events, self.decisions, self.notes]

    def stamps(self) -> dict[int, datetime | None]:
        """Batch index -> record timestamp (``start`` for events; None when absent)."""
        out: dict[int, datetime | None] = {}
        for items in self._lists():
            for r in items:
                out[r.index] = r.start if isinstance(r, Event) else r.ts
        return out

    def drop_seen(self, last_ts: datetime) -> int:
        """Drop records with a timestamp <= ``last_ts`` (source watermark); returns how many.
        Records without a timestamp are kept."""
        dropped = 0
        for items in self._lists():
            kept = [
                r
                for r in items
                if (ts := (r.start if isinstance(r, Event) else r.ts)) is None or ts > last_ts
            ]
            dropped += len(items) - len(kept)
            items[:] = kept
        return dropped

    def subset(self, exclude: set[int]) -> Batch:
        """A view without the records at ``exclude`` (shares record objects)."""
        out = Batch(counts=dict(self.counts), unknown_audiences=self.unknown_audiences)
        for src, dst in zip(self._lists(), out._lists(), strict=True):
            dst.extend(r for r in src if r.index not in exclude)
        return out

    def clear(self) -> None:
        self.messages.clear()
        self.transcripts.clear()
        self.events.clear()
        self.decisions.clear()
        self.notes.clear()


def _invalid(index: int, path: str, reason: str) -> Invalid:
    return Invalid(f"record {index}: {reason} at {path or '(record)'}", index=index, path=path)


def validate_record(index: int, record: Any) -> str:
    """Validate one record against its schema; returns its type."""
    if not isinstance(record, dict):
        raise _invalid(index, "", "not an object")
    kind = record.get("type")
    if kind not in _VALIDATORS:
        raise _invalid(index, "type", "unknown record type")
    error = next(iter(_VALIDATORS[kind].iter_errors(record)), None)
    if error is not None:
        # Never use error.message: it quotes the offending value.
        if error.validator == "additionalProperties":
            # The path could lead through caller-chosen keys: report the record index only.
            raise _invalid(index, "", "unknown field")
        path = ".".join(str(p) for p in error.absolute_path)
        raise _invalid(index, path, f"failed {error.validator}")
    return kind


def parse_dt(value: str, tz: tzinfo, index: int, path: str) -> datetime:
    """ISO 8601; an explicit offset is honoured, a naive value is read in the persona's zone."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid(index, path, "not an ISO 8601 datetime") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def parse_batch(
    records: list[Any], *, tz: tzinfo, audiences: set[str], default_audience: str
) -> Batch:
    """Validate every record and build a :class:`Batch`; any invalid record fails the call."""
    batch = Batch(counts={name: 0 for name in RECORD_TYPES})

    def audience(value: str | None) -> str:
        if value is None or value == "default":  # the reserved alias (SPEC §4.3)
            return default_audience
        if value in audiences:
            return value
        batch.unknown_audiences += 1
        return default_audience

    for i, rec in enumerate(records):
        kind = validate_record(i, rec)
        batch.counts[kind] += 1
        if kind == "message":
            batch.messages.append(
                Message(
                    from_owner=rec["from_owner"],
                    audience=audience(rec.get("audience")),
                    text=rec["text"],
                    ts=parse_dt(rec["ts"], tz, i, "ts"),
                    thread=rec["thread"],
                    position=rec.get("position"),
                    index=i,
                )
            )
        elif kind == "call_transcript":
            batch.transcripts.append(
                Transcript(
                    audience=audience(rec.get("audience")),
                    ts=parse_dt(rec["ts"], tz, i, "ts"),
                    turns=[(t["speaker"], t["text"]) for t in rec["turns"]],
                    index=i,
                )
            )
        elif kind == "calendar_event":
            start = parse_dt(rec["start"], tz, i, "start")
            end = parse_dt(rec["end"], tz, i, "end")
            if end < start:
                raise _invalid(i, "end", "end before start")
            batch.events.append(
                Event(
                    start=start,
                    end=end,
                    attendees=rec["attendees"],
                    response=rec["response"],
                    recurring=rec.get("recurring", False),
                    online=rec.get("online", False),
                    index=i,
                )
            )
        elif kind == "decision":
            batch.decisions.append(
                Decision(
                    situation=rec["situation"],
                    choice=rec["choice"],
                    audience=audience(rec["audience"]) if "audience" in rec else None,
                    ts=parse_dt(rec["ts"], tz, i, "ts") if "ts" in rec else None,
                    index=i,
                )
            )
        else:
            batch.notes.append(
                Note(
                    text=rec["text"],
                    ts=parse_dt(rec["ts"], tz, i, "ts") if "ts" in rec else None,
                    index=i,
                )
            )
    return batch
