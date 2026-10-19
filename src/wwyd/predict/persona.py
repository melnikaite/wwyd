# SPDX-License-Identifier: AGPL-3.0-or-later
"""``get_persona``: compact persona text for a system prompt (SPEC §6.5)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from wwyd.errors import Invalid
from wwyd.predict.context import Claim, in_categories, persona_claims
from wwyd.store.audiences import get_audience

MIN_TOKENS, MAX_TOKENS = 50, 20_000


def _sections(claims: list[Claim]) -> list[tuple[str, list[Claim]]]:
    """Boundaries, style, then the other kinds grouped by category (best group first)."""
    by_weight = sorted(claims, key=lambda c: -c.weight)
    sections: list[tuple[str, list[Claim]]] = []
    for kind, title in (("boundary", "Boundaries"), ("style", "Style")):
        items = [c for c in by_weight if c.kind == kind]
        if items:
            sections.append((title, items))
    groups: dict[str, list[Claim]] = {}
    for claim in by_weight:
        if claim.kind not in ("boundary", "style"):
            groups.setdefault(claim.category, []).append(claim)
    sections.extend(groups.items())  # dict keeps first-seen order = best claim first
    return sections


def get_persona(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    audience: str | None = None,
    categories: list[str] | None = None,
    max_tokens: int = 1500,
    format: str = "text",
) -> dict[str, Any]:
    if not MIN_TOKENS <= max_tokens <= MAX_TOKENS:
        raise Invalid(f"max_tokens must be {MIN_TOKENS}-{MAX_TOKENS}", field="max_tokens")
    if format not in ("text", "json"):
        raise Invalid("format must be text or json", field="format")
    audience_id = get_audience(conn, persona_id, audience)["id"] if audience else None
    claims = persona_claims(conn, persona_id, audience_id)
    if categories:
        paths = [p.strip().strip("/").lower() for p in categories]
        claims = [c for c in claims if in_categories(c, paths)]

    budget = max_tokens * 4  # chars
    used = 0
    truncated = False
    kept: list[tuple[str, list[Claim]]] = []
    for title, items in _sections(claims):
        header = f"{title}:\n"
        taken: list[Claim] = []
        for claim in items:
            cost = len(f"- {claim.text}\n") + (0 if taken else len(header) + 1)
            if used + cost > budget:
                truncated = True
                break
            taken.append(claim)
            used += cost
        if taken:
            kept.append((title, taken))
        if truncated:
            break

    count = sum(len(items) for _, items in kept)
    if format == "json":
        text = json.dumps(
            [
                {
                    "section": title,
                    "claims": [{"id": c.id, "kind": c.kind, "text": c.text} for c in items],
                }
                for title, items in kept
            ],
            ensure_ascii=False,
        )
    else:
        text = "\n\n".join(
            f"{title}:\n" + "\n".join(f"- {c.text}" for c in items) for title, items in kept
        )
    return {"text": text, "claims_used": count, "truncated": truncated}
