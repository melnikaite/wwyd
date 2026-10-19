# Builtin analyzers

Habits nobody puts into words are computed in code from the processed episodes of every
`ingest_raw` job (SPEC §7.2, D19). Code: `src/wwyd/analyzers/`. Tests with synthetic
episodes: `tests/test_analyzers_builtin.py` (ground truth, thresholds, accumulation,
resends, DST, weakening), `tests/test_analyzer_contract.py` (output contract and key
filter), `tests/test_analyzer_tools.py` (the three tools).

## How they work

A builtin is shipped code with two pure functions:

- `run(items, ctx)` turns the episodes of one scope into **counters** in the output
  contract `{metric: {"YYYY-MM": {key: number}}}`. Episodes are never kept.
- `render(state, ctx)` turns the accumulated counters into one claim spec per *facet*:
  text, `share`, sample count, period and `stats`, or no text below threshold.

The host validates the output (`analyzers/contract.py`), drops every key that fails the
key filter (`analyzers/keyfilter.py`, counted in `analyzer_keys_dropped`, never logged),
and **adds** the rest to `analyzer_state` (`analyzers/state.py`). Then it reads the whole
state back and re-renders the claims. Because state accumulates across jobs, small batches
still count: a claim appears as soon as the totals pass, even if no single batch would.

**Scope.** Builtin rows are created lazily, one per (builtin, exact category) the first
time a job has episodes in that category. A row sees the job's processed episodes whose
category is *exactly* its category, not the subtree: every episode is counted once per
builtin, and the claim lands in the episode's own category. Audience-scoped builtins
(stylometry) get one row per audience with the episodes of that audience; LLM
analyzers (M7c) are bound to a subtree and share the same validate → filter → merge path.
A `disabled` row is skipped (`analyzers_skipped_disabled`); builtins cannot be retired.

**Each episode counts once.** Only processed episodes reach the analyzers. A `partial`
job's remaining episodes are counted when they are resent, so a job plus its resend gives
the same state as one full job (tested). An `interrupted` job ran no analyzers.

**Thresholds** (constants in `analyzers/builtin/base.py`):

- at least **30 samples**;
- a **share of at least 0.75** for any rule-like statement;
- a **period of at least 3 weeks** (21 days, from the first to the last sampled local day,
  read from the `days` metric).

**Host-computed fields.** `start`/`end` are already in the persona timezone; a naive value
is read in it. `weekday`, `hour`, `month` and the day come from the local start. Durations
are computed on UTC instants, so a DST switch never adds or removes an hour (subtracting
two aware datetimes with the same `ZoneInfo` would give wall-clock time instead).
`latency_s` is `when` (or `when.start`) − `situation_at`, also on UTC instants; it is null
when either is missing or the difference is negative. Analyzers (and the sandbox input)
see `local_start`, `weekday`, `hour`, `month`, `duration_s`, `latency_s`, `labels`,
`tags`, `category`, `audience`, `where` and the text fields.

**Intervals.** An episode with `start` and `end` is an interval. Intervals of 0 minutes or
longer than 8 hours (all-day blocks) are ignored by `time_duration` (lengths) and
`time_gaps`. Every interval counts, whatever the episode class: `when` is always when the
owner acted (D23), so a call (`situation` + `said`) with a span is a span. Response
latency comes only from `situation_at`.

**Confidence** is `min(0.95, 0.35 + 0.6 × share × min(1, samples / 150))`.

## Keyed claims

Key `builtin:<name>:<scope_id>` plus `:<facet>` when a builtin renders several statements
(IDs, so renames keep the key). Texts are rendered by code only, hold only numbers,
weekday/hour words and the category path, and start `pending`, `visibility: shared`,
`origin: derived`. `evidence` = `{sources: {analyzer: n}, share, period, stats}`; it is
replaced on every run (the state is the source of truth), never summed.

- **No live claim, passes:** a new pending claim.
- **Pending or approved claim, passes:** numbers, confidence, `last_evidence_at` refreshed;
  the text is re-rendered while `origin` is `derived` (never after an owner edit).
- **Below threshold, pending claim:** archived (applied `decay` change, reason
  `analyzer_weakened`). A later passing run creates a new pending claim.
- **Below threshold, approved claim:** numbers refreshed, text kept, and a `proposed`
  archive change for the owner (one at a time), like a lost contradiction (SPEC §10.2).
- **Rejected claim:** untouched (negative memory); a re-run never resurrects it.

Counters in the job's `counts`: `analyzers_run`, `analyzer_errors`, `analyzer_keys_dropped`,
`analyzers_skipped_disabled`, `analyzer_claims_created|updated|weakened|proposed`.

## Key classes

Each metric declares one class per `.`-separated key segment; a key with the wrong number
of segments is dropped.

- `numeric`: digits and `:_-` only (hours, histogram edges, day of month).
- `vocab`: audience names and IDs, weekday names (`mon`…`sun`, `monday`…), the category's
  labels (`category_labels`), and the reserved `_`.
- `text` (default): passes the guard's regex part (no email, phone, URL, handle); is not
  name-like (no digit, no mixed case, not capitalised in ≥ 50% of its non-initial
  occurrences in the batch); and occurs in ≥ 5 distinct episodes on ≥ 3 distinct days of
  the batch. Multi-word keys (bigrams) check each word. Statistics come from in-memory
  episode text (default `said`). Residual risk: a lowercase name used that often passes
  (SPEC §2).

## time_activity

**Scope** category. **State:** `wd_hour` = `<weekday>.<HH>` (vocab, numeric), episode
count by local weekday and hour; `audience_weekday` = `<audience_id>.<weekday>` (`_` when
the episode named no audience); `days` = `DD`. Per-audience detail is kept per weekday, not
per hour, so a month stays under the contract's 200 keys per metric.

- Facet `hours`: Mon–Fri hourly counts; drop the earliest and latest 10% of activity; the
  remaining window `[start, end)` holds share `s`. If `s ≥ 0.75`:
  *"work/meetings: active 10:00–15:00 on weekdays (100% of 84 weekday episodes)."*
- Facet `slots` (old `meeting_free_slots`): Mon–Fri split into mornings and afternoons
  (from 13:00). With mean ≥ 3 per half-day, the (at most two) half-days with at most
  `0.25 × mean`, share `min(1 − count/mean)`: *"work/meetings: none on Friday afternoons
  (0 of 84 episodes)."* or *"… rarely on Monday mornings (2 of 140 episodes)."*
- Facet `weekends`: none at all, or the weekend daily average ≤ 15% of the weekday daily
  average: *"work/meetings: none on weekends (0 of 84 episodes)."* / *"… rarely on
  weekends (3 of 200 episodes)."*

**Test:** 6 weeks of meetings at 10:00 (25 min), 11:00 (50 min) and 14:00 (25 min) except
Friday afternoons produce exactly the three texts above; the DST test checks hours stay
local across 2026-10-25.

## time_duration

**Scope** category. **State:** `length` = `<minutes>` (numeric), interval length (any
episode class) rounded to 5 min, `180` = 180+; `latency` = `<audience_id>.<1|0>.<edge>`
(vocab, numeric, numeric) for every episode with `latency_s` (`when` − `situation_at`),
`1` when the situation arrived Mon–Fri 09:00–18:00 local time (`situation_at`), edges 0,
5, 15, 60, 240, 1440 min (latencies over 14 days and episodes without an audience are
ignored); `days`.

- Facet `length` (old `meeting_length`): the most common bucket holds ≥ 0.75 →
  *"work/meetings: typically 25 min (80% of 212)."*; else the top two together →
  *"work/meetings: typically 25 or 50 min (100% of 84)."*
- Facet `latency:<audience_id>` (style, applies to that audience only; old
  `reply_latency`): per side with ≥ 10 responses, the smallest edge with a cumulative share
  ≥ 0.75: *"communication/style: responds within 5 min in work hours (100% of 30
  responses)."*, *"… within 15 min in work hours, within 4 hours otherwise (…)"*, or one side.

**Test:** the 6-week synthetic calendar gives *"typically 25 or 50 min (100% of 84)"*;
30 replies with `situation_at` 3 minutes before `when` give the latency text above with audience `work-chat`.

## time_gaps

**Scope** category. **State:** `gap` = `<edge>` (numeric, edges 0, 5, 10, 15, 30, 60 min);
`days`. Per local day, intervals of any episode class sorted by start; gap = start − latest end so far
(overlap = 0); gaps of 120 min or more are not "between" items. Gaps are computed within
one job: two intervals of one day sent in different jobs add no gap.

- For 15, 10, 5 min in that order, share of gaps ≥ that: if ≥ 0.75 →
  *"work/meetings: keeps at least 15 min between items (100% of 30 same-day gaps)."*
- Else gaps under 5 min ≥ 0.75 → *"work/meetings: usually back to back (80% of 90 gaps
  under 5 min)."*

**Test:** the synthetic calendar's 35-minute gaps give the 15-minute text; a later month
of 0- and 20-minute gaps turns the approved claim into a proposal and archives the pending
length claim.

## stylometry

**Scope** audience (one row per audience; an episode naming no audience counts for the
default audience). **Input:** `said` only, the owner's own text. **State** per month:
`n` (`_`, messages), `days` (`DD`), `chars` (lower edge of 0, 10, 20, 30, 50, 80, 120, 200,
300, 500, 800 characters of the stripped text), `sentences` (lower edge of 0, 1, 2, 3, 4,
6, 10 sentences by `wwyd_helpers.sentences`), `start` (first letter: `0` lowercase, `1`
capital, `2` the text starts with no letter), `ending` (index into the classes
`period, exclamation, question, ellipsis, smiley, emoji, other, none, brackets`; the last
is `))`+ endings such as `)))`, which win over `smiley`), `emoji` (messages with 0, 1, 2,
3+ emoji), `words` and `bigrams` (`text` keys; per month only the 200 most frequent, the
contract cap, then the key filter drops name-like keys and keys in fewer than 5 episodes
on 3 days of the batch).

**Numbers** (`evidence.stats`, numbers only): `n`; `chars_hist`, `sentences_hist`; p50
and p90 as the upper edge of the bucket holding that quantile (the open last bucket counts
as twice its lower edge), so they are upper bounds; `lowercase_share` and `capital_share`
of messages starting with a letter; `ending_hist`, `ending_dominant` (index), its
`ending_share`, `period_share`; `emoji_hist`, `emoji_share` (messages with any emoji).

**Favourite words (keyness).** Dunning's log-likelihood G² of each word of the audience
against the owner's usage with all *other* audiences (summed accumulated state):
`G² = 2·(a·ln(a/E₁) + b·ln(b/E₂))`, `E₁ = c·(a+b)/(c+d)`, `E₂ = d·(a+b)/(c+d)` with `a`, `b`
the word's counts and `c`, `d` the corpus sizes. A favourite is over-represented
(`a/c > b/d`), used ≥ 5 times with the audience, and has G² ≥ 6.63 (p < 0.01); best G²
first, at most 10 (`favourite_words(conn, persona_id, audience_id)`), 5 in the claim text.
Bigrams use the same measure (favourite phrases, predict only). With a single audience
there is no reference and no favourites. Audiences render in ID order within a job, so an
audience rendered before another one's state of the same job is merged sees it on the next
run.

**Claim** (`builtin:stylometry:<audience_id>`, kind `style`, category
`communication/style`, applies to that audience only). Needs ≥ 30 messages over ≥ 3 weeks
and at least one rule-like part (share ≥ 0.75); the confidence share is the lowest of them:
- sentences: the smallest of 1, 2, 3 that covers ≥ 0.75 → "in 1–2 short lines (90% under
  50 chars)" ("short" when p90 ≤ 120);
- lowercase or capital start ≥ 0.75;
- dominant ending ≥ 0.75 (not `none`) → "ends with “)))”", "ends with a period", …;
  else no period in ≥ 0.75 → "no final period";
- emoji in ≥ 50% → "emoji in 60% of messages"; ≤ 5% → "no emoji" (descriptive only);
- favourite words, as many as keep the text ≤ 500 chars and guard-clean.

*"Writes to family in 1 short line (90% under 50 chars), lowercase, no final period,
favourite words: darling, hugs (35 messages)."*

**In predict** (`kind: reply` only, §8.1 steps 6 and 8). Only the approved claim keyed for
the resolved audience and passing the §8.3 SQL filter counts. Its stats become a style
block in the answer's user message (the system prompt stays static): p50/p90 chars and
sentences, lowercase-start share, usual ending and its share, period share, emoji share,
favourite words (from the state behind the key; when the owner edited the text, only the
words the text still names) and favourite phrases (derived text only). Post-processing,
each rule only at share ≥ 0.8: lowercase the first letter (not acronyms or a lone "I") when
`lowercase_share` ≥ 0.8; drop a single trailing period when `1 − period_share` ≥ 0.8; if the
answer is longer than 3 × p90 chars, one `shorten` call (counted in limits, usage and the
audit row's tokens); if it fails or is still longer than that, the original is kept. The
rules run after shortening. No typos are ever added.

**Test:** synthetic family replies (lowercase, no period, `)))`, emoji, short) against
capitalised work replies give the texts above and work-specific favourites; a capitalised
name and a name in 3 episodes never become keys; 20 then 15 messages accumulate into a
claim; predict tests cover each rule, its threshold, the re-ask and `decision`.

## Not ported (no input in the episode model)

The old `accept_buffer` and `accept_size` rules needed invite responses and attendee
counts; `message_length` is now part of `stylometry`.

## Annotation

Before the LLM passes, the LLM labels and code counts (SPEC §7.3). A category is annotated
only when its labels will be counted: it has at least `analyzer_min_episodes` (100)
episodes in the job, or an active LLM analyzer is bound to it or to an ancestor. Smaller
increments cost no annotation tokens.

- `decision` with at most 50 distinct normalised values (casefolded, whitespace collapsed,
  trailing punctuation dropped): one call maps each distinct value to `accepted`,
  `declined` or `other`.
- `situation` with at most 200 distinct values: one call maps them to a short type
  (`1-1`, `standup`), reusing the category's situation labels.
- More distinct values (free text): tagging, 20 episodes per call, at most 5 tags each,
  reusing the category's tag vocabulary.

Names are normalised to one path segment (lowercase, letters, digits and `-`, at most 40
chars). A new name must pass the verbatim guard and the concentrated-word check: a word
that occurs in the batch in fewer than 5 episodes or on fewer than 3 days may be a name and
is refused. Accepted names are stored in `category_labels` (at most 60 per category and
field); the value-to-label mappings are never stored. Labels reach the analyzers as
`labels` / `tags` and the pass prompts as `[labels: decision=declined; situation=1-1]`.

## LLM analyzers

Hypotheses written by the LLM and checked by code (SPEC §7.4). Code:
`wwyd/analyzers/authoring.py`, `hypothesis.py`, `llm.py`.

- **Trigger:** a category with at least 100 processed episodes of its own in the job, one
  round per category per job, room under `max_analyzers`, sandbox assets present and
  sandbox time left. The analyzer is bound to the category's subtree.
- **Authoring:** one call with an aggregate profile (counts, fill rates, distinct counts,
  label and tag distributions, weekday and hour histograms, length and duration stats), 20
  truncated samples, the helper API, the category's existing analyzer statements (retired
  ones too) and its claims. At most 5 hypotheses `{statement_template, kind, source}`.
  Placeholders: `{share}` (`87%`), `{n}`, `{hits}`, `{months}`. Statement and source
  (including string literals, comments and identifier parts) pass the verbatim guard and the
  concentrated-word check; a statement that restates an existing analyzer of the category
  (token Jaccard >= 0.6, placeholders removed) is dropped.
- **Contract:** the script reads `{"episodes": [...]}` on stdin and prints
  `{"n": {"YYYY-MM": {"_": n}}, "hits": {...}}`, optionally `base_n` / `base_hits`. Other
  metrics and keys are dropped.
- **Test:** older 70% of the subtree's timed processed episodes and the recent 30%
  holdout; no state written. Accept if n >= 30, hits/n >= 0.75, the one-sided exact
  binomial p-value against the base rate (base_hits/base_n, else 0.5) passes
  Benjamini–Hochberg at q = 0.1 across the round, and the holdout share is >= 0.75.
- **Accepted:** `active`; the same job's analyzer step runs it over all processed episodes
  (each counted once) and creates the pending claim `anl:<id>`. **Rejected:** `retired`,
  statement kept, source cleared.
- **Rendering:** over the 12 months up to the latest month with data: n >= 30 and share
  >= 0.75, unless the last 3 months hold at least 10 samples with a share below 0.75; a
  failing render weakens the claim like a builtin's (pending -> archived, approved -> a
  proposal). Evidence `stats`: `n`, `hits` (12-month totals), `n_by_month`,
  `hits_by_month` (lists over the months of `period`, oldest first).
- **Failures:** for LLM analyzers `failures` counts consecutive failed runs (reset by a
  success); the third disables the analyzer. Run wall time is charged to
  `sandbox_cpu_seconds_per_day` (usage metric `sandbox_seconds`).

Synthetic test: `tests/test_llm_analyzers.py` (late meetings declined: accepted with 100%;
a Monday hypothesis rejected and not re-proposed; weakening after three months of accepted
late meetings).

## word_usage

Usage notes for an audience's favourite words (SPEC §7.2). When the set returned by
`stylometry.favourite_words` differs from the set noted last time, up to 5 snippets of
±5 words per word are taken from `said` in memory and one call writes a style note, key
`builtin:word_usage:<audience_id>`. The note passes the guard, a 5-gram check against the
snippets and the concentrated-word check. The noted set is the `noted` metric of the
audience's `word_usage` builtin row (one key per word; these are stylometry keys that
already passed the key filter); it is replaced on every attempt, so an unchanged set costs
nothing. Disabling that row turns notes off for the audience.
