# SPDX-License-Identifier: AGPL-3.0-or-later
"""``stylometry``: how the owner writes to one audience (SPEC §7.2, D22; docs/habits.md).

Input: the ``said`` of the audience's episodes (the only owner text). State, per month:

* ``n`` (``_``): messages; ``days`` (``DD``);
* ``chars`` / ``sentences``: histograms keyed by the lower bucket edge (:data:`CHAR_EDGES`,
  :data:`SENTENCE_EDGES`);
* ``start``: first letter, ``0`` lowercase, ``1`` capital, ``2`` no letter first;
* ``ending``: index into :data:`ENDING_CLASSES` (the helpers' classes plus ``brackets`` for
  ``))``-style endings, which take precedence over ``smiley``);
* ``emoji``: messages by emoji count (``0``, ``1``, ``2``, ``3`` = 3 or more);
* ``words`` / ``bigrams``: ``text`` keys, the 200 most frequent per month (contract cap),
  then the host key filter.

Favourite words are the audience's words with the highest Dunning log-likelihood (G²)
against the owner's usage with every other audience (:func:`keyness`).
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter as Tally
from collections.abc import Mapping, Sequence

from wwyd.analyzers.builtin.base import (
    MIN_SHARE,
    Builtin,
    ClaimSpec,
    Context,
    Counter,
    Item,
    bucket,
    enough,
    hist,
    pct,
    period_of,
)
from wwyd.analyzers.contract import Output
from wwyd.analyzers.keyfilter import NUMERIC, RESERVED, TEXT
from wwyd.analyzers.state import State, read_state, totals
from wwyd.ingest.guard import find_identifier
from wwyd.sandbox.helpers import wwyd_helpers as h

NAME = "stylometry"
CLAIM_CATEGORY = "communication/style"
CHAR_EDGES = (0, 10, 20, 30, 50, 80, 120, 200, 300, 500, 800)
SENTENCE_EDGES = (0, 1, 2, 3, 4, 6, 10)
ENDING_CLASSES = (*h.ENDINGS, "brackets")
EMOJI_EDGES = (0, 1, 2, 3)
MAX_KEYS = 200  # contract: keys per metric per month
MAX_FAVOURITES = 10
SHOWN_FAVOURITES = 5
MIN_KEY_COUNT = 5  # a favourite word occurs at least this often with the audience
MIN_G2 = 6.63  # log-likelihood critical value for p < 0.01 (1 d.f.)
MAX_TEXT = 500

ENDING_WORDS = {
    "none": "no final punctuation",
    "period": "ends with a period",
    "exclamation": "ends with “!”",
    "question": "ends with “?”",
    "ellipsis": "ends with “…”",
    "smiley": "ends with a smiley",
    "emoji": "ends with an emoji",
    "brackets": "ends with “)))”",
}


def ending_class(text: str) -> str:
    s = text.rstrip()
    if s.endswith("))") and not s.endswith("())"):
        return "brackets"
    return h.ending(s)


def _start(text: str) -> str:
    for ch in text:
        if ch.isalpha():
            return "0" if ch.islower() else "1"
        if not ch.isspace():
            return "2"
    return "2"


def _top(c: Tally[str]) -> dict[str, float]:
    return {k: float(v) for k, v in c.most_common(MAX_KEYS)}


def run(items: Sequence[Item], ctx: Context) -> Output:
    c = Counter()
    words: dict[str, Tally[str]] = {}
    bigrams: dict[str, Tally[str]] = {}
    for it in items:
        text = (it.episode.said or "").strip()
        if not text or it.month is None:
            continue
        m = it.month
        c.add("n", m, RESERVED)
        c.day(it)
        c.add("chars", m, str(CHAR_EDGES[bucket(len(text), CHAR_EDGES)]))
        n_sent = len(h.sentences(text))
        c.add("sentences", m, str(SENTENCE_EDGES[bucket(n_sent, SENTENCE_EDGES)]))
        c.add("start", m, _start(text))
        c.add("ending", m, str(ENDING_CLASSES.index(ending_class(text))))
        c.add("emoji", m, str(EMOJI_EDGES[bucket(len(h.emojis(text)), EMOJI_EDGES)]))
        words.setdefault(m, Tally()).update(h.count_words([text]))
        bigrams.setdefault(m, Tally()).update(h.count_bigrams([text]))
    out = c.output()
    for metric, per in (("words", words), ("bigrams", bigrams)):
        months = {m: _top(t) for m, t in per.items() if t}
        if months:
            out[metric] = months
    return out


# --- numbers ----------------------------------------------------------------------------


def percentile(counts: Sequence[int], edges: Sequence[float], q: float) -> float:
    """Upper edge of the bucket holding the q-quantile (the open last bucket: 2 × its
    lower edge); 0 when empty. Conservative: the true quantile is at most this."""
    n = sum(counts)
    if n == 0:
        return 0.0
    acc = 0
    for i, k in enumerate(counts):
        acc += k
        if acc >= q * n:
            return float(edges[i + 1] if i + 1 < len(edges) else 2 * edges[i])
    return float(2 * edges[-1])


def profile(state: State) -> dict[str, float | list[float]]:
    """The numeric profile stored in ``evidence.stats`` (numbers only, export schema)."""
    chars = hist(state, "chars", [str(e) for e in CHAR_EDGES])
    sents = hist(state, "sentences", [str(e) for e in SENTENCE_EDGES])
    ends = hist(state, "ending", [str(i) for i in range(len(ENDING_CLASSES))])
    starts = hist(state, "start", ["0", "1", "2"])
    emo = hist(state, "emoji", [str(e) for e in EMOJI_EDGES])
    n = int(totals(state, "n").get(RESERVED, 0)) or sum(chars)
    lettered = starts[0] + starts[1]
    dom = (
        max(range(len(ends)), key=lambda i: ends[i]) if sum(ends) else ENDING_CLASSES.index("none")
    )
    return {
        "n": n,
        "chars_hist": chars,
        "chars_p50": percentile(chars, CHAR_EDGES, 0.5),
        "chars_p90": percentile(chars, CHAR_EDGES, 0.9),
        "sentences_hist": sents,
        "sentences_p50": percentile(sents, SENTENCE_EDGES, 0.5),
        "sentences_p90": percentile(sents, SENTENCE_EDGES, 0.9),
        "lowercase_share": round(starts[0] / lettered, 3) if lettered else 0.0,
        "capital_share": round(starts[1] / lettered, 3) if lettered else 0.0,
        "ending_hist": ends,
        "ending_dominant": dom,
        "ending_share": round(ends[dom] / n, 3) if n else 0.0,
        "period_share": round(ends[ENDING_CLASSES.index("period")] / n, 3) if n else 0.0,
        "emoji_hist": emo,
        "emoji_share": round(sum(emo[1:]) / n, 3) if n else 0.0,
    }


def keyness(
    target: Mapping[str, float], reference: Mapping[str, float], *, limit: int = MAX_FAVOURITES
) -> list[str]:
    """Words over-represented in ``target`` against ``reference`` by Dunning's G²
    (log-likelihood), with at least :data:`MIN_KEY_COUNT` uses and G² ≥ :data:`MIN_G2`;
    best first, ties by count then alphabetically. Empty without a reference corpus."""
    c, d = sum(target.values()), sum(reference.values())
    if c <= 0 or d <= 0:
        return []
    scored = []
    for word, a in target.items():
        b = reference.get(word, 0.0)
        if a < MIN_KEY_COUNT or a / c <= b / d:
            continue
        e1, e2 = c * (a + b) / (c + d), d * (a + b) / (c + d)
        g2 = 2 * (a * math.log(a / e1) + (b * math.log(b / e2) if b > 0 else 0.0))
        if g2 >= MIN_G2:
            scored.append((-g2, -a, word))
    return [w for _, _, w in sorted(scored)[:limit]]


def _analyzers(conn: sqlite3.Connection, persona_id: str) -> dict[str, str]:
    """audience_id -> analyzer_id of the persona's stylometry rows."""
    return {
        r["scope_id"]: r["id"]
        for r in conn.execute(
            "SELECT id, scope_id FROM analyzers WHERE persona_id = ? AND origin = 'builtin'"
            " AND name = ? AND scope_kind = 'audience'",
            (persona_id, NAME),
        )
    }


def _favourites(
    conn: sqlite3.Connection, analyzer_id: str, own: State, metric: str, limit: int
) -> list[str]:
    persona = conn.execute(
        "SELECT persona_id FROM analyzers WHERE id = ?", (analyzer_id,)
    ).fetchone()
    if persona is None:
        return []
    reference: dict[str, float] = {}
    for other in _analyzers(conn, persona[0]).values():
        if other == analyzer_id:
            continue
        for k, v in totals(read_state(conn, other), metric).items():
            reference[k] = reference.get(k, 0.0) + v
    return keyness(totals(own, metric), reference, limit=limit)


def favourite_words(conn: sqlite3.Connection, persona_id: str, audience_id: str) -> list[str]:
    """The audience's current favourite words (≤ 10, best first) from accumulated state;
    empty without a stylometry row or without any other audience to compare against."""
    analyzer_id = _analyzers(conn, persona_id).get(audience_id)
    if analyzer_id is None:
        return []
    return _favourites(conn, analyzer_id, read_state(conn, analyzer_id), "words", MAX_FAVOURITES)


def favourite_phrases(
    conn: sqlite3.Connection, persona_id: str, audience_id: str, limit: int = 3
) -> list[str]:
    analyzer_id = _analyzers(conn, persona_id).get(audience_id)
    if analyzer_id is None:
        return []
    return _favourites(conn, analyzer_id, read_state(conn, analyzer_id), "bigrams", limit)


# --- claim ------------------------------------------------------------------------------


def _range(lo: float, hi: float) -> str:
    return f"{int(lo)}" if lo == hi else f"{int(lo)}–{int(hi)}"


def describe(stats: Mapping[str, float | list[float]]) -> tuple[list[str], list[float]]:
    """Rule-like phrases and their shares (each share ≥ MIN_SHARE)."""
    n = int(stats["n"])  # type: ignore[arg-type]
    parts: list[str] = []
    shares: list[float] = []
    sents: list[float] = stats["sentences_hist"]  # type: ignore[assignment]
    # sentences_hist buckets by SENTENCE_EDGES: index 1 = one sentence, 2 = two
    for upto in (1, 2, 3):
        share = sum(sents[: SENTENCE_EDGES.index(upto) + 1]) / n if n else 0.0
        if share >= MIN_SHARE:
            p90 = stats["chars_p90"]
            short = "short " if float(p90) <= 120 else ""  # type: ignore[arg-type]
            unit = "line" if upto == 1 else "lines"
            parts.append(f"in {_range(1, upto)} {short}{unit} (90% under {int(p90)} chars)")  # type: ignore[arg-type]
            shares.append(share)
            break
    low = float(stats["lowercase_share"])  # type: ignore[arg-type]
    if low >= MIN_SHARE:
        parts.append("lowercase")
        shares.append(low)
    elif float(stats["capital_share"]) >= MIN_SHARE:  # type: ignore[arg-type]
        parts.append("starting with a capital")
        shares.append(float(stats["capital_share"]))  # type: ignore[arg-type]
    dom = ENDING_CLASSES[int(stats["ending_dominant"])]  # type: ignore[arg-type]
    end_share = float(stats["ending_share"])  # type: ignore[arg-type]
    period = float(stats["period_share"])  # type: ignore[arg-type]
    if end_share >= MIN_SHARE and dom in ENDING_WORDS and dom != "none":
        parts.append(ENDING_WORDS[dom])
        shares.append(end_share)
    elif 1 - period >= MIN_SHARE:
        parts.append("no final period")
        shares.append(1 - period)
    emo = float(stats["emoji_share"])  # type: ignore[arg-type]
    if emo >= 0.5:
        parts.append(f"emoji in {pct(emo)}% of messages")
    elif emo <= 0.05:
        parts.append("no emoji")
    return parts, shares


def render(state: State, ctx: Context) -> list[ClaimSpec]:
    stats = profile(state)
    n = int(stats["n"])  # type: ignore[arg-type]
    if n == 0:
        return []
    period = period_of(state)
    spec = ClaimSpec("", "style", None, 0.0, n, period, stats, audience_id=ctx.scope_id)
    if not enough(n, period):
        return [spec]
    parts, shares = describe(stats)
    if not shares:
        return [spec]
    spec.share = min(shares)
    words: list[str] = []
    if ctx.conn is not None:
        words = _favourites(ctx.conn, ctx.analyzer_id, state, "words", SHOWN_FAVOURITES)
    head = (
        f"Writes to {ctx.scope_path} "
        if parts[0].startswith("in ")
        else f"Writes to {ctx.scope_path}: "
    )
    body = head + ", ".join(parts)
    text = body + f" ({n} messages)."
    for k in range(len(words), 0, -1):
        candidate = body + ", favourite words: " + ", ".join(words[:k]) + f" ({n} messages)."
        if len(candidate) <= MAX_TEXT and find_identifier(candidate) is None:
            text = candidate
            break
    spec.text = text if len(text) <= MAX_TEXT else None
    return [spec]


BUILTIN = Builtin(
    name=NAME,
    scope_kind="audience",
    kind="style",
    statement_template="How the owner writes to {audience}",
    key_classes={
        "n": (NUMERIC,),
        "days": (NUMERIC,),
        "chars": (NUMERIC,),
        "sentences": (NUMERIC,),
        "start": (NUMERIC,),
        "ending": (NUMERIC,),
        "emoji": (NUMERIC,),
        "words": (TEXT,),
        "bigrams": (TEXT,),
    },
    run=run,
    render=render,
    claim_category=CLAIM_CATEGORY,
    text_fields=("said",),
)
