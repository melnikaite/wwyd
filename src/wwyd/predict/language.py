# SPDX-License-Identifier: AGPL-3.0-or-later
"""Language handling for predict without per-language code (SPEC §8.1 steps 6-7).

The analyze call names the situation's language itself (``g_language``, an ISO 639 code);
the server only normalises that code and compares Unicode scripts (Latin, Cyrillic, CJK,
Arabic, ...), read from :mod:`unicodedata`. There are no word lists, so any language
works the same way.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

_CODE = re.compile(r"[a-z]{2,3}")
# Japanese mixes kanji and kana, so these count as one script.
_CJK = {"CJK", "HIRAGANA", "KATAKANA"}
MIN_LETTERS = 4
DOMINANT = 0.8


def normalise(code: object) -> str | None:
    """``"de-DE"`` -> ``"de"``; anything that does not look like an ISO 639 code -> ``None``."""
    if not isinstance(code, str):
        return None
    base = code.strip().lower().replace("_", "-").split("-")[0]
    return base if _CODE.fullmatch(base) else None


def _char_script(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    try:
        word = unicodedata.name(ch).split()[0]
    except ValueError:
        return None
    return "CJK" if word in _CJK else word


def script(text: str) -> str | None:
    """The dominant Unicode script of ``text`` (``"LATIN"``, ``"CYRILLIC"``, ``"CJK"``, ...),
    or ``None`` when there are too few letters or no script dominates."""
    counts = Counter(s for ch in text if (s := _char_script(ch)))
    total = sum(counts.values())
    if total < MIN_LETTERS:
        return None
    name, top = counts.most_common(1)[0]
    return name if top >= DOMINANT * total else None


def answer_line(code: str) -> str:
    return f"Answer language: {code}"
