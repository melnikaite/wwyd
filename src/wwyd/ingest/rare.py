# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concentrated batch words: an extra check for LLM-written names and code (SPEC §2 P2).

The verbatim guard (§7.7) catches capitalised and identifier-like tokens, but not a name the
owner always writes in lowercase. Derived *names* (label and tag names, analyzer statements
and code, usage notes) are short and generic by design, so they get one more check: a word
that occurs in the batch's texts but is **concentrated** there (in fewer than 5 distinct
episodes or on fewer than 3 distinct days, the key filter's spread rule of §7.2 inverted)
is treated as possibly a name and the text is dropped. Generic words ("meeting",
"declined") are spread over the batch; a lowercase name that is just as spread is the
residual risk stated in SPEC §2.

Holds derived per-token sets only (episode indices and day strings) and must be cleared with
the batch.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from wwyd.ingest.episodes import Episode

MIN_EPISODES = 5
MIN_DAYS = 3
MIN_LEN = 3  # shorter tokens ("ok", "a") are never treated as names

_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
_FIELDS = ("situation", "decision", "where", "said")


class RareWords:
    def __init__(self, episodes: Iterable[Episode]) -> None:
        eps: dict[str, set[int]] = defaultdict(set)
        days: dict[str, set[str]] = defaultdict(set)
        for ep in episodes:
            day = ep.start.date().isoformat() if ep.start is not None else None
            for name in _FIELDS:
                text = getattr(ep, name, None)
                if not text:
                    continue
                for tok in _TOKEN.findall(text):
                    if len(tok) < MIN_LEN:
                        continue
                    t = tok.casefold()
                    eps[t].add(ep.index)
                    if day is not None:
                        days[t].add(day)
        self._rare: set[str] = {
            t for t, e in eps.items() if len(e) < MIN_EPISODES or len(days[t]) < MIN_DAYS
        }

    def words(self, text: str) -> set[str]:
        return {t.casefold() for t in _TOKEN.findall(text) if len(t) >= MIN_LEN}

    def find(self, text: str, *, ignore: Iterable[str] = ()) -> bool:
        """True when ``text`` contains a concentrated batch word (not in ``ignore``)."""
        hits = self.words(text) & self._rare
        return bool(hits - {w.casefold() for w in ignore})

    def clear(self) -> None:
        self._rare.clear()
