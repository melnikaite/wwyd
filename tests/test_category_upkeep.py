# SPDX-License-Identifier: AGPL-3.0-or-later
"""Category upkeep (SPEC §4.4, §6.3, §10.6): aliases, merges moving analyzers, nightly
merge and split, undo, wipe and export/import of the new sections."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from wwyd.compact import compact
from wwyd.compact import scheduler as sched
from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, LLMUnavailable
from wwyd.ids import new_id
from wwyd.store import analyzers as an
from wwyd.store import categories as ca
from wwyd.store import changes as chg
from wwyd.store import portable
from wwyd.store.claims import add_claims, get_claim, insert_claim
from wwyd.store.personas import delete_persona

pytestmark = pytest.mark.anyio

WORDS = [
    "apples",
    "bread",
    "cheese",
    "dates",
    "eggs",
    "figs",
    "grapes",
    "honey",
    "ink",
    "jam",
    "kale",
    "lemons",
    "mango",
    "nuts",
    "olives",
    "pears",
    "quinoa",
    "rice",
    "salt",
    "tea",
    "udon",
    "vanilla",
    "walnuts",
    "yams",
    "zucchini",
    "basil",
    "cocoa",
    "dill",
]


def cid(conn, pid, path):
    return ca.get_category(conn, pid, path)["id"]


def builtin(conn, pid, path, name="time_gaps"):
    with transaction(conn):
        return an.ensure_builtin(
            conn,
            pid,
            name=name,
            scope_kind="category",
            scope_id=cid(conn, pid, path),
            kind="habit",
            statement_template="t",
        )["id"]


def llm_analyzer(conn, pid, path, name="film_genres"):
    aid = new_id("anl")
    with transaction(conn):
        conn.execute(
            "INSERT INTO analyzers(id, persona_id, scope_kind, scope_id, origin, name,"
            " statement_template, kind, source, status, created_at)"
            " VALUES (?, ?, 'category', ?, 'llm', ?, 'Likes {x}', 'preference', 'def f(): pass',"
            " 'active', '2026-10-01T00:00:00Z')",
            (aid, pid, cid(conn, pid, path), name),
        )
    return aid


def put_state(conn, aid, rows):
    with transaction(conn):
        conn.executemany(
            "INSERT INTO analyzer_state(analyzer_id, month, metric, key, value) VALUES (?,?,?,?,?)",
            [(aid, *r) for r in rows],
        )


def state(conn, aid):
    return {
        (r["month"], r["metric"], r["key"]): r["value"]
        for r in conn.execute("SELECT * FROM analyzer_state WHERE analyzer_id = ?", (aid,))
    }


def keyed(conn, pid, path, name="time_gaps", status="pending", text="Gap of 10 minutes"):
    cat = cid(conn, pid, path)
    with transaction(conn):
        return insert_claim(
            conn,
            pid,
            kind="habit",
            category_id=cat,
            text=text,
            audiences_all=True,
            audience_ids=[],
            visibility="shared",
            status=status,
            confidence=0.6,
            evidence={},
            origin="derived",
            key=f"builtin:{name}:{cat}",
        )


def episode_cat(conn, pid, path):
    with transaction(conn):
        r = ca.CategoryResolver(conn, pid)
        r.resolve(path)
        r.record(None)
    return cid(conn, pid, path)


def aliases(conn, pid):
    return {
        r["path"]: r["category_id"]
        for r in conn.execute("SELECT * FROM category_aliases WHERE persona_id = ?", (pid,))
    }


def resolve(conn, pid, path):
    with transaction(conn):
        return ca.CategoryResolver(conn, pid).resolve(path)


# --- aliases on owner rename and merge ------------------------------------------------------


def test_rename_leaves_aliases_and_routes(conn, pid):
    ca.upsert_category(conn, pid, "work/meetings/one-on-one")
    meetings = cid(conn, pid, "work/meetings")
    ca.upsert_category(conn, pid, "work/meetings", rename_to="schedule/meetings")
    assert aliases(conn, pid) == {
        "work/meetings": meetings,
        "work/meetings/one-on-one": cid(conn, pid, "schedule/meetings/one-on-one"),
    }
    assert resolve(conn, pid, "work/meetings") == (meetings, "schedule/meetings")
    # The rest of a path below an alias is appended (and created under the new path).
    _, path = resolve(conn, pid, "work/meetings/standups")
    assert path == "schedule/meetings/standups"
    tree = {n["path"]: n for n in ca.list_categories(conn, pid)}
    sched_node = {c["path"]: c for c in tree["schedule"]["children"]}["schedule/meetings"]
    assert sched_node["aliases"] == ["work/meetings"]
    # A real category at an alias path drops the alias.
    ca.upsert_category(conn, pid, "work/meetings")
    assert "work/meetings" not in aliases(conn, pid)


def test_rename_undo_restores_paths_and_aliases(conn, pid):
    ca.upsert_category(conn, pid, "hobbies/films")
    ca.upsert_category(conn, pid, "hobbies/films", rename_to="hobbies/movies")
    ca.upsert_category(conn, pid, "hobbies/movies", rename_to="hobbies/cinema")
    assert set(aliases(conn, pid)) == {"hobbies/films", "hobbies/movies"}
    last = chg.list_changes(conn, pid, op="category_rename")["changes"][0]["id"]
    chg.undo_change(conn, pid, last)
    assert cid(conn, pid, "hobbies/movies")
    assert set(aliases(conn, pid)) == {"hobbies/films"}
    first = chg.list_changes(conn, pid, op="category_rename", state="applied")["changes"][0]
    chg.undo_change(conn, pid, first["id"])
    assert cid(conn, pid, "hobbies/films") and aliases(conn, pid) == {}


def test_merge_moves_everything_and_undo_restores(conn, pid):
    ca.upsert_category(conn, pid, "hobbies/movies")
    films = episode_cat(conn, pid, "hobbies/films")
    ca.upsert_category(conn, pid, "hobbies/films/noir")
    movies = cid(conn, pid, "hobbies/movies")
    noir = cid(conn, pid, "hobbies/films/noir")
    plain = add_claims(
        conn, pid, [{"kind": "preference", "category": "hobbies/films/noir", "text": "Likes noir"}]
    )["created"]
    # Builtins: films and noir both have time_gaps; movies too. Only films has time_duration.
    g_movies, g_films, g_noir = (
        builtin(conn, pid, p) for p in ("hobbies/movies", "hobbies/films", "hobbies/films/noir")
    )
    d_films = builtin(conn, pid, "hobbies/films", "time_duration")
    put_state(conn, g_movies, [("2026-10", "gap_min", "10_15", 3)])
    put_state(conn, g_films, [("2026-10", "gap_min", "10_15", 4), ("2026-09", "gap_min", "5", 1)])
    put_state(conn, g_noir, [("2026-10", "gap_min", "10_15", 2)])
    put_state(conn, d_films, [("2026-10", "dur", "30", 7)])
    llm = llm_analyzer(conn, pid, "hobbies/films")
    k_movies = keyed(conn, pid, "hobbies/movies", text="Gaps in movies")
    k_films = keyed(conn, pid, "hobbies/films", text="Gaps in films")
    k_dur = keyed(conn, pid, "hobbies/films", "time_duration", text="Durations in films")
    with transaction(conn):
        conn.execute(
            "INSERT INTO category_labels VALUES (?, ?, 'decision', 'declined')", (pid, films)
        )
        conn.execute("INSERT INTO category_aliases VALUES (?, 'kino', ?)", (pid, films))

    res = ca.merge_categories(conn, pid, ["hobbies/films"], "hobbies/movies")
    assert set(res["removed_categories"]) == {"hobbies/films", "hobbies/films/noir"}
    assert aliases(conn, pid) == {
        "hobbies/films": movies,
        "hobbies/films/noir": movies,
        "kino": movies,
    }
    assert resolve(conn, pid, "hobbies/films") == (movies, "hobbies/movies")
    assert state(conn, g_movies) == {
        ("2026-10", "gap_min", "10_15"): 9,
        ("2026-09", "gap_min", "5"): 1,
    }
    rows = {
        r["id"]: r for r in conn.execute("SELECT * FROM analyzers WHERE persona_id = ?", (pid,))
    }
    assert g_films not in rows and g_noir not in rows
    assert rows[d_films]["scope_id"] == movies and rows[llm]["scope_id"] == movies
    assert get_claim(conn, pid, k_films)["status"] == "archived"  # target had a live key
    assert get_claim(conn, pid, k_movies)["status"] == "pending"
    assert conn.execute("SELECT key FROM claims WHERE id = ?", (k_dur,)).fetchone()[0] == (
        f"builtin:time_duration:{movies}"
    )
    assert get_claim(conn, pid, plain[0])["category"] == "hobbies/movies"
    labels = conn.execute("SELECT * FROM category_labels WHERE persona_id = ?", (pid,)).fetchall()
    assert [(r["category_id"], r["label"]) for r in labels] == [(movies, "declined")]

    chg.undo_change(conn, pid, res["change_id"])
    assert cid(conn, pid, "hobbies/films") == films
    assert cid(conn, pid, "hobbies/films/noir") == noir
    assert aliases(conn, pid) == {"kino": films}
    assert state(conn, g_movies) == {("2026-10", "gap_min", "10_15"): 3}
    assert state(conn, g_films) == {
        ("2026-10", "gap_min", "10_15"): 4,
        ("2026-09", "gap_min", "5"): 1,
    }
    assert state(conn, g_noir) == {("2026-10", "gap_min", "10_15"): 2}
    rows = {
        r["id"]: r for r in conn.execute("SELECT * FROM analyzers WHERE persona_id = ?", (pid,))
    }
    assert rows[d_films]["scope_id"] == films and rows[llm]["scope_id"] == films
    assert get_claim(conn, pid, k_films)["status"] == "pending"
    assert conn.execute("SELECT key FROM claims WHERE id = ?", (k_dur,)).fetchone()[0] == (
        f"builtin:time_duration:{films}"
    )
    assert get_claim(conn, pid, plain[0])["category"] == "hobbies/films/noir"
    labels = conn.execute("SELECT * FROM category_labels WHERE persona_id = ?", (pid,)).fetchall()
    assert [(r["category_id"], r["label"]) for r in labels] == [(films, "declined")]


def test_merge_undo_conflicts_after_later_claim_change(conn, pid):
    ca.upsert_category(conn, pid, "hobbies/films")
    ids = add_claims(
        conn, pid, [{"kind": "preference", "category": "hobbies/films", "text": "Likes noir"}]
    )["created"]
    res = ca.merge_categories(conn, pid, ["hobbies/films"], "hobbies")
    with transaction(conn):
        chg.execute(conn, pid, "decay", {"kind": "archive", "claim": ids[0], "reason": "decayed"})
    with pytest.raises(Conflict):
        chg.undo_change(conn, pid, res["change_id"])


# --- nightly merge ---------------------------------------------------------------------------


async def test_nightly_merge_cross_language_and_rules(conn, pid, fake_llm):
    ca.upsert_category(conn, pid, "hobbies/movies")  # owner
    kino = episode_cat(conn, pid, "кино")
    films = episode_cat(conn, pid, "hobbies/films")
    episode_cat(conn, pid, "hobbies/films/noir")
    add_claims(conn, pid, [{"kind": "preference", "category": "кино", "text": "Watches a lot"}])
    fake_llm.on(
        "category_merge",
        {
            "pairs": [
                {"a": "кино", "b": "hobbies/movies"},  # episode -> owner (b is the target)
                {"a": "hobbies/movies", "b": "hobbies/films"},  # direction flipped: films goes
                {"a": "hobbies/films/noir", "b": "hobbies/films"},  # descendant: refused
                {"a": "food", "b": "money"},  # two seeds: refused
                {"a": "ghost", "b": "hobbies"},
            ]
        },
    )
    out = await compact(conn, pid, apply=True, steps=["category_merge"])
    call = fake_llm.by_purpose("category_merge")[0]
    shown = json.loads(call["user"])
    assert {c["path"] for c in shown["new"]} == {"кино", "hobbies/films", "hobbies/films/noir"}
    assert "food" in {c["path"] for c in shown["others"]}
    assert [c["op"] for c in out["changes"]] == ["category_merge", "category_merge"]
    paths = set(ca.category_ids_by_path(conn, pid))
    assert "кино" not in paths and "hobbies/films" not in paths and "hobbies/movies" in paths
    assert {"food", "money"} <= paths
    movies = cid(conn, pid, "hobbies/movies")
    assert aliases(conn, pid)["кино"] == movies
    assert aliases(conn, pid)["hobbies/films/noir"] == movies
    # Each merge is its own undoable change.
    chg.undo_change(conn, pid, out["changes"][1]["id"])
    chg.undo_change(conn, pid, out["changes"][0]["id"])
    assert cid(conn, pid, "кино") == kino and cid(conn, pid, "hobbies/films") == films


async def test_nightly_merge_episode_pair_newer_into_older(conn, pid, fake_llm):
    old = episode_cat(conn, pid, "films")
    with transaction(conn):
        conn.execute(
            "UPDATE categories SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?", (old,)
        )
    episode_cat(conn, pid, "movies")
    fake_llm.on("category_merge", {"pairs": [{"a": "films", "b": "movies"}]})
    await compact(conn, pid, apply=True, steps=["category_merge"])
    assert "movies" not in ca.category_ids_by_path(conn, pid)
    assert aliases(conn, pid) == {"movies": old}


async def test_nightly_merge_code_candidates_confirmed(conn, pid, fake_llm):
    ca.upsert_category(conn, pid, "hobbies/films")  # owner
    ca.upsert_category(conn, pid, "leisure")
    episode_cat(conn, pid, "leisure/films")  # same leaf, other parent
    episode_cat(conn, pid, "hobbies/film")  # one edit away
    episode_cat(conn, pid, "кино")  # nothing in common: open search only
    seen = []

    def confirm(call):
        items = json.loads(call["user"])
        seen.extend((i["a"]["path"], i["b"]["path"]) for i in items)
        return {"answers": [{"pair": i["pair"], "same": i["a"]["path"] != "hobbies/film"}
                            for i in items]}  # fmt: skip

    fake_llm.on("category_confirm", confirm)
    fake_llm.on("category_merge", {"pairs": []})
    out = await compact(conn, pid, apply=True, steps=["category_merge"])
    assert ("leisure/films", "hobbies/films") in seen and ("hobbies/film", "hobbies/films") in seen
    assert len(fake_llm.by_purpose("category_confirm")) == 1
    (open_call,) = fake_llm.by_purpose("category_merge")
    assert [c["path"] for c in json.loads(open_call["user"])["new"]] == ["кино"]
    paths = set(ca.category_ids_by_path(conn, pid))
    assert "leisure/films" not in paths and "hobbies/film" in paths  # only confirmed merges
    assert [c["op"] for c in out["changes"]] == ["category_merge"]


async def test_nightly_merge_all_covered_skips_open_search(conn, pid, fake_llm):
    ca.upsert_category(conn, pid, "hobbies/films")
    ca.upsert_category(conn, pid, "leisure")
    episode_cat(conn, pid, "leisure/films")
    fake_llm.on("category_confirm", {"answers": [{"pair": 1, "same": False}]})
    out = await compact(conn, pid, apply=True, steps=["category_merge"])
    assert len(fake_llm.calls) == 1 and out["changes"] == []


def test_merge_candidate_pairs_rules(conn, pid):
    from wwyd.compact.categories import candidate_pairs, merge_candidates

    ca.upsert_category(conn, pid, "art")
    episode_cat(conn, pid, "arts/painting")
    episode_cat(conn, pid, "car")  # short leaves only match when identical
    episode_cat(conn, pid, "arts/painting/oil")  # descendant of a new one: refused
    new, others = merge_candidates(conn, pid)
    got = {(a["path"], b["path"]) for a, b in candidate_pairs(new, others)}
    assert got == set()


async def test_nightly_merge_watermark_and_scheduler(conn, pid, fake_llm):
    episode_cat(conn, pid, "films")
    fake_llm.on("category_merge", {"pairs": []})
    ran = await sched.run_nightly_async(conn, datetime(2026, 10, 18, 4, 0, tzinfo=UTC))
    assert ran == [pid] and len(fake_llm.by_purpose("category_merge")) == 1
    # Nothing new since the previous run (categories created before it): no call.
    with transaction(conn):
        conn.execute(
            "UPDATE categories SET created_at = '2026-10-01T00:00:00Z' WHERE persona_id = ?", (pid,)
        )
    await sched.run_nightly_async(conn, datetime(2026, 10, 19, 4, 0, tzinfo=UTC))
    assert len(fake_llm.by_purpose("category_merge")) == 1


async def test_dry_run_and_llm_errors(conn, pid, fake_llm):
    episode_cat(conn, pid, "films")
    out = await compact(conn, pid, apply=False)
    assert fake_llm.calls == []
    assert out["category_pending"]["category_merge"] == ["films"]

    def boom(call):
        raise LLMUnavailable("down")

    fake_llm.on("category_merge", boom)
    out = await compact(conn, pid, apply=True, steps=["category_merge"])
    assert out["skipped_steps"] == {"category_merge": 1} and out["changes"] == []


# --- nightly split ---------------------------------------------------------------------------


def fill(conn, pid, path, n):
    items = [
        {"kind": "preference", "category": path, "text": f"Buys {w} weekly"} for w in WORDS[:n]
    ]
    return add_claims(conn, pid, items)["created"]


def grouping(call, names=("fruit", "other-food"), drop=0):
    labels = [c["id"] for c in json.loads(call["user"])["claims"]]
    labels = labels[drop:]
    half = len(labels) // 2
    return {
        "groups": [
            {"name": names[0], "description": "Fruit and berries", "claims": labels[:half]},
            {"name": names[1], "description": "Everything else", "claims": labels[half:]},
        ]
    }


async def test_split_moves_claims_and_undo(conn, pid, fake_llm):
    ids = fill(conn, pid, "food", 21)
    k = keyed(conn, pid, "food")
    g = builtin(conn, pid, "food")
    fake_llm.on("category_split", grouping)
    dry = await compact(conn, pid, apply=False, steps=["category_split"])
    assert dry["category_pending"] == {"category_split": ["food"]} and fake_llm.calls == []
    out = await compact(conn, pid, apply=True, steps=["category_split"])
    assert [c["op"] for c in out["changes"]] == ["category_split"]
    shown = json.loads(fake_llm.by_purpose("category_split")[0]["user"])
    assert shown["category"] == "food" and len(shown["claims"]) == 21  # keyed claim stays
    cats = {get_claim(conn, pid, i)["category"] for i in ids}
    assert cats == {"food/fruit", "food/other-food"}
    assert get_claim(conn, pid, k)["category"] == "food"
    row = conn.execute("SELECT scope_id FROM analyzers WHERE id = ?", (g,)).fetchone()
    assert row[0] == cid(conn, pid, "food")
    origin = ca.get_category(conn, pid, "food/fruit")["origin"]
    assert origin == "episode" and ca.get_category(conn, pid, "food/fruit")["description"]
    chg.undo_change(conn, pid, out["changes"][0]["id"])
    assert {get_claim(conn, pid, i)["category"] for i in ids} == {"food"}
    assert "food/fruit" not in ca.category_ids_by_path(conn, pid)


async def test_split_threshold_and_depth(conn, pid, fake_llm):
    fill(conn, pid, "food", 20)  # not more than the threshold
    ca.upsert_category(conn, pid, "work/meetings/standups")
    fill(conn, pid, "work/meetings/standups", 22)  # depth 3: never split
    fake_llm.on("category_split", grouping)
    out = await compact(conn, pid, apply=True, steps=["category_split"])
    assert out["changes"] == [] and fake_llm.calls == []


@pytest.mark.parametrize(
    "make",
    [
        lambda call: grouping(call, names=("fruit", "fruit")),  # duplicate name
        lambda call: grouping(call, names=("a/b", "other")),  # not one segment
        lambda call: grouping(call, names=("call-5551234567", "other")),  # guard
        lambda call: {"groups": grouping(call)["groups"][:1]},  # one group
        lambda call: {"groups": [{"name": "x", "description": "", "claims": ["c999"]}] * 2},
        lambda call: "nonsense",
    ],
)
async def test_split_bad_groupings_discarded(conn, pid, fake_llm, make):
    ids = fill(conn, pid, "food", 21)
    fake_llm.on("category_split", make)
    out = await compact(conn, pid, apply=True, steps=["category_split"])
    assert out["changes"] == [] and out["discarded_splits"] == 1
    assert {get_claim(conn, pid, i)["category"] for i in ids} == {"food"}
    assert not any(p.startswith("food/") for p in ca.category_ids_by_path(conn, pid))


async def test_split_partial_grouping_is_accepted(conn, pid, fake_llm):
    ids = fill(conn, pid, "food", 21)

    def partial(call):
        out = grouping(call, drop=1)  # c1 is missed
        groups = out["groups"]
        groups[1]["claims"].append(groups[0]["claims"][0])  # c2 listed twice
        groups[1]["claims"].append("c999")  # unknown label
        groups.append({"name": "bad/name", "description": "x", "claims": ["c1"]})
        return out

    fake_llm.on("category_split", partial)
    out = await compact(conn, pid, apply=True, steps=["category_split"])
    assert [c["op"] for c in out["changes"]] == ["category_split"]
    labels = {f"c{i + 1}": cid for i, cid in enumerate(reversed(ids))}  # shown newest first
    assert get_claim(conn, pid, labels["c1"])["category"] == "food"  # stays in the parent
    assert get_claim(conn, pid, labels["c2"])["category"] == "food/fruit"  # first wins
    assert {get_claim(conn, pid, i)["category"] for i in ids} == {
        "food",
        "food/fruit",
        "food/other-food",
    }


async def test_split_description_guarded(conn, pid, fake_llm):
    fill(conn, pid, "food", 21)

    def bad(call):
        out = grouping(call)
        out["groups"][0]["description"] = "Ask at shop@example.com"
        return out

    fake_llm.on("category_split", bad)
    out = await compact(conn, pid, apply=True, steps=["category_split"])
    assert out["changes"] == []


# --- wipe and export/import ------------------------------------------------------------------


def test_delete_persona_wipes_analyzers_aliases_labels(conn, pid, user):
    ca.upsert_category(conn, pid, "hobbies/films")
    aid = builtin(conn, pid, "hobbies/films")
    put_state(conn, aid, [("2026-10", "gap_min", "1", 1)])
    llm_analyzer(conn, pid, "hobbies/films")
    with transaction(conn):
        conn.execute(
            "INSERT INTO category_labels VALUES (?, ?, 'tag', 'x')",
            (pid, cid(conn, pid, "hobbies/films")),
        )
    ca.upsert_category(conn, pid, "hobbies/films", rename_to="hobbies/movies")
    delete_persona(conn, pid, user.login)
    for table in ("analyzers", "category_aliases", "category_labels"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE persona_id = ?", (pid,)).fetchone()
        assert n[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM analyzer_state").fetchone()[0] == 0


def _rich(conn, pid):
    ca.upsert_category(conn, pid, "hobbies/films")
    films = cid(conn, pid, "hobbies/films")
    g = builtin(conn, pid, "hobbies/films")
    put_state(conn, g, [("2026-10", "gap_min", "10_15", 3)])
    llm = llm_analyzer(conn, pid, "hobbies/films")
    put_state(conn, llm, [("2026-10", "genre", "noir", 2)])
    with transaction(conn):
        conn.execute("INSERT INTO category_labels VALUES (?, ?, 'tag', 'late')", (pid, films))
    ca.upsert_category(conn, pid, "hobbies/films", rename_to="hobbies/movies")


def test_export_import_round_trip(conn, pid, user):
    _rich(conn, pid)
    doc = portable.export_persona(conn, pid)
    assert doc["category_aliases"] == [{"path": "hobbies/films", "category": "hobbies/movies"}]
    assert doc["category_labels"] == [
        {"category": "hobbies/movies", "field": "tag", "label": "late"}
    ]
    by_name = {a["name"]: a for a in doc["analyzers"]}
    assert by_name["time_gaps"] == {
        "origin": "builtin",
        "name": "time_gaps",
        "scope": {"category": "hobbies/movies"},
        "status": "active",
        "state": {"gap_min": {"2026-10": {"10_15": 3.0}}},
    }
    assert by_name["film_genres"]["source"] == "def f(): pass"
    portable.validate_document(doc)

    res = portable.import_persona(conn, pid, doc, mode="replace")
    assert res["analyzers_created"] == 2 and res["aliases_imported"] == 1
    again = portable.export_persona(conn, pid)
    for key in ("category_aliases", "category_labels", "analyzers"):
        assert again[key] == doc[key], key
    # merge mode replaces the state of the same builtin (not added twice)
    res = portable.import_persona(conn, pid, doc, mode="merge")
    assert res["analyzers_updated"] == 2
    assert portable.export_persona(conn, pid)["analyzers"] == doc["analyzers"]


@pytest.mark.parametrize(
    "state",
    [
        {"gap_min": {"2026-13": {"a": 1}}},
        {"gap_min": {"2026-10": {"a": "x"}}},
        {"gap_min": {"2026-10": {"+49 30 1234567": 1}}},
        {"Bad": {"2026-10": {"a": 1}}},
    ],
)
def test_import_validates_state(conn, pid, state):
    doc = portable.export_persona(conn, pid)
    doc["analyzers"] = [
        {
            "origin": "builtin",
            "name": "time_gaps",
            "scope": {"category": "food"},
            "status": "active",
            "state": state,
        }
    ]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)


def test_import_skips_unknown_scopes_and_builtins(conn, pid):
    doc = portable.export_persona(conn, pid)
    base = {"origin": "builtin", "status": "active", "state": {}}
    doc["analyzers"] = [
        {**base, "name": "time_gaps", "scope": {"category": "nowhere"}},
        {**base, "name": "no_such_builtin", "scope": {"category": "food"}},
        {**base, "name": "time_gaps", "scope": {"category": "food"}},
    ]
    res = portable.import_persona(conn, pid, doc)
    assert res["analyzers_skipped"] == 2 and res["analyzers_created"] == 1
