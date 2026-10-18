# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export and import of a whole persona (``schema/persona.v1.json``)."""

from __future__ import annotations

import copy
import json
import sqlite3
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from wwyd.db import transaction
from wwyd.errors import Invalid, TooLarge
from wwyd.ids import new_id, now_iso
from wwyd.store import categories as cats
from wwyd.store.audiences import audience_ids_by_name
from wwyd.store.claims import _SELECT, _to_dicts, check_evidence, check_text, insert_claim
from wwyd.store.personas import (
    DEFAULT_AUDIENCE,
    check_timezone,
    get_settings,
    wipe_persona_content,
)

FORMAT = "wwyd.persona"
VERSION = 1
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
LEGACY_VISIBILITY = {"anyone": "shared", "audiences": "shared"}


def schema_text() -> str:
    return resources.files("wwyd.schema").joinpath("persona.v1.json").read_text(encoding="utf-8")


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(schema_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def export_persona(conn: sqlite3.Connection, persona_id: str) -> dict[str, Any]:
    settings = get_settings(conn, persona_id)
    audiences = [
        {"name": r["name"], "description": r["description"], "is_default": bool(r["is_default"])}
        for r in conn.execute(
            "SELECT * FROM audiences WHERE persona_id = ? ORDER BY name", (persona_id,)
        )
    ]
    categories = [
        {"path": r["path"], "description": r["description"]}
        for r in conn.execute(
            "SELECT path, description FROM categories WHERE persona_id = ? ORDER BY path",
            (persona_id,),
        )
    ]
    rows = conn.execute(
        f"{_SELECT} WHERE c.persona_id = ? ORDER BY c.seq", (persona_id,)
    ).fetchall()
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now_iso(),
        "persona": {"display_name": settings["display_name"], "timezone": settings["timezone"]},
        "audiences": audiences,
        "categories": categories,
        "claims": _to_dicts(conn, rows),
    }


def normalize_document(document: Any) -> Any:
    """Map legacy visibility values to ``shared`` (SPEC Q1) on a copy of the document."""
    if not isinstance(document, dict) or not isinstance(document.get("claims"), list):
        return document
    document = copy.deepcopy(document)
    for claim in document["claims"]:
        if isinstance(claim, dict) and claim.get("visibility") in LEGACY_VISIBILITY:
            claim["visibility"] = LEGACY_VISIBILITY[claim["visibility"]]
    return document


def validate_document(document: Any) -> dict[str, Any]:
    """Validate against persona.v1.json. Error messages carry paths, never values."""
    if len(json.dumps(document, ensure_ascii=False)) > MAX_DOCUMENT_BYTES:
        raise TooLarge("document larger than 10 MB", limit=MAX_DOCUMENT_BYTES)
    document = normalize_document(document)
    errors = sorted(_validator().iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        problems = [f"{e.json_path} ({e.validator})" for e in errors[:10]]
        raise Invalid(
            "document does not match persona.v1.json", errors=problems, error_count=len(errors)
        )
    return document


def import_persona(
    conn: sqlite3.Connection, persona_id: str, document: Any, *, mode: str = "merge"
) -> dict[str, Any]:
    if mode not in ("merge", "replace"):
        raise Invalid("mode must be merge or replace", field="mode")
    document = validate_document(document)
    defaults = [a["name"] for a in document["audiences"] if a.get("is_default")]
    if len(defaults) > 1:
        raise Invalid("document marks more than one audience as default", field="audiences")
    counts = dict.fromkeys(
        (
            "audiences_created",
            "audiences_updated",
            "categories_created",
            "categories_updated",
            "claims_created",
            "claims_updated",
        ),
        0,
    )
    with transaction(conn):
        if mode == "replace":
            wipe_persona_content(conn, persona_id)
        _import_persona_fields(conn, persona_id, document["persona"])
        _import_audiences(conn, persona_id, document["audiences"], defaults, counts)
        _import_categories(conn, persona_id, document["categories"], counts)
        _import_claims(conn, persona_id, document["claims"], counts)
    return {"mode": mode, **counts}


def _import_persona_fields(conn: sqlite3.Connection, persona_id: str, persona: dict) -> None:
    if "timezone" in persona:
        check_timezone(persona["timezone"])
        conn.execute(
            "UPDATE personas SET timezone = ? WHERE id = ?", (persona["timezone"], persona_id)
        )
    if "display_name" in persona:
        conn.execute(
            "UPDATE personas SET display_name = ? WHERE id = ?",
            (persona["display_name"].strip(), persona_id),
        )


def _import_audiences(
    conn: sqlite3.Connection,
    persona_id: str,
    audiences: list[dict],
    defaults: list[str],
    counts: dict[str, int],
) -> None:
    ts = now_iso()
    existing = audience_ids_by_name(conn, persona_id)
    for aud in audiences:
        if aud["name"] in existing:
            if "description" in aud:
                conn.execute(
                    "UPDATE audiences SET description = ?, updated_at = ? WHERE id = ?",
                    (aud["description"], ts, existing[aud["name"]]),
                )
            counts["audiences_updated"] += 1
        else:
            aud_id = new_id("aud")
            conn.execute(
                "INSERT INTO audiences(id, persona_id, name, description, is_default, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (aud_id, persona_id, aud["name"], aud.get("description", ""), ts, ts),
            )
            existing[aud["name"]] = aud_id
            counts["audiences_created"] += 1
    target = defaults[0] if defaults else None
    has_default = conn.execute(
        "SELECT 1 FROM audiences WHERE persona_id = ? AND is_default = 1", (persona_id,)
    ).fetchone()
    if target is None and not has_default:
        target = DEFAULT_AUDIENCE[0]
        if target not in existing:
            existing[target] = new_id("aud")
            conn.execute(
                "INSERT INTO audiences(id, persona_id, name, description, is_default, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (existing[target], persona_id, *DEFAULT_AUDIENCE, ts, ts),
            )
            counts["audiences_created"] += 1
    if target is not None:
        conn.execute(
            "UPDATE audiences SET is_default = 0 WHERE persona_id = ? AND is_default = 1",
            (persona_id,),
        )
        conn.execute("UPDATE audiences SET is_default = 1 WHERE id = ?", (existing[target],))


def _import_categories(
    conn: sqlite3.Connection, persona_id: str, categories: list[dict], counts: dict[str, int]
) -> None:
    existing = cats.category_ids_by_path(conn, persona_id)
    for cat in sorted(categories, key=lambda c: c["path"].count("/")):
        if cat["path"] in existing:
            if "description" in cat:
                conn.execute(
                    "UPDATE categories SET description = ?, updated_at = ? WHERE id = ?",
                    (cat["description"], now_iso(), existing[cat["path"]]),
                )
            counts["categories_updated"] += 1
        else:
            before = len(cats.category_ids_by_path(conn, persona_id))
            cats.create_category(
                conn, persona_id, cat["path"], cat.get("description", ""), create_parents=True
            )
            existing = cats.category_ids_by_path(conn, persona_id)
            counts["categories_created"] += len(existing) - before


def _import_claims(
    conn: sqlite3.Connection, persona_id: str, claims: list[dict], counts: dict[str, int]
) -> None:
    audiences = audience_ids_by_name(conn, persona_id)
    categories = cats.category_ids_by_path(conn, persona_id)
    for index, claim in enumerate(claims):
        where = f"claims[{index}]"
        if claim["category"] not in categories:
            raise Invalid(f"{where}: category is not defined", field=f"{where}.category")
        aud = claim.get("audiences", "all")
        audience_ids = []
        if aud != "all":
            missing = [n for n in aud if n not in audiences]
            if missing:
                raise Invalid(f"{where}: audience is not defined", field=f"{where}.audiences")
            audience_ids = [audiences[n] for n in aud]
        try:
            text = check_text(claim["text"])
            evidence = check_evidence(claim.get("evidence"))
        except Invalid as exc:
            raise Invalid(
                f"{where}: {exc.message}", field=f"{where}.{exc.details.get('field')}"
            ) from None
        claim_id = claim.get("id")
        owner = None
        if claim_id:
            owner = conn.execute(
                "SELECT persona_id FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
        if owner is not None and owner["persona_id"] != persona_id:
            claim_id = None  # belongs to another persona on this server: import as a new claim
        updated = owner is not None and claim_id is not None
        if updated:
            conn.execute("DELETE FROM claims WHERE id = ?", (claim_id,))
        status = claim.get("status", "pending")
        insert_claim(
            conn,
            persona_id,
            claim_id=claim_id,
            kind=claim["kind"],
            category_id=categories[claim["category"]],
            text=text,
            audiences_all=aud == "all",
            audience_ids=audience_ids,
            visibility=claim.get("visibility", "shared"),
            status=status,
            confidence=claim.get("confidence", 0.5),
            evidence={} if status == "rejected" else evidence,
            origin=claim.get("origin", "imported"),
            key=claim.get("key"),
            proposed_category=claim.get("proposed_category"),
            superseded_by=claim.get("superseded_by"),
            archive_reason=claim.get("archive_reason"),
            created_at=claim.get("created_at"),
            updated_at=claim.get("updated_at"),
            reviewed_at=claim.get("reviewed_at"),
            archived_at=claim.get("archived_at") or (now_iso() if status == "archived" else None),
            last_evidence_at=claim.get("last_evidence_at"),
        )
        counts["claims_updated" if updated else "claims_created"] += 1
    conn.execute(
        "UPDATE claims SET superseded_by = NULL WHERE persona_id = ? AND superseded_by IS NOT NULL"
        " AND superseded_by NOT IN (SELECT id FROM claims WHERE persona_id = ?)",
        (persona_id, persona_id),
    )
