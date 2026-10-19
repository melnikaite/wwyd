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
