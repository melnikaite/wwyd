# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claim selection for predict and get_persona. Visibility is decided here, in SQL (§8.3)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from wwyd.store.evidence import context_rank


@dataclass(frozen=True)
class Claim:
    id: str
    kind: str
    category: str
    text: str
    confidence: float
    weight: float  # context rank (store.evidence.context_rank)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.line())

    def line(self) -> str:
        return f"[{self.id}] ({self.kind}, {self.category}) {self.text}"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_BASE = (
    "SELECT c.seq, c.id, c.kind, c.text, c.confidence, c.evidence, k.path AS category"
    " FROM claims c JOIN categories k ON k.id = c.category_id"
    " WHERE c.persona_id = :persona AND c.status = 'approved'"
)
# §8.3: applies to this audience, and owner_only only when explicitly allowed.
_AUDIENCE = (
    " AND (c.audiences_all = 1 OR EXISTS (SELECT 1 FROM claim_audiences ca"
    "      WHERE ca.claim_id = c.id AND ca.audience_id = :audience))"
)
_VISIBILITY = " AND (c.visibility = 'shared' OR :private = 1)"


def _claims(rows: list[sqlite3.Row]) -> list[Claim]:
    return [
        Claim(
            r["id"],
            r["kind"],
            r["category"],
            r["text"],
            r["confidence"],
            context_rank(r["confidence"], r["evidence"]),
        )
        for r in rows
    ]


def visible_claims(
    conn: sqlite3.Connection, persona_id: str, audience_id: str, *, include_private: bool
) -> list[Claim]:
    """Approved claims that apply to the audience and are visible through predict.

    ``include_private`` must already be ``requested AND caller is owner``.
    """
    rows = conn.execute(
        _BASE + _AUDIENCE + _VISIBILITY + " ORDER BY c.seq",
        {"persona": persona_id, "audience": audience_id, "private": int(include_private)},
    ).fetchall()
    return _claims(rows)


def persona_claims(
    conn: sqlite3.Connection, persona_id: str, audience_id: str | None
) -> list[Claim]:
    """Approved claims of every visibility (owner/read view), optionally for one audience."""
    sql = _BASE + (_AUDIENCE if audience_id else "") + " ORDER BY c.seq"
    return _claims(conn.execute(sql, {"persona": persona_id, "audience": audience_id}).fetchall())


_WORD = re.compile(r"\w{3,}", re.UNICODE)


def fts_terms(text: str, limit: int = 24) -> list[str]:
    seen: dict[str, None] = {}
    for word in _WORD.findall(text.casefold()):
        if not word.isdigit():
            seen.setdefault(word, None)
        if len(seen) >= limit:
            break
    return list(seen)


def fts_match(
    conn: sqlite3.Connection,
    persona_id: str,
    audience_id: str,
    situation: str,
    *,
    include_private: bool,
    limit: int = 30,
) -> list[str]:
    """IDs of visible claims matching any term of the situation, best first."""
    terms = fts_terms(situation)
    if not terms:
        return []
    query = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
    rows = conn.execute(
        "SELECT c.id FROM claims_fts f JOIN claims c ON c.seq = f.rowid"
        " WHERE claims_fts MATCH :q AND c.persona_id = :persona AND c.status = 'approved'"
        + _AUDIENCE
        + _VISIBILITY
        + " ORDER BY bm25(claims_fts) LIMIT :limit",
        {
            "q": query,
            "persona": persona_id,
            "audience": audience_id,
            "private": int(include_private),
            "limit": limit,
        },
    ).fetchall()
    return [r["id"] for r in rows]


def in_categories(claim: Claim, paths: list[str]) -> bool:
    return any(claim.category == p or claim.category.startswith(p + "/") for p in paths)


def budgeted(claims: list[Claim], budget_tokens: int) -> list[Claim]:
    """Boundaries first, then highest weight, until the budget is used."""
    ordered = sorted(claims, key=lambda c: (c.kind != "boundary", -c.weight))
    out: list[Claim] = []
    used = 0
    for claim in ordered:
        if used + claim.tokens > budget_tokens:
            continue
        out.append(claim)
        used += claim.tokens
    return out
