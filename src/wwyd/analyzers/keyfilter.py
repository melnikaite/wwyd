# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host key filter (SPEC §7.2, D19): analyzer code is never trusted with privacy.

Each metric declares a key class per segment; compound keys join segments with ``.``. A
key whose segment count differs from its declaration is dropped. Classes:

* ``numeric`` — only digits and ``:_-.`` (inside one segment: digits and ``:_-``);
* ``vocab`` — host-known words: audience names and IDs, weekday names, the scope
  category's labels (``category_labels``) and the reserved ``_``;
* ``text`` (the default) — the guard's regex part, not name-like, and spread over at
  least 5 distinct episodes on at least 3 distinct days of the batch.

Dropped keys are only counted; they are never logged or returned.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from wwyd.analyzers.contract import Output
from wwyd.ingest.episodes import Episode
from wwyd.ingest.guard import find_identifier

NUMERIC = "numeric"
VOCAB = "vocab"
TEXT = "text"
KEY_CLASSES = (NUMERIC, VOCAB, TEXT)
RESERVED = "_"

MIN_EPISODES = 5
MIN_DAYS = 3
NAME_LIKE_SHARE = 0.5

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_NUMERIC = re.compile(r"^[0-9:_-]+$")
_TOKEN = re.compile(r"\w(?:[\w'’-]*\w)?", re.UNICODE)
_SENTENCE_END = frozenset('.!?…:;"«»()[]-–—*•>\n')

KeyClasses = Mapping[str, Sequence[str]]  # metric -> class per segment


@dataclass
class _TokenStats:
    episodes: set[int] = field(default_factory=set)
    days: set[str] = field(default_factory=set)
    non_initial: int = 0
    non_initial_caps: int = 0
    mixed_case: bool = False


def _is_initial(text: str, start: int) -> bool:
    i = start - 1
    while i >= 0 and text[i] in " \t\r":
        i -= 1
    return i < 0 or text[i] in _SENTENCE_END


class TextStats:
    """Per-batch token statistics over chosen episode text fields (in memory only)."""

    def __init__(self, episodes: Iterable[Episode], fields: Sequence[str] = ("said",)) -> None:
        self._tokens: dict[str, _TokenStats] = defaultdict(_TokenStats)
        for ep in episodes:
            day = ep.start.date().isoformat() if ep.start is not None else None
            for name in fields:
                text = getattr(ep, name, None)
                if not text:
                    continue
                for m in _TOKEN.finditer(text):
                    token = m.group()
                    st = self._tokens[token.casefold()]
                    st.episodes.add(ep.index)
                    if day is not None:
                        st.days.add(day)
                    if any(c.isupper() for c in token[1:]) and any(c.islower() for c in token):
                        st.mixed_case = True
                    if not _is_initial(text, m.start()):
                        st.non_initial += 1
                        if token[0].isupper():
                            st.non_initial_caps += 1

    def word_passes(self, word: str) -> bool:
        if not word or any(c.isdigit() for c in word):
            return False
        if any(c.isupper() for c in word[1:]) and any(c.islower() for c in word):
            return False  # mixed case in the key itself
        st = self._tokens.get(word.casefold())
        if st is None or st.mixed_case:
            return False
        if st.non_initial and st.non_initial_caps / st.non_initial >= NAME_LIKE_SHARE:
            return False
        return len(st.episodes) >= MIN_EPISODES and len(st.days) >= MIN_DAYS


@dataclass
class KeyFilter:
    vocab: frozenset[str]
    text_stats: TextStats | None = None

    @classmethod
    def build(
        cls,
        *,
        audiences: Iterable[str] = (),
        labels: Iterable[str] = (),
        text_stats: TextStats | None = None,
    ) -> KeyFilter:
        words = {RESERVED, *WEEKDAY_KEYS, *WEEKDAY_NAMES, *audiences, *labels}
        return cls(frozenset(words), text_stats)

    def segment_passes(self, segment: str, cls_name: str) -> bool:
        if segment == RESERVED:
            return True
        if cls_name == NUMERIC:
            return bool(_NUMERIC.match(segment))
        if cls_name == VOCAB:
            return segment in self.vocab
        if find_identifier(segment) is not None or self.text_stats is None:
            return False
        words = segment.split()
        return bool(words) and all(self.text_stats.word_passes(w) for w in words)

    def key_passes(self, key: str, classes: Sequence[str]) -> bool:
        if key == RESERVED:
            return True
        segments = key.split(".")
        if len(segments) != len(classes):
            return False
        return all(self.segment_passes(s, c) for s, c in zip(segments, classes, strict=True))

    def apply(self, output: Output, classes: KeyClasses) -> tuple[Output, int]:
        """Return the output without failing keys and the number of keys dropped. A metric
        without a declaration is treated as a single ``text`` segment."""
        dropped = 0
        out: Output = {}
        for metric, months in output.items():
            decl = classes.get(metric, (TEXT,))
            kept_months: dict[str, dict[str, float]] = {}
            for month, keys in months.items():
                kept = {k: v for k, v in keys.items() if self.key_passes(k, decl)}
                dropped += len(keys) - len(kept)
                if kept:
                    kept_months[month] = kept
            if kept_months:
                out[metric] = kept_months
        return out, dropped
