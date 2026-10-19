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

from wwyd.analyzers.contract import validate_output
from wwyd.db import transaction
from wwyd.errors import Invalid, TooLarge
from wwyd.ids import new_id, now_iso
from wwyd.ingest.guard import find_identifier
from wwyd.store import categories as cats
from wwyd.store.audiences import audience_ids_by_name, check_not_alias
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
        {"path": r["path"], "description": r["description"], "origin": r["origin"]}
        for r in conn.execute(
            "SELECT path, description, origin FROM categories WHERE persona_id = ? ORDER BY path",
            (persona_id,),
        )
    ]
    aliases = [
        {"path": r["path"], "category": r["target"]}
        for r in conn.execute(
            "SELECT a.path, c.path AS target FROM category_aliases a"
            " JOIN categories c ON c.id = a.category_id WHERE a.persona_id = ? ORDER BY a.path",
            (persona_id,),
        )
    ]
    labels = [
        {"category": r["path"], "field": r["field"], "label": r["label"]}
        for r in conn.execute(
            "SELECT c.path, l.field, l.label FROM category_labels l"
            " JOIN categories c ON c.id = l.category_id WHERE l.persona_id = ?"
            " ORDER BY c.path, l.field, l.label",
            (persona_id,),
        )
    ]
    rows = conn.execute(
        f"{_SELECT} WHERE c.persona_id = ? ORDER BY c.seq", (persona_id,)
    ).fetchall()
    analyzers, analyzer_ids = _export_analyzers(conn, persona_id)
    refs = _export_refs(conn, persona_id, analyzer_ids)
    claims = _to_dicts(conn, rows)
    for claim in claims:
        if claim.get("key"):
            claim["key"] = _portable_key(claim["key"], refs)
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now_iso(),
        "persona": {"display_name": settings["display_name"], "timezone": settings["timezone"]},
        "audiences": audiences,
        "categories": categories,
        "category_aliases": aliases,
        "category_labels": labels,
        "analyzers": analyzers,
        "claims": claims,
    }


# Keyed claims (``builtin:<name>:<scope id>[:<facet>...]``, ``anl:<analyzer id>``) embed
# row IDs that do not survive an import. Exported keys replace every ``:``-separated
# segment that is a category, audience or analyzer ID of the persona with a reference:
# ``{category:<path>}``, ``{audience:<name>}`` or ``{analyzer:<index in "analyzers">}``.
# Paths and names never contain ``:``, ``{`` or ``}``, so the encoding is unambiguous.
# Import maps references back to the persona's (new) IDs; a key with a reference that
# cannot be resolved is dropped (the claim is imported unkeyed, counted in keys_dropped).


def _export_refs(
    conn: sqlite3.Connection, persona_id: str, analyzer_ids: list[str]
) -> dict[str, str]:
    refs = {
        r["id"]: "{category:" + r["path"] + "}"
        for r in conn.execute("SELECT id, path FROM categories WHERE persona_id = ?", (persona_id,))
    }
    for r in conn.execute("SELECT id, name FROM audiences WHERE persona_id = ?", (persona_id,)):
        refs[r["id"]] = "{audience:" + r["name"] + "}"
    for index, analyzer_id in enumerate(analyzer_ids):
        refs[analyzer_id] = "{analyzer:" + str(index) + "}"
    return refs


def _portable_key(key: str, refs: dict[str, str]) -> str:
    return ":".join(refs.get(seg, seg) for seg in key.split(":"))


def _local_key(key: str, refs: dict[str, dict[str, str | None]]) -> str | None:
    """Map a portable key back; keys without references (older exports) pass unchanged."""
    out = []
    for seg in key.split(":"):
        if seg.startswith("{") and seg.endswith("}") and ":" not in seg:
            return None  # malformed reference
        out.append(seg)
    # Segments were split on ":", so a reference spans two pieces: "{kind" and "value}".
    merged: list[str] = []
    i = 0
    while i < len(out):
        seg = out[i]
        if seg.startswith("{") and i + 1 < len(out) and out[i + 1].endswith("}"):
            kind, value = seg[1:], out[i + 1][:-1]
            target = refs.get(kind, {}).get(value)
            if target is None:
                return None
            merged.append(target)
            i += 2
            continue
        merged.append(seg)
        i += 1
    return ":".join(merged)


def _state(conn: sqlite3.Connection, analyzer_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in conn.execute(
        "SELECT month, metric, key, value FROM analyzer_state WHERE analyzer_id = ?"
        " ORDER BY metric, month, key",
        (analyzer_id,),
    ):
        out.setdefault(r["metric"], {}).setdefault(r["month"], {})[r["key"]] = r["value"]
    return out


def _export_analyzers(
    conn: sqlite3.Connection, persona_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Scopes by category path or audience name; analyzers whose scope is gone are left
    out (nothing could run them). Also returns the exported analyzers' IDs, in order."""
    out = []
    ids = []
    for r in conn.execute(
        "SELECT a.*, CASE a.scope_kind"
        " WHEN 'category' THEN (SELECT path FROM categories WHERE id = a.scope_id)"
        " ELSE (SELECT name FROM audiences WHERE id = a.scope_id) END AS scope_name"
        " FROM analyzers a WHERE a.persona_id = ? ORDER BY a.seq",
        (persona_id,),
    ).fetchall():
        if r["scope_name"] is None:
            continue
        item: dict[str, Any] = {
            "origin": r["origin"],
            "name": r["name"],
            "scope": {r["scope_kind"]: r["scope_name"]},
            "status": r["status"],
        }
        if r["origin"] == "llm":
            item.update(
                statement_template=r["statement_template"],
                kind=r["kind"],
                source=r["source"],
                claim=r["claim_id"],
            )
        item["state"] = _state(conn, r["id"])
        out.append(item)
        ids.append(r["id"])
    return out, ids


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
            "aliases_imported",
            "labels_imported",
            "analyzers_created",
            "analyzers_updated",
            "analyzers_skipped",
            "keys_dropped",
        ),
        0,
    )
    from wwyd.store.audiences import audience_names, revoke_vanished

    with transaction(conn):
        before = audience_names(conn, persona_id)
        if mode == "replace":
            wipe_persona_content(conn, persona_id)
        _import_persona_fields(conn, persona_id, document["persona"])
        _import_audiences(conn, persona_id, document["audiences"], defaults, counts)
        _import_categories(conn, persona_id, document["categories"], counts)
        analyzer_ids, links = _import_analyzers(
            conn, persona_id, document.get("analyzers", []), counts
        )
        refs: dict[str, dict[str, str | None]] = {
            "category": dict(cats.category_ids_by_path(conn, persona_id)),
            "audience": dict(audience_ids_by_name(conn, persona_id, alias=False)),
            "analyzer": {str(i): a for i, a in enumerate(analyzer_ids)},
        }
        claim_ids = _import_claims(conn, persona_id, document["claims"], counts, refs)
        for analyzer_id, claim_id in links.items():
            conn.execute(
                "UPDATE analyzers SET claim_id = ? WHERE id = ?",
                (claim_ids.get(claim_id), analyzer_id),
            )
        _import_aliases(conn, persona_id, document.get("category_aliases", []), counts)
        _import_labels(conn, persona_id, document.get("category_labels", []), counts)
        revoke_vanished(conn, persona_id, before)
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
    existing = audience_ids_by_name(conn, persona_id, alias=False)
    for aud in audiences:
        check_not_alias(aud["name"], "audiences")
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
    for index, cat in enumerate(categories):
        _check_path(cat["path"], f"categories[{index}].path")
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
                conn,
                persona_id,
                cat["path"],
                cat.get("description", ""),
                create_parents=True,
                origin=cat.get("origin", "owner"),
            )
            existing = cats.category_ids_by_path(conn, persona_id)
            counts["categories_created"] += len(existing) - before


def _import_claims(
    conn: sqlite3.Connection,
    persona_id: str,
    claims: list[dict],
    counts: dict[str, int],
    refs: dict[str, dict[str, str | None]] | None = None,
) -> dict[str, str]:
    """Returns document claim ID -> stored claim ID."""
    id_map: dict[str, str] = {}
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
        key = claim.get("key")
        if key:
            key = _local_key(key, refs or {})
            if key is None:
                counts["keys_dropped"] += 1
        status = claim.get("status", "pending")
        stored = insert_claim(
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
            key=key,
            proposed_category=claim.get("proposed_category"),
            superseded_by=claim.get("superseded_by"),
            archive_reason=claim.get("archive_reason"),
            created_at=claim.get("created_at"),
            updated_at=claim.get("updated_at"),
            reviewed_at=claim.get("reviewed_at"),
            archived_at=claim.get("archived_at") or (now_iso() if status == "archived" else None),
            last_evidence_at=claim.get("last_evidence_at"),
        )
        if claim.get("id"):
            id_map[claim["id"]] = stored
        counts["claims_updated" if updated else "claims_created"] += 1
    conn.execute(
        "UPDATE claims SET superseded_by = NULL WHERE persona_id = ? AND superseded_by IS NOT NULL"
        " AND superseded_by NOT IN (SELECT id FROM claims WHERE persona_id = ?)",
        (persona_id, persona_id),
    )
    return id_map


def _check_path(path: str, where: str) -> None:
    """Paths are stored normalised; a document path must already be in that form."""
    try:
        ok = cats.normalize_category_path(path) == path
    except Invalid:
        ok = False
    if not ok:
        raise Invalid(f"{where}: not a normalised category path", field=where)


def _import_aliases(
    conn: sqlite3.Connection, persona_id: str, aliases: list[dict], counts: dict[str, int]
) -> None:
    """An alias at a path that is a real category, or to an unknown category, is skipped."""
    ids = cats.category_ids_by_path(conn, persona_id)
    for index, alias in enumerate(aliases):
        _check_path(alias["path"], f"category_aliases[{index}].path")
        if alias["path"] in ids or alias["category"] not in ids:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO category_aliases(persona_id, path, category_id)"
            " VALUES (?, ?, ?)",
            (persona_id, alias["path"], ids[alias["category"]]),
        )
        counts["aliases_imported"] += 1


def _import_labels(
    conn: sqlite3.Connection, persona_id: str, labels: list[dict], counts: dict[str, int]
) -> None:
    ids = cats.category_ids_by_path(conn, persona_id)
    for index, item in enumerate(labels):
        where = f"category_labels[{index}]"
        if find_identifier(item["label"]) is not None:
            raise Invalid(f"{where}: label looks like a contact identifier", field=where)
        if item["category"] not in ids:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO category_labels(persona_id, category_id, field, label)"
            " VALUES (?, ?, ?, ?)",
            (persona_id, ids[item["category"]], item["field"], item["label"]),
        )
        counts["labels_imported"] += cur.rowcount


def _checked_state(state: Any, where: str) -> dict[str, dict[str, dict[str, float]]]:
    """The output contract (SPEC §7.2) plus the guard's regex part on every key."""
    try:
        out = validate_output(state)
    except Invalid as exc:
        raise Invalid(f"{where}: {exc.message}", field=where) from None
    for months in out.values():
        for keys in months.values():
            if any(find_identifier(k) is not None for k in keys):
                raise Invalid(f"{where}: a key looks like a contact identifier", field=where)
    return out


def _import_analyzers(
    conn: sqlite3.Connection, persona_id: str, analyzers: list[dict], counts: dict[str, int]
) -> tuple[list[str | None], dict[str, str]]:
    """Scopes map by category path / audience name (unknown -> skipped and counted), as do
    builtins this server does not ship. An analyzer that already exists for the same
    (origin, name, scope) is updated and its state *replaced* by the document's (the
    document is a snapshot of the same counters, so adding would double-count). New
    non-retired LLM analyzers beyond the owner's ``max_analyzers`` are skipped and counted.

    Returns the stored analyzer ID per document index (None if skipped) and
    ``{analyzer_id: document claim ID}`` links, set once the claims are imported."""
    from wwyd.analyzers.builtin import BUILTINS

    ids: list[str | None] = []
    links: dict[str, str] = {}
    limit = _limit(conn, persona_id, "max_analyzers")
    llm_count = conn.execute(
        "SELECT count(*) FROM analyzers WHERE persona_id = ? AND origin = 'llm'"
        " AND status <> 'retired'",
        (persona_id,),
    ).fetchone()[0]

    categories = cats.category_ids_by_path(conn, persona_id)
    audiences = audience_ids_by_name(conn, persona_id, alias=False)
    for index, item in enumerate(analyzers):
        where = f"analyzers[{index}]"
        state = _checked_state(item.get("state", {}), f"{where}.state")
        ((scope_kind, scope_name),) = item["scope"].items()
        scope_id = (categories if scope_kind == "category" else audiences).get(scope_name)
        builtin = BUILTINS.get(item["name"]) if item["origin"] == "builtin" else None
        if scope_id is None or (
            item["origin"] == "builtin" and (builtin is None or builtin.scope_kind != scope_kind)
        ):
            counts["analyzers_skipped"] += 1
            ids.append(None)
            continue
        if item["origin"] == "builtin":
            kind, template, source = builtin.kind, builtin.statement_template, ""
        else:
            kind, template, source = item["kind"], item["statement_template"], item["source"]
            if find_identifier(template) is not None:
                raise Invalid(
                    f"{where}: statement_template looks like a contact identifier", field=where
                )
        claim_id = None  # linked after the claims are imported
        row = conn.execute(
            "SELECT id, status FROM analyzers WHERE persona_id = ? AND origin = ? AND name = ?"
            " AND scope_kind = ? AND scope_id = ?",
            (persona_id, item["origin"], item["name"], scope_kind, scope_id),
        ).fetchone()
        if item["origin"] == "llm":
            was = row is not None and row["status"] != "retired"
            now = item["status"] != "retired"
            if now and not was:
                if limit is not None and llm_count >= limit:
                    counts["analyzers_skipped"] += 1
                    ids.append(None)
                    continue
                llm_count += 1
            elif was and not now:
                llm_count -= 1
        if row is None:
            analyzer_id = new_id("anl")
            conn.execute(
                "INSERT INTO analyzers(id, persona_id, scope_kind, scope_id, origin, name,"
                " statement_template, kind, source, status, created_at, claim_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analyzer_id, persona_id, scope_kind, scope_id, item["origin"], item["name"],
                    template, kind, source, item["status"], now_iso(), claim_id,
                ),
            )  # fmt: skip
            counts["analyzers_created"] += 1
        else:
            analyzer_id = row["id"]
            conn.execute(
                "UPDATE analyzers SET statement_template = ?, kind = ?, source = ?, status = ?,"
                " claim_id = ? WHERE id = ?",
                (template, kind, source, item["status"], claim_id, analyzer_id),
            )
            conn.execute("DELETE FROM analyzer_state WHERE analyzer_id = ?", (analyzer_id,))
            counts["analyzers_updated"] += 1
        conn.executemany(
            "INSERT INTO analyzer_state(analyzer_id, month, metric, key, value)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (analyzer_id, month, metric, key, value)
                for metric, months in state.items()
                for month, keys in months.items()
                for key, value in keys.items()
            ],
        )
        ids.append(analyzer_id)
        if item.get("claim"):
            links[analyzer_id] = item["claim"]
    return ids, links


def _limit(conn: sqlite3.Connection, persona_id: str, name: str) -> int | None:
    from wwyd.limits import get_limiter

    row = conn.execute("SELECT user_id FROM personas WHERE id = ?", (persona_id,)).fetchone()
    value = get_limiter(conn).effective_limits(row["user_id"]).get(name) if row else None
    return int(value) if value is not None else None
