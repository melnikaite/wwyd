# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from wwyd.errors import Invalid
from wwyd.store import personas
from wwyd.store.claims import add_claims, list_claims


def test_defaults(conn, user):
    s = personas.get_settings(conn, user.persona_id)
    assert s["display_name"] == "owner" and s["timezone"] == "UTC"
    assert s["escalate_below"] == 0.6 and s["llm_for_ingest"] is True
    assert "{owner}" in s["disclosure_template"] and s["raw_text_note"]


def test_update(conn, user):
    s = personas.update_settings(
        conn,
        user.persona_id,
        display_name="Alex",
        timezone="Europe/Berlin",
        escalate_below=0.75,
        disclosure_template="Auto-reply for {owner}.",
        llm_for_ingest=False,
    )
    assert (s["display_name"], s["timezone"], s["escalate_below"], s["llm_for_ingest"]) == (
        "Alex",
        "Europe/Berlin",
        0.75,
        False,
    )
    assert personas.get_settings(conn, user.persona_id) == s


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timezone": "Mars/Olympus"},
        {"timezone": "../etc/passwd"},
        {"escalate_below": 1.5},
        {"disclosure_template": "No placeholder"},
        {"display_name": "  "},
    ],
)
def test_update_rejects(conn, user, kwargs):
    with pytest.raises(Invalid):
        personas.update_settings(conn, user.persona_id, **kwargs)


def test_delete_persona_requires_login_and_reseeds(conn, user):
    pid = user.persona_id
    personas.update_settings(conn, pid, timezone="Europe/Berlin")
    add_claims(conn, pid, [{"kind": "trait", "category": "food", "text": "Likes tea"}])
    with pytest.raises(Invalid):
        personas.delete_persona(conn, pid, "someone@example.com")
    assert personas.delete_persona(conn, pid, "OWNER@example.com") == {"deleted": True}
    assert list_claims(conn, pid, status="any", include_archived=True)["claims"] == []
    assert conn.execute("SELECT COUNT(*) FROM claims_fts").fetchone()[0] == 0
    assert personas.get_settings(conn, pid)["timezone"] == "UTC"
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE persona_id = ?", (pid,)).fetchone()[
        0
    ] == len(personas.SEED_CATEGORIES)
    assert (
        conn.execute("SELECT name FROM audiences WHERE persona_id = ?", (pid,)).fetchall()[0][0]
        == "everyone"
    )
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
