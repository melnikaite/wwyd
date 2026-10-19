"""Text and counting helpers for analyzers (SPEC §7.2).

Pure standard library. The same source runs natively in the host (builtin analyzers import
``wwyd.sandbox.helpers.wwyd_helpers``) and in the sandbox, where the runner injects it as
the module ``wwyd_helpers`` at run time, so changing it never requires rebuilding the
sandbox assets. Keep it dependency-free and limited to what the guest stdlib ships
(``re``, ``unicodedata``, ``collections``).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable

__all__ = [
    "Token",
    "tokenize",
    "words",
    "sentences",
    "is_emoji",
    "emojis",
    "ending",
    "ENDINGS",
    "count_words",
    "count_bigrams",
    "month_key",
    "bucket",
    "histogram",
    "add_counts",
]

_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
_SENT_END = re.compile(r"(?<=[.!?…])\s+|(?<=\))\s+(?=\S)|\n+")
_MONTH = re.compile(r"^(\d{4})-(\d{2})")

ENDINGS = ("period", "exclamation", "question", "ellipsis", "smiley", "emoji", "other", "none")


class Token(str):
    """A lowercased word that remembers how it was written (``.original``, ``.case``)."""

    original: str
    case: str  # "lower", "upper", "title" or "mixed"

    def __new__(cls, original: str) -> Token:
        t = super().__new__(cls, original.lower())
        t.original = original
        if original.islower():
            t.case = "lower"
        elif original.isupper():
            t.case = "upper" if len(original) > 1 else "title"
        elif original[:1].isupper() and original[1:].islower():
            t.case = "title"
        else:
            t.case = "mixed"
        return t


def tokenize(text: str) -> list[Token]:
    """Unicode words (letters, inner apostrophes and hyphens; no digits)."""
    return [Token(m.group()) for m in _WORD.finditer(text or "")]


def words(text: str) -> list[str]:
    """Lowercased words of ``text``."""
    return [m.group().lower() for m in _WORD.finditer(text or "")]


def sentences(text: str) -> list[str]:
    """Split on sentence-ending punctuation, ``)``-smileys and newlines."""
    return [s.strip() for s in _SENT_END.split(text or "") if s and s.strip()]


def is_emoji(ch: str) -> bool:
    if len(ch) != 1:
        return False
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or unicodedata.category(ch) == "So"
    )


def emojis(text: str) -> list[str]:
    """Emoji characters in ``text`` (modifiers and joiners are skipped)."""
    return [ch for ch in text or "" if is_emoji(ch) and not 0x1F3FB <= ord(ch) <= 0x1F3FF]


def ending(text: str) -> str:
    """Class of the final punctuation of a message: one of :data:`ENDINGS`."""
    s = (text or "").rstrip()
    if not s:
        return "none"
    last = s[-1]
    if last == ")" or s.endswith((":)", ";)", ":D", "xD")):
        return "smiley" if last != ")" or s.count("(") < s.count(")") else "other"
    if last == "…" or s.endswith("..."):
        return "ellipsis"
    if last == ".":
        return "period"
    if last == "!":
        return "exclamation"
    if last == "?":
        return "question"
    if is_emoji(last) or 0x1F3FB <= ord(last) <= 0x1F3FF or last in "‍️":
        return "emoji"
    if last.isalnum():
        return "none"
    return "other"


def count_words(texts: Iterable[str]) -> Counter[str]:
    c: Counter[str] = Counter()
    for t in texts:
        c.update(words(t))
    return c


def count_bigrams(texts: Iterable[str]) -> Counter[str]:
    """Adjacent word pairs, joined with a space."""
    c: Counter[str] = Counter()
    for t in texts:
        w = words(t)
        c.update(f"{a} {b}" for a, b in zip(w, w[1:], strict=False))
    return c


def month_key(iso: str) -> str:
    """``"YYYY-MM"`` from an ISO date or datetime string."""
    m = _MONTH.match(iso or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError("not an ISO date")
    return f"{m.group(1)}-{m.group(2)}"


def bucket(value: float, edges: Iterable[float]) -> str:
    """Label of the histogram bin for ``value``: ``"a-b"`` for ``a <= value < b``.

    ``edges`` must be ascending; below the first edge gives ``"<a"``, from the last ``"a+"``.
    """
    es = list(edges)
    if not es or value < es[0]:
        return f"<{_fmt(es[0])}" if es else "_"
    for lo, hi in zip(es, es[1:], strict=False):
        if lo <= value < hi:
            return f"{_fmt(lo)}-{_fmt(hi)}"
    return f"{_fmt(es[-1])}+"


def histogram(values: Iterable[float], edges: Iterable[float]) -> dict[str, int]:
    es = list(edges)
    return dict(Counter(bucket(v, es) for v in values))


def add_counts(acc: dict, month: str, metric: str, key: str, n: float = 1) -> dict:
    """Add ``n`` to ``acc[metric][month][key]`` (the analyzer output shape)."""
    d = acc.setdefault(metric, {}).setdefault(month, {})
    d[key] = d.get(key, 0) + n
    return acc


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)
