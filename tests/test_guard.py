# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from wwyd.ingest.guard import find_identifier


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Writes to anna.k@example.org first", "email"),
        ("Shares links like https://example.com/x", "url"),
        ("Mentions www.example.net often", "url"),
        ("Points people to example.io", "url"),
        ("Calls +49 151 2345 6789 in the evening", "phone"),
        ("Texts 0151-23456789", "phone"),
        ("Pings @jdoe_42 for reviews", "handle"),
    ],
)
def test_guard_finds_identifiers(text, kind):
    assert find_identifier(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "Short, no greeting, answers within minutes during work hours.",
        "Keeps at least 10 min between calls (87% of 212 gaps).",
        "Active 08:30-19:00 on weekdays, rarely on Sunday.",
        "Meetings in 2026 are typically 25 or 50 min.",
        "Uses e.g. bullet points; i.e. prefers lists.",
    ],
)
def test_guard_passes_ordinary_claims(text):
    assert find_identifier(text) is None
