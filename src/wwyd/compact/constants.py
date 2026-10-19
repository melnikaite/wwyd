# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compaction defaults (SPEC §10). One place, so config/admin ceilings can override later."""

from __future__ import annotations

from datetime import timedelta

# §10.1 dedupe and absorb
FTS_CANDIDATES = 5
DUPLICATE_JACCARD = 0.6
BORDERLINE_JACCARD = 0.3
JUDGE_MAX_PAIRS = 40  # per category per judge call; the rest waits for the next run

# §10.3 decay
DECAY_HALF_LIFE_DAYS = 6 * 30.4375  # six months
DECAY_FLOOR = 0.2
DECAY_MIN_STEP = 0.005  # smaller drops are not written (they accumulate until next night)

# §10.4 size budget: the per-category and token ceilings are limits (wwyd.limits,
# ``max_claims_per_category`` / ``max_approved_tokens``), configurable per user.
CHARS_PER_TOKEN = 4

# §10.5 purge
PURGE_AFTER = timedelta(days=30)

# Nightly run, persona-local time.
NIGHTLY_HOUR = 3
NIGHTLY_MINUTE = 30
SCHEDULER_TICK_SECONDS = 60


def absorbed_confidence(a: float, b: float) -> float:
    """Confidence after two claims agree: goes up, never reaches 1."""
    hi, lo = max(a, b), min(a, b)
    return round(min(0.99, hi + (1 - hi) * lo * 0.5), 4)


# Negation/polarity markers (i18n data: en, ru, uk, de, es, fr). A pair with Jaccard
# >= DUPLICATE_JACCARD whose token symmetric difference contains one of these may be a
# contradiction, so it goes to the judge instead of merging. Tokens are casefolded \w+
# words, so "don't" appears as "don" + "t".
POLARITY_MARKERS = frozenset(
    {
        # en
        "never", "not", "no", "none", "nobody", "nothing", "don", "doesn", "didn", "won",
        "isn", "aren", "wasn", "can", "cannot", "rarely", "seldom", "hardly", "avoids",
        "avoid", "declines", "decline", "refuses", "refuse", "without",
        # ru
        "не", "ни", "никогда", "нет", "редко", "избегает", "отказывается", "без",
        # uk
        "ніколи", "ні", "рідко", "уникає", "відмовляється",
        # de
        "nicht", "nie", "niemals", "kein", "keine", "keinen", "selten", "ohne",
        # es
        "nunca", "jamás", "rara", "sin",
        # fr
        "ne", "pas", "jamais", "rarement", "sans", "aucun", "aucune",
    }
)  # fmt: skip
