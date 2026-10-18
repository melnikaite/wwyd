# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verbatim guard (SPEC §7.4).

M1 implements part 1 only: claim texts that contain an email address, a phone number, a
URL or an ``@handle`` are rejected. Parts 2 and 3 (verbatim 5-grams and identifier-like
tokens from non-owner batch text) arrive with ``ingest_raw`` in M4.
"""

from __future__ import annotations

import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
URL = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]*://\S+)"  # scheme://...
    r"|(?:\bwww\.\S+)"
    r"|(?:\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|ai|dev|app|me|co|de|ru|uk|info|biz)"
    r"(?:/\S*)?\b)",
    re.IGNORECASE,
)
# 7+ digits, optionally with a leading + and common separators; avoids years and times.
PHONE = re.compile(r"(?<![\w])\+?\d(?:[\s().-]*\d){6,}(?![\w])")
HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_][A-Za-z0-9_.]{1,}")

_CHECKS = (("email", EMAIL), ("url", URL), ("phone", PHONE), ("handle", HANDLE))


def find_identifier(text: str) -> str | None:
    """Return the kind of contact identifier found in ``text`` (never the match), or None."""
    for kind, pattern in _CHECKS:
        if pattern.search(text):
            return kind
    return None


def passes_regex_guard(text: str) -> bool:
    return find_identifier(text) is None
