# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path

import pytest

from wwyd.errors import Conflict, Invalid
from wwyd.store import portable
from wwyd.store.audiences import upsert_audience
from wwyd.store.categories import upsert_category
from wwyd.store.claims import add_claims, get_claim, review_claims, update_claim
from wwyd.store.personas import create_user, delete_persona, update_settings

REPO = Path(__file__).resolve().parents[1]


def strip(doc):
    doc = copy.deepcopy(doc)
    doc.pop("exported_at")
    return doc


def populate(conn, pid):
    update_settings(conn, pid, display_name="Alex", timezone="Europe/Berlin")
    upsert_audience(conn, pid, "family", description="Parents and siblings")
    upsert_audience(conn, pid, "work-slack", description="Colleagues", is_default=True)
    upsert_category(conn, pid, "hobbies/chess", description="Chess")
    ids = add_claims(
        conn,
        pid,
        [
            {
                "kind": "style",
                "category": "communication/style",
                "text": "Short, no greeting.",
                "audiences": ["work-slack"],
                "status": "approved",
                "evidence": {
                    "sources": {"message": 40},
                    "share": 0.9,
                    "period": "2026-04..2026-10",
                    "stats": {"len_hist": [1, 2, 3]},
                },
            },
            {
                "kind": "boundary",
                "category": "communication/boundaries",
                "text": "Never discusses salary in writing.",
                "visibility": "owner_only",
                "status": "approved",
                "confidence": 0.95,
            },
            {
                "kind": "habit",
                "category": "hobbies/chess",
                "text": "Plays chess on Sundays.",
                "audiences": ["family", "work-slack"],
            },
            {"kind": "trait", "category": "food", "text": "Hates cilantro."},
            {"kind": "trait", "category": "travel", "text": "Old habit."},
        ],
        allow_approved=True,
    )["created"]
    review_claims(conn, pid, reject=[ids[3]])
    update_claim(conn, pid, ids[4], status="archived")
    return ids


def test_packaged_schema_matches_published_copy():
    packaged = resources.files("wwyd.schema").joinpath("persona.v1.json").read_text()
    assert packaged == (REPO / "schema" / "persona.v1.json").read_text()


def test_export_validates_against_schema(conn, pid):
    populate(conn, pid)
    doc = portable.export_persona(conn, pid)
    portable.validate_document(doc)
    assert doc["format"] == "wwyd.persona" and doc["version"] == 1
    assert doc["persona"] == {"display_name": "Alex", "timezone": "Europe/Berlin"}
    statuses = sorted(c["status"] for c in doc["claims"])
    assert statuses == ["approved", "approved", "archived", "pending", "rejected"]


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_round_trip_through_delete_persona(conn, user, mode):
    pid = user.persona_id
    populate(conn, pid)
    before = portable.export_persona(conn, pid)
    delete_persona(conn, pid, user.login)
    counts = portable.import_persona(conn, pid, before, mode=mode)
    assert counts["claims_created"] == 5
    after = portable.export_persona(conn, pid)
    assert strip(after) == strip(before)
    # FTS works on imported claims
    from wwyd.store.claims import search_claims

    assert len(search_claims(conn, pid, "salary")["claims"]) == 1


def test_merge_updates_existing_claims(conn, pid):
    ids = populate(conn, pid)
    doc = portable.export_persona(conn, pid)
    doc["claims"][0]["text"] = "Short, never a greeting."
    doc["audiences"].append({"name": "friends", "description": "", "is_default": False})
    counts = portable.import_persona(conn, pid, doc)
    assert counts["claims_updated"] == 5 and counts["claims_created"] == 0
    assert counts["audiences_created"] == 1
    assert get_claim(conn, pid, ids[0])["text"] == "Short, never a greeting."


def test_import_into_other_persona_gets_new_ids(conn, pid):
    ids = populate(conn, pid)
    doc = portable.export_persona(conn, pid)
    other = create_user(conn, "other@example.com")
    counts = portable.import_persona(conn, other.persona_id, doc)
    assert counts["claims_created"] == 5
    other_ids = {c["id"] for c in portable.export_persona(conn, other.persona_id)["claims"]}
    assert not other_ids & set(ids)
    assert get_claim(conn, pid, ids[0])["text"] == "Short, no greeting."


def test_legacy_visibility_is_normalised(conn, pid):
    doc = {
        "format": "wwyd.persona",
        "version": 1,
        "persona": {},
        "audiences": [],
        "categories": [],
        "claims": [
            {"kind": "trait", "category": "food", "text": "Likes tea", "visibility": "anyone"},
            {"kind": "trait", "category": "food", "text": "Likes figs", "visibility": "audiences"},
        ],
    }
    portable.import_persona(conn, pid, doc)
    exported = portable.export_persona(conn, pid)["claims"]
    assert {c["visibility"] for c in exported} == {"shared"}
    assert {c["origin"] for c in exported} == {"imported"}


def base_doc():
    return {
        "format": "wwyd.persona",
        "version": 1,
        "persona": {},
        "audiences": [],
        "categories": [],
        "claims": [],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="other"),
        lambda d: d.update(version=2),
        lambda d: d.update(extra=True),
        lambda d: d["claims"].append({"kind": "mood", "category": "food", "text": "x"}),
        lambda d: d["claims"].append(
            {"kind": "trait", "category": "food", "text": "x", "contact_email": "a@b.c"}
        ),
        lambda d: d["claims"].append({"kind": "trait", "category": "Food", "text": "x"}),
        lambda d: d["claims"].append({"kind": "trait", "category": "food", "text": "x" * 501}),
        lambda d: d["claims"].append(
            {"kind": "trait", "category": "food", "text": "x", "evidence": {"names": ["Bob"]}}
        ),
        lambda d: d["claims"].append(
            {"kind": "trait", "category": "food", "text": "x", "visibility": "public"}
        ),
        lambda d: d["audiences"].append({"name": "Bad Name"}),
        lambda d: d.pop("claims"),
    ],
)
def test_schema_failures(conn, pid, mutate):
    doc = base_doc()
    mutate(doc)
    with pytest.raises(Invalid) as exc:
        portable.import_persona(conn, pid, doc)
    assert exc.value.details["errors"]


def test_reference_failures_roll_back(conn, pid):
    doc = base_doc()
    doc["claims"] = [
        {"kind": "trait", "category": "food", "text": "fine"},
        {"kind": "trait", "category": "garden", "text": "unknown category"},
    ]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    assert portable.export_persona(conn, pid)["claims"] == []
    doc["claims"] = [{"kind": "trait", "category": "food", "text": "x", "audiences": ["ghost"]}]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    doc["claims"] = [{"kind": "trait", "category": "food", "text": "mail x@example.com"}]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    doc["claims"] = []
    doc["audiences"] = [{"name": "a", "is_default": True}, {"name": "b", "is_default": True}]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    doc = base_doc()
    doc["claims"] = [
        {
            "kind": "habit",
            "category": "food",
            "text": "one",
            "key": "habit:x",
            "status": "approved",
        },
        {
            "kind": "habit",
            "category": "food",
            "text": "two",
            "key": "habit:x",
            "status": "approved",
        },
    ]
    with pytest.raises(Conflict):
        portable.import_persona(conn, pid, doc)


def test_replace_without_default_creates_everyone(conn, pid):
    doc = base_doc()
    doc["audiences"] = [{"name": "family"}]
    doc["categories"] = [{"path": "food/fruit"}]
    portable.import_persona(conn, pid, doc, mode="replace")
    exported = portable.export_persona(conn, pid)
    assert [(a["name"], a["is_default"]) for a in exported["audiences"]] == [
        ("everyone", True),
        ("family", False),
    ]
    assert [c["path"] for c in exported["categories"]] == ["food", "food/fruit"]
