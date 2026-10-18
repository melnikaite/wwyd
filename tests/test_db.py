# SPDX-License-Identifier: AGPL-3.0-or-later
from wwyd.db import migrate, open_db
from wwyd.ids import new_id
from wwyd.store.personas import SEED_CATEGORIES, create_user, get_or_create_user


def test_migrations_apply_once(tmp_path):
    conn = open_db(tmp_path / "a.db")
    assert migrate(conn) == []
    rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [tuple(r) for r in rows] == [(1, "0001_init.sql")]
    conn.close()
    conn = open_db(tmp_path / "a.db")  # reopen: nothing new to apply
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


def test_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_only_m1_tables(conn):
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not r[0].startswith(("sqlite_", "claims_fts_"))
    }
    assert names == {
        "schema_migrations",
        "users",
        "personas",
        "audiences",
        "categories",
        "claims",
        "claim_audiences",
        "claims_fts",
        "changes",
    }


def test_ids_are_prefixed_and_random():
    ids = {new_id("clm") for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("clm_") and len(i) == 20 for i in ids)


def test_first_user_is_admin_and_persona_is_seeded(conn):
    first = create_user(conn, "a@example.com")
    second, created = get_or_create_user(conn, "b@example.com")
    again, created_again = get_or_create_user(conn, "A@example.com")
    assert first.is_admin and not second.is_admin and created
    assert not created_again and again.user_id == first.user_id
    assert (
        conn.execute("SELECT password_hash FROM users WHERE id = ?", (first.user_id,)).fetchone()[0]
        is None
    )
    paths = [
        r[0]
        for r in conn.execute(
            "SELECT path FROM categories WHERE persona_id = ? ORDER BY path", (first.persona_id,)
        )
    ]
    assert paths == sorted(p for p, _ in SEED_CATEGORIES)
    assert all(d for _, d in SEED_CATEGORIES)
    aud = conn.execute(
        "SELECT name, description, is_default FROM audiences WHERE persona_id = ?",
        (first.persona_id,),
    ).fetchall()
    assert [tuple(a) for a in aud] == [("everyone", "Anyone not in another group", 1)]
