# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unicode category paths and organic creation at ingest (SPEC §4.4, D21)."""

from __future__ import annotations

import json

import pytest
from conftest import call, call_error

from wwyd.errors import Invalid
from wwyd.ingest import jobs
from wwyd.limits import get_limiter
from wwyd.store.categories import normalize_category_path

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def setup(fake_llm):
    fake_llm.on("statements", {"claims": []})
    fake_llm.on("describe_categories", {"descriptions": []})


# --- normalisation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("work/meetings", "work/meetings"),  # the old ASCII rule is a subset
        ("Кино/Сериалы", "кино/сериалы"),
        ("  Hobbies / Board Games ", "hobbies/board-games"),
        ("ｆｏｏｄ/Ｔｅａ", "food/tea"),  # NFKC: full-width -> ASCII
        ("Straße", "strasse"),  # casefold
        ("café_au-lait", "café_au-lait"),
        ("a" * 40, "a" * 40),
        ("x/y/z", "x/y/z"),
    ],
)
def test_normalize_ok(raw, expected):
    assert normalize_category_path(raw) == expected
    assert normalize_category_path(expected) == expected  # idempotent


@pytest.mark.parametrize(
    "raw",
    ["a/b/c/d", "", "a//b", "/a", "-a", "_a", "a.b", "a@b", "a" * 41, "food/tea!", "a/ /b", 7],
)
def test_normalize_rejects(raw):
    with pytest.raises(Invalid) as info:
        normalize_category_path(raw)
    if isinstance(raw, str) and len(raw) > 3:
        assert raw not in json.dumps(info.value.to_payload(), ensure_ascii=False)


async def test_category_tools_normalise(client):
    out = await call(client, "upsert_category", {"path": "Hobbies/Настольные Игры"})
    assert out["category"]["path"] == "hobbies/настольные-игры" and out["created"]
    again = await call(client, "upsert_category", {"path": "HOBBIES/настольные игры"})
    assert again["created"] is False
    assert (await call_error(client, "upsert_category", {"path": "a/b/c/d"}))["code"] == "invalid"
    added = await call(
        client,
        "add_claims",
        {"claims": [{"kind": "trait", "category": "Hobbies/Настольные Игры", "text": "Plays."}]},
    )
    assert len(added["created"]) == 1
    listed = await call(client, "list_claims", {"status": "pending", "category": "HOBBIES"})
    assert listed["claims"][0]["category"] == "hobbies/настольные-игры"


# --- organic creation ---------------------------------------------------------------------


async def ingest(client, episodes, **kw):
    out = await call(client, "ingest_raw", {"episodes": episodes, **kw})
    await jobs.wait(out["job_id"])
    return out, await call(client, "job_status", {"job_id": out["job_id"]})


def said(category):
    return {"category": category, "said": "I like it."}


def paths(conn, pid):
    return {
        r["path"]: r["origin"]
        for r in conn.execute("SELECT path, origin FROM categories WHERE persona_id = ?", (pid,))
    }


def changes(conn, pid, op="category_create"):
    return conn.execute(
        "SELECT * FROM changes WHERE persona_id = ? AND op = ? ORDER BY rowid", (pid, op)
    ).fetchall()


async def test_unknown_paths_are_created_with_parents(client, conn, pid):
    _, status = await ingest(
        client, [said("Кино/Фантастика"), said("кино/фантастика"), said("hobbies/films")]
    )
    assert status["status"] == "done"
    assert status["counts"]["categories_created"] == 3
    assert status["counts"]["unmapped_categories"] == 0
    got = paths(conn, pid)
    assert got["кино"] == got["кино/фантастика"] == got["hobbies/films"] == "episode"
    assert got["hobbies"] == "seed"
    (change,) = changes(conn, pid)
    payload = json.loads(change["payload"])
    assert sorted(c["path"] for c in payload["categories"]) == [
        "hobbies/films",
        "кино",
        "кино/фантастика",
    ]
    assert change["job_id"] == status_job(conn)


def status_job(conn):
    return conn.execute("SELECT id FROM jobs ORDER BY created_at DESC, rowid DESC").fetchone()[0]


async def test_existing_paths_create_nothing(client, conn, pid):
    _, status = await ingest(client, [said("food"), said("work/meetings")])
    assert status["counts"]["categories_created"] == 0 and changes(conn, pid) == []


async def test_new_categories_per_job_limit(client, conn, pid, user):
    get_limiter(conn).set_own_limits(user.user_id, {"new_categories_per_job": 2})
    eps = [said("hobbies/films"), said("hobbies/films/sci-fi"), said("hobbies/chess"), said("x/y")]
    _, status = await ingest(client, eps)
    counts = status["counts"]
    assert counts["categories_created"] == 2
    # chess -> its existing ancestor hobbies; x/y has no existing ancestor -> other.
    assert counts["unmapped_categories"] == 2
    got = paths(conn, pid)
    assert "hobbies/chess" not in got and "x" not in got
    assert got["other"] == "seed"


async def test_max_categories_limit_counts_seeds(client, conn, pid, user):
    n = len(paths(conn, pid))
    get_limiter(conn).set_own_limits(user.user_id, {"max_categories": n + 1})
    _, status = await ingest(client, [said("travel/trains"), said("travel/planes")] * 2)
    assert status["counts"]["categories_created"] == 1
    assert status["counts"]["unmapped_categories"] == 2  # each planes episode
    assert "travel/trains" in paths(conn, pid) and "travel/planes" not in paths(conn, pid)


async def test_alias_routing(client, conn, pid, monkeypatch):
    ids = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM categories")}
    conn.execute(
        "INSERT INTO category_aliases(persona_id, path, category_id) VALUES (?, ?, ?), (?, ?, ?)",
        (pid, "movies", ids["hobbies"], pid, "movies/scifi", ids["tech"]),
    )
    seen = []

    def fake_run(conn, *, persona_id, episodes, tz, counts, job_id=None):
        seen.extend(ep.category for ep in episodes)
        return set()

    monkeypatch.setattr(jobs, "run_analyzers", fake_run)
    eps = [said("movies"), said("movies/drama"), said("movies/scifi"), said("movies/scifi/old")]
    _, status = await ingest(client, eps, llm_for_ingest=False)
    assert seen == ["hobbies", "hobbies/drama", "tech", "tech/old"]
    assert status["counts"]["categories_created"] == 2
    assert "movies" not in paths(conn, pid)


async def test_phone_like_path_is_never_created(client, conn, pid):
    _, status = await ingest(client, [said("contacts/555-123-4567")], llm_for_ingest=False)
    assert status["counts"]["unmapped_categories"] == 1
    assert not any("555" in p for p in paths(conn, pid))


# --- the description call ---------------------------------------------------------------


async def test_description_call_is_guarded_and_never_overwrites(client, conn, pid, fake_llm):
    def describe(call):
        assert call["user"] == "- hobbies/films\n- кино"
        return {
            "descriptions": [
                {"path": "hobbies/films", "description": "Which films the owner watches."},
                {"path": "Кино", "description": "Фильмы, которые смотрит с Velmorine."},
                {"path": "food", "description": "Not a new category."},
            ]
        }

    fake_llm.on("describe_categories", describe)
    eps = [said("hobbies/films"), said("кино"), {"category": "food", "said": "ask Velmorine"}]
    out, status = await ingest(client, eps)
    assert out["estimated_tokens"] > 0
    assert len(fake_llm.by_purpose("describe_categories")) == 1
    counts = status["counts"]
    assert counts["dropped_by_guard"] == 1 and counts["dropped_invalid"] == 1
    desc = {
        r["path"]: r["description"]
        for r in conn.execute(
            "SELECT path, description FROM categories WHERE persona_id = ?", (pid,)
        )
    }
    assert desc["hobbies/films"] == "Which films the owner watches."
    assert desc["кино"] == ""
    assert desc["food"].startswith("What and how")


async def test_no_description_call_without_llm(client, conn, pid, fake_llm):
    _, status = await ingest(client, [said("hobbies/films")], llm_for_ingest=False)
    assert status["counts"]["categories_created"] == 1
    assert fake_llm.calls == []


async def test_description_failure_keeps_categories(client, conn, pid, fake_llm):
    from wwyd.errors import LLMUnavailable

    def down(call):
        raise LLMUnavailable("down")

    fake_llm.on("describe_categories", down)
    _, status = await ingest(client, [said("hobbies/films")])
    assert status["status"] == "partial" and status["remaining_records"] == [0]
    assert "hobbies/films" in paths(conn, pid)
    fake_llm.on("describe_categories", {"descriptions": "nope"})
    _, status = await ingest(client, [said("hobbies/chess")])
    assert status["status"] == "done" and status["counts"]["llm_calls_failed"] == 1


# --- undo -----------------------------------------------------------------------------------


async def test_undo_category_create(client, conn, pid):
    await ingest(client, [said("hobbies/films/sci-fi")], llm_for_ingest=False)
    (change,) = changes(conn, pid)
    undone = await call(client, "undo_change", {"id": change["id"]})
    assert undone["change"]["state"] == "undone"
    assert not any(p.startswith("hobbies/films") for p in paths(conn, pid))

    await ingest(client, [said("travel/trains")], llm_for_ingest=False)
    change = changes(conn, pid)[-1]
    await call(
        client,
        "add_claims",
        {"claims": [{"kind": "preference", "category": "travel/trains", "text": "Trains."}]},
    )
    err = await call_error(client, "undo_change", {"id": change["id"]})
    assert err["code"] == "conflict"
    assert "travel/trains" in paths(conn, pid)
