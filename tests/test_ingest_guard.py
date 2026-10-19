# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verbatim guard parts 2 and 3 (SPEC §7.4)."""

from __future__ import annotations

from wwyd.ingest.guard import BatchGuard


def guard(other=(), owner=(), vocabulary=()):
    other = list(other)
    return BatchGuard(other_texts=other, all_texts=other + list(owner), vocabulary=set(vocabulary))


def test_part1_still_applies():
    assert guard().check("Mails a.b@example.org") == "identifier"


def test_verbatim_five_words_from_non_owner_text():
    g = guard(other=["Could you please send me the draft by Friday?"])
    assert g.check("Asks people to please send me the draft early.") == "verbatim"
    assert g.check("Sends drafts early when asked.") is None


def test_owner_text_is_not_checked_for_five_grams():
    g = guard(owner=["I prefer green tea in the morning"])
    assert g.check("Prefers green tea in the morning.") is None


def test_identifier_and_name_tokens():
    g = guard(other=["see zq7x-canary-41b9 and ask our friend Velmorine about McKay"])
    assert g.check("Mentions zq7x-canary-41b9 often.") == "name"
    assert g.check("Often talks to velmorine.") == "name"  # case-insensitive
    assert g.check("Trusts McKay.") == "name"


def test_owner_text_names_are_caught_too():
    g = guard(owner=["Lunch with Velmorine tomorrow"])
    assert g.check("Has lunch with Velmorine.") == "name"


def test_sentence_initial_capitals_numbers_and_vocabulary_pass():
    g = guard(
        other=["Thanks. Meeting on Friday at 10 in English, ok? Work-slack is busy, said Anna"],
        vocabulary={"work-slack", "Anna"},
    )
    assert g.check("Meets on Friday at 10, writes in English.") is None
    assert g.check("Thanks people in work-slack.") is None
    assert g.check("Is close to Anna.") is None  # the display name is vocabulary


def test_only_said_is_owner_text():
    """situation, decision and where are non-owner text (5-gram check); said is owner."""
    from wwyd.ingest.episodes import Batch, Episode
    from wwyd.ingest.passes import guard_for

    def ep(**kw):
        return Episode(index=0, category_id="c", category="x", audience=None, **kw)

    batch = Batch(
        [
            ep(
                situation="could you please send me the draft today",
                said="I prefer green tea in the morning",
            ),
            ep(decision="declined because the meeting runs too late"),
            ep(where="the big shared office downstairs here", decision="ok"),
        ]
    )
    g = guard_for(batch, {"Anna"})
    assert g.check("Asks to please send me the draft early.") == "verbatim"
    assert g.check("Declined because the meeting runs too late.") == "verbatim"
    assert g.check("Works in the big shared office downstairs.") == "verbatim"
    assert g.check("Prefers green tea in the morning.") is None


def test_episode_category_names_are_not_vocabulary(conn, pid):
    """Seeded and owner-created categories whitelist their names; episode-created ones do not."""
    from wwyd.db import transaction
    from wwyd.store.categories import CategoryResolver, guard_vocabulary, upsert_category

    upsert_category(conn, pid, "hobbies/chess")
    with transaction(conn):
        CategoryResolver(conn, pid).resolve("friends/velmorine")
    vocab = guard_vocabulary(conn, pid)
    assert {"chess", "food", "communication"} <= vocab
    assert "velmorine" not in vocab and "friends" not in vocab
