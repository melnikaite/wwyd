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
(stylometry, next phase) get one row per audience with the episodes of that audience; LLM
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

**Intervals.** An episode with `start` and `end` is an interval. Intervals of 0 minutes or
longer than 8 hours (all-day blocks) are ignored by `time_duration` (lengths) and
`time_gaps`. A *reply* episode (`said` + `situation`) with an interval `when` is read as
"message received at start, reply sent at end" and feeds reply latency instead.

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

**Scope** category. **State:** `length` = `<minutes>` (numeric), non-reply interval length
rounded to 5 min, `180` = 180+; `latency` = `<audience_id>.<1|0>.<edge>` (vocab, numeric,
numeric) for reply intervals, `1` when the message came in Mon–Fri 09:00–18:00, edges 0, 5,
15, 60, 240, 1440 min (latencies over 14 days and replies without an audience are
ignored); `days`.

- Facet `length` (old `meeting_length`): the most common bucket holds ≥ 0.75 →
  *"work/meetings: typically 25 min (80% of 212)."*; else the top two together →
  *"work/meetings: typically 25 or 50 min (100% of 84)."*
- Facet `latency:<audience_id>` (style, applies to that audience only; old
  `reply_latency`): per side with ≥ 10 replies, the smallest edge with a cumulative share
  ≥ 0.75: *"communication/style: replies within 5 min in work hours (100% of 30
  replies)."*, *"… within 15 min in work hours, within 4 hours otherwise (…)"*, or one side.

**Test:** the 6-week synthetic calendar gives *"typically 25 or 50 min (100% of 84)"*;
30 replies after 3 minutes give the latency text above with audience `work-chat`.

## time_gaps

**Scope** category. **State:** `gap` = `<edge>` (numeric, edges 0, 5, 10, 15, 30, 60 min);
`days`. Per local day, non-reply intervals sorted by start; gap = start − latest end so far
(overlap = 0); gaps of 120 min or more are not "between" items. Gaps are computed within
one job: two intervals of one day sent in different jobs add no gap.

- For 15, 10, 5 min in that order, share of gaps ≥ that: if ≥ 0.75 →
  *"work/meetings: keeps at least 15 min between items (100% of 30 same-day gaps)."*
- Else gaps under 5 min ≥ 0.75 → *"work/meetings: usually back to back (80% of 90 gaps
  under 5 min)."*

**Test:** the synthetic calendar's 35-minute gaps give the 15-minute text; a later month
of 0- and 20-minute gaps turns the approved claim into a proposal and archives the pending
length claim.

## Not ported (no input in the episode model)

The old `accept_buffer` and `accept_size` rules needed invite responses and attendee
counts; `message_length` moves into the stylometry builtin (M7b).
