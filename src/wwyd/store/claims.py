# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claims: CRUD, review, FTS search and absorb on add (SPEC §10.1)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, NotFound, TooLarge
from wwyd.ids import new_id, now_iso, parse_ts
from wwyd.ingest.guard import find_identifier
from wwyd.store import categories as cats
from wwyd.store.audiences import audience_ids_by_name, get_audience
from wwyd.store.categories import category_ids_by_path, get_category
from wwyd.store.common import (
    KINDS,
    MAX_TEXT,
    STATUSES,
    VISIBILITIES,
    check_category_path,
    check_limit,
    decode_cursor,
    dumps,
    encode_cursor,
    normalize_text,
)

MAX_ADD = 200
MAX_IDS = 1000
MAX_QUERY = 1000
RESTORE_WINDOW = timedelta(days=30)

_SELECT = (
    "SELECT c.*, cat.path AS category FROM claims c JOIN categories cat ON cat.id = c.category_id"
)
_PERIOD = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?\.\.\d{4}-\d{2}(?:-\d{2})?$")
_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_EVIDENCE_KEYS = {"sources", "share", "period", "stats"}


# --- validation -------------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def check_evidence(evidence: Any) -> dict[str, Any]:
    """Evidence is aggregate-only: counters, a share, a period and numeric statistics."""
    if evidence is None:
        return {"sources": {}}
    if not isinstance(evidence, dict) or set(evidence) - _EVIDENCE_KEYS:
        raise Invalid(
            "evidence may only contain sources, share, period and stats", field="evidence"
        )
    out: dict[str, Any] = {}
    sources = evidence.get("sources", {})
    if not isinstance(sources, dict) or not all(
        isinstance(k, str)
        and _SOURCE_NAME.match(k)
        and isinstance(v, int)
        and not isinstance(v, bool)
        and v >= 0
        for k, v in sources.items()
    ):
        raise Invalid("evidence.sources must map source types to counts", field="evidence")
    out["sources"] = dict(sources)
    if "share" in evidence:
        if not _is_number(evidence["share"]) or not 0 <= evidence["share"] <= 1:
            raise Invalid("evidence.share must be between 0 and 1", field="evidence")
        out["share"] = evidence["share"]
    if "period" in evidence:
        if not isinstance(evidence["period"], str) or not _PERIOD.match(evidence["period"]):
            raise Invalid("evidence.period must look like 2026-04..2026-10", field="evidence")
        out["period"] = evidence["period"]
    if "stats" in evidence:
        stats = evidence["stats"]
        if not isinstance(stats, dict) or not all(
            isinstance(k, str)
            and _SOURCE_NAME.match(k)
            and (_is_number(v) or (isinstance(v, list) and all(_is_number(x) for x in v)))
            for k, v in stats.items()
        ):
            raise Invalid(
                "evidence.stats must map names to numbers or number lists", field="evidence"
            )
        out["stats"] = stats
    return out


def check_text(text: Any, *, guard: bool = True) -> str:
    if not isinstance(text, str) or not text.strip():
        raise Invalid("text must be a non-empty string", field="text")
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise Invalid(f"text longer than {MAX_TEXT} characters", field="text")
    if guard and (found := find_identifier(text)) is not None:
        raise Invalid(f"text contains a contact identifier ({found})", field="text", reason="guard")
    return text


def _check_choice(value: Any, choices: Sequence[str], field: str) -> str:
    if value not in choices:
        raise Invalid(f"{field} must be one of {', '.join(choices)}", field=field)
    return value


def _check_confidence(value: Any) -> float:
    if not _is_number(value) or not 0 <= value <= 1:
        raise Invalid("confidence must be between 0 and 1", field="confidence")
    return float(value)


def _resolve_audiences(audiences: Any, by_name: dict[str, str]) -> tuple[bool, list[str]]:
    """Return ``(audiences_all, audience_ids)``; raises Invalid(reason=unknown_audience)."""
    if audiences is None or audiences == "all":
        return True, []
    if not isinstance(audiences, list) or not audiences:
        raise Invalid('audiences must be "all" or a non-empty list of names', field="audiences")
    ids = []
    for name in dict.fromkeys(audiences):
        if name not in by_name:
            raise Invalid(
                f"unknown audience {name!r}", field="audiences", reason="unknown_audience"
            )
        ids.append(by_name[name])
    return False, ids


# --- reading ----------------------------------------------------------------------------


def _audience_names(conn: sqlite3.Connection, claim_ids: Iterable[str]) -> dict[str, list[str]]:
    ids = list(claim_ids)
    result: dict[str, list[str]] = {i: [] for i in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT ca.claim_id, a.name FROM claim_audiences ca"
            f" JOIN audiences a ON a.id = ca.audience_id WHERE ca.claim_id IN ({marks})"
            f" ORDER BY a.name",
            chunk,
        ):
            result[r["claim_id"]].append(r["name"])
    return result


def _to_dicts(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    names = _audience_names(conn, (r["id"] for r in rows if not r["audiences_all"]))
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "category": r["category"],
            "text": r["text"],
            "audiences": "all" if r["audiences_all"] else names[r["id"]],
            "visibility": r["visibility"],
            "status": r["status"],
            "confidence": r["confidence"],
            "evidence": json.loads(r["evidence"]),
            "key": r["key"],
            "origin": r["origin"],
            "proposed_category": r["proposed_category"],
            "superseded_by": r["superseded_by"],
            "archive_reason": r["archive_reason"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "reviewed_at": r["reviewed_at"],
            "archived_at": r["archived_at"],
            "last_evidence_at": r["last_evidence_at"],
        }
        for r in rows
    ]


def _row(conn: sqlite3.Connection, persona_id: str, claim_id: str) -> sqlite3.Row:
    row = conn.execute(
        f"{_SELECT} WHERE c.persona_id = ? AND c.id = ?", (persona_id, claim_id)
    ).fetchone()
    if row is None:
        raise NotFound(f"claim {claim_id!r} not found", id=claim_id)
    return row


def get_claim(conn: sqlite3.Connection, persona_id: str, claim_id: str) -> dict[str, Any]:
    return _to_dicts(conn, [_row(conn, persona_id, claim_id)])[0]


def _filters(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    category: str | None,
    kind: str | None,
    audience: str | None,
    status: str | None,
    include_archived: bool,
) -> tuple[list[str], list[Any]]:
    where, args = ["c.persona_id = ?"], [persona_id]
    status = status or "approved"
    if status == "any":
        if not include_archived:
            where.append("c.status <> 'archived'")
    else:
        _check_choice(status, (*STATUSES, "any"), "status")
        where.append("c.status = ?")
        args.append(status)
    if kind is not None:
        where.append("c.kind = ?")
        args.append(_check_choice(kind, KINDS, "kind"))
    if category is not None:
        check_category_path(category)
        get_category(conn, persona_id, category)
        where.append("(cat.path = ? OR cat.path LIKE ? ESCAPE '\\')")
        escaped = category.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args += [category, escaped + "/%"]
    if audience is not None:
        aud = get_audience(conn, persona_id, audience)
        where.append(
            "(c.audiences_all = 1 OR EXISTS (SELECT 1 FROM claim_audiences ca"
            " WHERE ca.claim_id = c.id AND ca.audience_id = ?))"
        )
        args.append(aud["id"])
    return where, args


def list_claims(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    category: str | None = None,
    kind: str | None = None,
    audience: str | None = None,
    status: str | None = "approved",
    include_archived: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    limit = check_limit(limit)
    after = decode_cursor(cursor)
    where, args = _filters(
        conn,
        persona_id,
        category=category,
        kind=kind,
        audience=audience,
        status=status,
        include_archived=include_archived,
    )
    where.append("c.seq > ?")
    rows = conn.execute(
        f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY c.seq LIMIT ?",
        [*args, after, limit + 1],
    ).fetchall()
    result: dict[str, Any] = {"claims": _to_dicts(conn, rows[:limit])}
    if len(rows) > limit:
        result["next_cursor"] = encode_cursor(rows[limit - 1]["seq"])
    return result


_FTS_TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_query(query: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word quoted, OR-ed together."""
    tokens = list(dict.fromkeys(t for t in _FTS_TOKEN.findall(query) if t))[:32]
    if not tokens:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def text_tokens(text: str) -> frozenset[str]:
    return frozenset(t.casefold() for t in _FTS_TOKEN.findall(text))


def jaccard(a: str, b: str) -> float:
    ta, tb = text_tokens(a), text_tokens(b)
    if not ta or not tb:
        return 1.0 if normalize_text(a) == normalize_text(b) else 0.0
    return len(ta & tb) / len(ta | tb)


def polarity_differs(a: str, b: str) -> bool:
    """True if a negation/polarity marker occurs in only one of the two texts."""
    from wwyd.compact.constants import POLARITY_MARKERS

    return bool((text_tokens(a) ^ text_tokens(b)) & POLARITY_MARKERS)


def find_similar(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    kind: str,
    category_id: str,
    text: str,
    statuses: Sequence[str] = ("pending", "approved", "rejected"),
    exclude: str | None = None,
    limit: int = 5,
) -> list[tuple[sqlite3.Row, float]]:
    """FTS top-``limit`` candidates of the same kind and category, with token-set Jaccard."""
    match = fts_query(text)
    if match is None:
        return []
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        "SELECT c.* FROM claims_fts JOIN claims c ON c.seq = claims_fts.rowid"
        f" WHERE claims_fts MATCH ? AND c.persona_id = ? AND c.kind = ? AND c.category_id = ?"
        f" AND c.status IN ({marks}) AND c.id <> ? ORDER BY bm25(claims_fts) LIMIT ?",
        [match, persona_id, kind, category_id, *statuses, exclude or "", limit],
    ).fetchall()
    return [(r, jaccard(text, r["text"])) for r in rows]


def search_claims(
    conn: sqlite3.Connection,
    persona_id: str,
    query: str,
    *,
    status: str | None = "approved",
    kind: str | None = None,
    audience: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    limit = check_limit(limit)
    if not isinstance(query, str) or len(query) > MAX_QUERY:
        raise Invalid(f"query must be a string of at most {MAX_QUERY} characters", field="query")
    match = fts_query(query)
    if match is None:
        return {"claims": []}
    where, args = _filters(
        conn,
        persona_id,
        category=None,
        kind=kind,
        audience=audience,
        status=status,
        include_archived=False,
    )
    rows = conn.execute(
        f"{_SELECT.replace('SELECT c.*,', 'SELECT c.*, bm25(claims_fts) AS rank,')}"
        f" JOIN claims_fts ON claims_fts.rowid = c.seq"
        f" WHERE claims_fts MATCH ? AND {' AND '.join(where)} ORDER BY rank LIMIT ?",
        [match, *args, limit],
    ).fetchall()
    claims = _to_dicts(conn, rows)
    # bm25() is negative (more negative = better) and its magnitude depends on corpus size
    # (tiny for small personas), so report it relative to the best hit: 1.0 = best match of
    # this result, other hits in (0, 1]. Not comparable across queries.
    best = min((row["rank"] for row in rows), default=0.0)
    for claim, row in zip(claims, rows, strict=True):
        claim["score"] = round(row["rank"] / best, 3) if best < 0 else 1.0
    return {"claims": claims}


# --- writing ----------------------------------------------------------------------------


def _merge_period(a: str | None, b: str | None) -> str | None:
    if not a or not b:
        return a or b
    a_start, a_end = a.split("..")
    b_start, b_end = b.split("..")
    return f"{min(a_start, b_start)}..{max(a_end, b_end)}"


def merge_evidence(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Sum source counters and widen the period; other fields keep the existing value."""
    merged = dict(old)
    sources = dict(old.get("sources", {}))
    for name, count in new.get("sources", {}).items():
        sources[name] = sources.get(name, 0) + count
    merged["sources"] = sources
    period = _merge_period(old.get("period"), new.get("period"))
    if period:
        merged["period"] = period
    return merged


def insert_claim(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    kind: str,
    category_id: str,
    text: str,
    audiences_all: bool,
    audience_ids: Sequence[str],
    visibility: str,
    status: str,
    confidence: float,
    evidence: dict[str, Any],
    origin: str,
    claim_id: str | None = None,
    key: str | None = None,
    proposed_category: str | None = None,
    superseded_by: str | None = None,
    archive_reason: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    reviewed_at: str | None = None,
    archived_at: str | None = None,
    last_evidence_at: str | None = None,
) -> str:
    """Insert one validated claim inside the caller's transaction."""
    ts = now_iso()
    claim_id = claim_id or new_id("clm")
    try:
        conn.execute(
            "INSERT INTO claims(id, persona_id, kind, category_id, text, norm_text, audiences_all,"
            " visibility, status, confidence, evidence, key, origin, proposed_category,"
            " superseded_by, archive_reason, created_at, updated_at, reviewed_at, archived_at,"
            " last_evidence_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                persona_id,
                kind,
                category_id,
                text,
                normalize_text(text),
                int(audiences_all),
                visibility,
                status,
                confidence,
                dumps(evidence),
                key,
                origin,
                proposed_category,
                superseded_by,
                archive_reason,
                created_at or ts,
                updated_at or ts,
                reviewed_at,
                archived_at,
                last_evidence_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        if "claims_key_live" in str(exc) or "claims.persona_id, claims.key" in str(exc):
            raise Conflict(f"another live claim already has key {key!r}", key=key) from None
        if "claims.id" in str(exc):
            raise Conflict(f"claim id {claim_id!r} already exists", id=claim_id) from None
        raise
    conn.executemany(
        "INSERT INTO claim_audiences(claim_id, audience_id) VALUES (?, ?)",
        [(claim_id, aid) for aid in audience_ids],
    )
    return claim_id


def add_claims(
    conn: sqlite3.Connection,
    persona_id: str,
    claims: Sequence[dict[str, Any]],
    *,
    absorb: bool = True,
    allow_approved: bool = False,
    origin: str = "added",
    job_id: str | None = None,
) -> dict[str, Any]:
    """Validate and store claims one by one; bad items are dropped with a reason.

    With ``absorb``, a claim whose text matches an existing pending/approved claim of the
    same kind and category (§10.1: FTS top 5, token-set Jaccard >= 0.6) strengthens it
    instead; one matching a rejected claim is always dropped (negative memory). Borderline
    pairs are left for compaction."""
    if len(claims) > MAX_ADD:
        raise TooLarge(f"at most {MAX_ADD} claims per call", limit=MAX_ADD)
    created: list[str] = []
    absorbed: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    with transaction(conn):
        audiences = audience_ids_by_name(conn, persona_id)
        categories = category_ids_by_path(conn, persona_id)
        for index, item in enumerate(claims):
            try:
                result = _add_one(
                    conn,
                    persona_id,
                    item,
                    audiences,
                    categories,
                    absorb=absorb,
                    allow_approved=allow_approved,
                    origin=origin,
                    job_id=job_id,
                )
            except Invalid as exc:
                entry = {"index": index, "reason": exc.details.get("reason", "invalid")}
                if entry["reason"] == "invalid":
                    entry["detail"] = exc.message
                dropped.append(entry)
                continue
            if result[0] == "created":
                created.append(result[1])
            elif result[0] == "absorbed":
                absorbed.append({"index": index, "into": result[1]})
            else:
                dropped.append({"index": index, "reason": result[1]})
    return {"created": created, "absorbed": absorbed, "dropped": dropped}


def _add_one(
    conn: sqlite3.Connection,
    persona_id: str,
    item: dict[str, Any],
    audiences: dict[str, str],
    categories: dict[str, str],
    *,
    absorb: bool,
    allow_approved: bool,
    origin: str,
    job_id: str | None,
) -> tuple[str, str]:
    if not isinstance(item, dict):
        raise Invalid("each claim must be an object")
    kind = _check_choice(item.get("kind"), KINDS, "kind")
    text = check_text(item.get("text"))
    category = item.get("category")
    if not isinstance(category, str) or category not in categories:
        raise Invalid("unknown category", field="category")
    category_id = categories[category]
    audiences_all, audience_ids = _resolve_audiences(item.get("audiences"), audiences)
    visibility = _check_choice(item.get("visibility", "shared"), VISIBILITIES, "visibility")
    confidence = _check_confidence(item.get("confidence", 0.5))
    evidence = check_evidence(item.get("evidence"))
    status = item.get("status") or "pending"
    _check_choice(status, ("pending", "approved"), "status")
    if status == "approved" and not allow_approved:
        status = "pending"
    proposed = item.get("proposed_category")
    if proposed is not None:
        check_category_path(proposed)

    match = _absorb_target(conn, persona_id, kind, category_id, text)
    if match is not None and match["status"] == "rejected":
        return "dropped", "previously_rejected"
    ts = now_iso()
    if match is not None and absorb:
        from wwyd.compact.constants import absorbed_confidence
        from wwyd.store import changes

        cols: dict[str, Any] = {
            "evidence": dumps(merge_evidence(json.loads(match["evidence"]), evidence)),
            "confidence": absorbed_confidence(match["confidence"], confidence),
            "last_evidence_at": ts,
            "updated_at": ts,
        }
        if (
            match["status"] == "pending"
            and match["origin"] != "owner"
            and match["norm_text"] != normalize_text(text)
        ):
            cols["text"] = text  # pending + pending: the newer wording wins
        changes.apply_updates(
            conn, persona_id, "absorb", {match["id"]: cols}, reason="add_claims", job_id=job_id
        )
        return "absorbed", match["id"]
    claim_id = insert_claim(
        conn,
        persona_id,
        kind=kind,
        category_id=category_id,
        text=text,
        audiences_all=audiences_all,
        audience_ids=audience_ids,
        visibility=visibility,
        status=status,
        confidence=confidence,
        evidence=evidence,
        origin=origin,
        proposed_category=proposed,
        reviewed_at=ts if status == "approved" else None,
        last_evidence_at=ts,
    )
    return "created", claim_id


def _absorb_target(
    conn: sqlite3.Connection, persona_id: str, kind: str, category_id: str, text: str
) -> sqlite3.Row | None:
    """Best duplicate for a new claim; a rejected duplicate wins (it must drop the claim)."""
    from wwyd.compact.constants import DUPLICATE_JACCARD, FTS_CANDIDATES

    exact = conn.execute(
        "SELECT * FROM claims WHERE persona_id = ? AND kind = ? AND category_id = ?"
        " AND norm_text = ? AND status <> 'archived'"
        " ORDER BY status = 'rejected' DESC, seq LIMIT 1",
        (persona_id, kind, category_id, normalize_text(text)),
    ).fetchone()
    if exact is not None:
        return exact
    found = [
        (row, score)
        for row, score in find_similar(
            conn, persona_id, kind=kind, category_id=category_id, text=text, limit=FTS_CANDIDATES
        )
        if score >= DUPLICATE_JACCARD and not polarity_differs(text, row["text"])
    ]
    if not found:
        return None
    found.sort(key=lambda x: (x[0]["status"] == "rejected", x[1]), reverse=True)
    return found[0][0]


def update_claim(
    conn: sqlite3.Connection,
    persona_id: str,
    claim_id: str,
    *,
    text: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    audiences: str | list[str] | None = None,
    visibility: str | None = None,
    confidence: float | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    with transaction(conn):
        row = _row(conn, persona_id, claim_id)
        now = datetime.now(UTC)
        ts = now_iso()
        sets: dict[str, Any] = {}
        restoring = False
        current = row["status"]
        if status is not None:
            _check_choice(status, STATUSES, "status")
        if current == "archived" and status not in ("approved", "pending"):
            if status is None or status == "archived":
                raise Conflict(
                    "archived claims must be restored (status approved or pending)"
                    " before they can be edited",
                    id=claim_id,
                )
            raise Invalid(
                "archived claims can only be restored to approved or pending", field="status"
            )
        if text is not None:
            text = check_text(text)
            if text != row["text"]:
                sets.update(text=text, norm_text=normalize_text(text), origin="owner")
        if kind is not None:
            sets["kind"] = _check_choice(kind, KINDS, "kind")
        if category is not None:
            check_category_path(category)
            sets["category_id"] = get_category(conn, persona_id, category)["id"]
        if visibility is not None:
            sets["visibility"] = _check_choice(visibility, VISIBILITIES, "visibility")
        if confidence is not None:
            sets["confidence"] = _check_confidence(confidence)
        audience_ids: list[str] | None = None
        if audiences is not None:
            try:
                all_flag, audience_ids = _resolve_audiences(
                    audiences, audience_ids_by_name(conn, persona_id)
                )
            except Invalid as exc:
                if exc.details.get("reason") == "unknown_audience":
                    raise NotFound(exc.message, field="audiences") from None
                raise
            sets["audiences_all"] = int(all_flag)
        if status is not None and status != current:
            if current == "archived":
                archived_at = row["archived_at"]
                if archived_at is None or now - parse_ts(archived_at) > RESTORE_WINDOW:
                    raise Conflict(
                        "archived claims can only be restored within 30 days", id=claim_id
                    )
                if (
                    row["key"]
                    and conn.execute(
                        "SELECT 1 FROM claims WHERE persona_id = ? AND key = ? AND status <> 'archived'",
                        (persona_id, row["key"]),
                    ).fetchone()
                ):
                    raise Conflict(
                        f"another live claim already has key {row['key']!r}", id=claim_id
                    )
                sets.update(archived_at=None, archive_reason=None, superseded_by=None)
                restoring = True
            sets["status"] = status
            if status == "archived":
                sets.update(archived_at=ts, archive_reason="owner")
            elif status == "rejected":
                sets.update(evidence="{}", reviewed_at=ts)
            elif status == "approved":
                sets["reviewed_at"] = ts
        from wwyd.store import changes

        before = changes.snapshot(conn, [claim_id]) if restoring else None
        if sets or audience_ids is not None:
            sets["updated_at"] = ts
            assignments = ", ".join(f"{col} = ?" for col in sets)
            conn.execute(
                f"UPDATE claims SET {assignments} WHERE id = ?", [*sets.values(), claim_id]
            )
        if audience_ids is not None:
            conn.execute("DELETE FROM claim_audiences WHERE claim_id = ?", (claim_id,))
            conn.executemany(
                "INSERT INTO claim_audiences(claim_id, audience_id) VALUES (?, ?)",
                [(claim_id, aid) for aid in audience_ids],
            )
        if before is not None:
            changes.record(
                conn,
                persona_id,
                "restore",
                {
                    "claims": [claim_id],
                    "before": before,
                    "after": changes.snapshot(conn, [claim_id]),
                },
                reason="owner",
            )
        return get_claim(conn, persona_id, claim_id)


def _check_ids(ids: Sequence[str], field: str) -> list[str]:
    if len(ids) > MAX_IDS:
        raise TooLarge(f"at most {MAX_IDS} ids per call", field=field, limit=MAX_IDS)
    return list(dict.fromkeys(ids))


def delete_claims(conn: sqlite3.Connection, persona_id: str, ids: Sequence[str]) -> dict[str, Any]:
    """Hard delete. Change-log entries that mention a deleted claim become ``expired``."""
    ids = _check_ids(ids, "ids")
    from wwyd.store import changes

    deleted = 0
    with transaction(conn):
        for claim_id in ids:
            cur = conn.execute(
                "DELETE FROM claims WHERE persona_id = ? AND id = ?", (persona_id, claim_id)
            )
            if cur.rowcount:
                deleted += 1
                changes.expire_for_claims(conn, persona_id, [claim_id])
    return {"deleted": deleted}


def review_claims(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    approve: Sequence[str] = (),
    reject: Sequence[str] = (),
    create_category: bool = False,
) -> dict[str, Any]:
    approve = _check_ids(approve, "approve")
    reject = _check_ids(reject, "reject")
    if set(approve) & set(reject):
        raise Invalid("an id cannot be both approved and rejected", field="approve")
    result: dict[str, Any] = {
        "approved": 0,
        "rejected": 0,
        "applied_changes": 0,
        "dismissed_changes": 0,
        "categories_created": [],
        "not_found": [],
        "skipped": [],
    }
    ts = now_iso()
    with transaction(conn):
        for change_id in (i for i in approve if i.startswith("chg_")):
            _review_change(conn, persona_id, change_id, True, result)
        for change_id in (i for i in reject if i.startswith("chg_")):
            _review_change(conn, persona_id, change_id, False, result)
        approve = [i for i in approve if not i.startswith("chg_")]
        reject = [i for i in reject if not i.startswith("chg_")]
        for claim_id in approve:
            row = _lookup(conn, persona_id, claim_id, result)
            if row is None:
                continue
            if row["status"] not in ("pending", "rejected"):
                result["skipped"].append(claim_id)
                continue
            sets: dict[str, Any] = {"status": "approved", "reviewed_at": ts, "updated_at": ts}
            proposed = row["proposed_category"]
            if create_category and proposed:
                existed = proposed in category_ids_by_path(conn, persona_id)
                sets["category_id"] = cats.create_category(
                    conn, persona_id, proposed, create_parents=True
                )
                sets["proposed_category"] = None
                if not existed:
                    result["categories_created"].append(proposed)
            conn.execute(
                f"UPDATE claims SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                [*sets.values(), claim_id],
            )
            result["approved"] += 1
        for claim_id in reject:
            row = _lookup(conn, persona_id, claim_id, result)
            if row is None:
                continue
            if row["status"] not in ("pending", "approved"):
                result["skipped"].append(claim_id)
                continue
            conn.execute(
                "UPDATE claims SET status = 'rejected', evidence = '{}', reviewed_at = ?,"
                " updated_at = ? WHERE id = ?",
                (ts, ts, claim_id),
            )
            result["rejected"] += 1
    return result


def _review_change(
    conn: sqlite3.Connection,
    persona_id: str,
    change_id: str,
    approve: bool,
    result: dict[str, Any],
) -> None:
    from wwyd.store import changes

    try:
        if approve:
            if changes.apply_proposal(conn, persona_id, change_id):
                result["applied_changes"] += 1
            else:
                result["skipped"].append(change_id)
        elif changes.dismiss_proposal(conn, persona_id, change_id):
            result["dismissed_changes"] += 1
        else:
            result["skipped"].append(change_id)
    except NotFound:
        result["not_found"].append(change_id)
    except Conflict:
        # Its claims changed since it was proposed; it can no longer apply as shown.
        conn.execute(
            "UPDATE changes SET state = 'dismissed' WHERE persona_id = ? AND id = ?",
            (persona_id, change_id),
        )
        result["skipped"].append(change_id)


def _lookup(
    conn: sqlite3.Connection, persona_id: str, item_id: str, result: dict[str, Any]
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM claims WHERE persona_id = ? AND id = ?", (persona_id, item_id)
    ).fetchone()
    if row is None:
        result["not_found"].append(item_id)
    return row
