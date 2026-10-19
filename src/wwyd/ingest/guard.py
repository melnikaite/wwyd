# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verbatim guard (SPEC §7.4).

Part 1 (:func:`find_identifier`): claim texts that contain an email address, a phone
number, a URL or an ``@handle`` are rejected. It also guards ``add_claims``.

Parts 2 and 3 (:class:`BatchGuard`) check derived texts against the ``ingest_raw`` batch:

2. any 5-word sequence that appears verbatim in a non-owner text of the batch;
3. a token that looks like an identifier or a name (contains a letter and a digit, mixed
   case such as ``McKay``, or capitalised in a non-initial position) in **any** batch text,
   unless it is in the persona's vocabulary or a small built-in list.

Part 3 deliberately also scans the owner's own texts: owners write contacts' names too
("ask Velmorine"), and P2 forbids storing them. Pure numbers ("10", "2026") are not
identifier-like; long digit runs are caught by part 1's phone pattern.
"""

from __future__ import annotations

import re

# Every pattern may only start where its leading run starts (the lookbehinds), so a long run
# such as "a.a.a.a..." is scanned once rather than once per position: linear time.
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
URL = re.compile(
    r"(?:(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://\S+)"  # scheme://...
    r"|(?:\bwww\.\S+)"
    r"|(?:(?<![a-z0-9-])(?<![a-z0-9-]\.)[a-z0-9-]+(?:\.[a-z0-9-]+)*"
    r"\.(?:com|net|org|io|ai|dev|app|me|co|de|ru|uk|info|biz)(?:/\S*)?\b)",
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


# --- parts 2 and 3: checks against the batch -------------------------------------------

NGRAM = 5
_WORD = re.compile(r"\w+", re.UNICODE)
_TOKEN = re.compile(r"\w(?:[\w'’-]*\w)?", re.UNICODE)
_SENTENCE_END = frozenset('.!?…:;"«»()[]-–—*•>')

_BUILTIN_WORDS = (
    # weekdays and months
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday "
    "January February March April May June July August September October November December "
    "понедельник вторник среда четверг пятница суббота воскресенье "
    "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь "
    # languages
    "English German French Spanish Italian Portuguese Dutch Polish Russian Ukrainian "
    "Belarusian Chinese Japanese Korean Arabic Hebrew Turkish Hindi "
    "английский немецкий французский испанский итальянский русский украинский белорусский "
    # pronouns and common all-caps words
    "I OK AI"
)
BUILTIN_VOCABULARY = frozenset(w.casefold() for w in _BUILTIN_WORDS.split())


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _looks_like_identifier(token: str, initial: bool) -> bool:
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    if has_digit:
        return has_alpha  # "zq7x", "A320"; plain numbers are not identifiers
    letters = [c for c in token if c.isalpha()]
    if any(c.isupper() for c in letters[1:]) and any(c.islower() for c in letters):
        return True  # mixed case: McKay, iPhone
    return letters[:1] != [] and letters[0].isupper() and not initial


def _is_initial(text: str, start: int) -> bool:
    """True when the token at ``start`` begins the text, a line or a sentence."""
    j = start - 1
    while j >= 0 and text[j].isspace():
        if text[j] == "\n":
            return True
        j -= 1
    return j < 0 or text[j] in _SENTENCE_END


class BatchGuard:
    """Parts 2 and 3 of the verbatim guard for one batch. Holds derived sets only (5-gram
    tuples, suspicious tokens) and must be dropped with the batch."""

    def __init__(
        self,
        *,
        other_texts: list[str],
        all_texts: list[str],
        vocabulary: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        allowed = set(BUILTIN_VOCABULARY)
        for phrase in vocabulary:
            allowed.update(_words(phrase))
            allowed.add(phrase.casefold())
        self._ngrams: set[tuple[str, ...]] = set()
        for text in other_texts:
            words = _words(text)
            for i in range(len(words) - NGRAM + 1):
                self._ngrams.add(tuple(words[i : i + NGRAM]))
        self._suspicious: set[str] = set()
        for text in all_texts:
            for match in _TOKEN.finditer(text):
                token = match.group()
                initial = _is_initial(text, match.start())
                if _looks_like_identifier(token, initial):
                    folded = token.casefold()
                    if folded not in allowed:
                        self._suspicious.add(folded)
                        self._suspicious.update(
                            p for p in _words(token) if len(p) > 2 and p not in allowed
                        )

    def check(self, text: str) -> str | None:
        """Return why ``text`` must be dropped (``identifier``, ``verbatim``, ``name``)."""
        if find_identifier(text) is not None:
            return "identifier"
        words = _words(text)
        for i in range(len(words) - NGRAM + 1):
            if tuple(words[i : i + NGRAM]) in self._ngrams:
                return "verbatim"
        tokens = {t.casefold() for t in _TOKEN.findall(text)} | set(words)
        if tokens & self._suspicious:
            return "name"
        return None

    def clear(self) -> None:
        self._ngrams.clear()
        self._suspicious.clear()
