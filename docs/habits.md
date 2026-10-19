# Habit detectors

Habits nobody puts into words are computed in code from `ingest_raw` batches (SPEC §7.2).
Code: `src/wwyd/ingest/detectors/`. Tests with synthetic calendars and message streams:
`tests/test_detectors.py`.

## How they work

Each detector is two pure functions:

- `collect(batch, tz)` turns one batch into **fixed-size statistics** (histograms and
  counters). Records are never kept.
- `render(stats, period_days)` turns statistics into the claim text, `share` and
  `confidence`, or returns nothing below threshold.

The statistics are stored in the claim's `evidence.stats`. A later batch adds its buckets
to the stored ones, then the claim is re-rendered from the merged numbers. A re-run
therefore updates the keyed claim instead of creating a duplicate (`src/wwyd/ingest/keyed.py`).

**Thresholds** (constants in `detectors/common.py`):

- at least **30 samples**;
- a **share of at least 0.75** for any rule-like statement;
- a **period of at least 3 weeks** (21 days, from the first to the last local date seen).

**Time handling.** Every timestamp keeps its UTC offset. A value without an offset is read
in the persona's timezone. Durations and gaps are computed on aware datetimes, so a DST
switch never adds or removes an hour. Day, weekday and hour buckets use the persona's
timezone.

**Event filters.** An event is *attended* when `response` is `accepted` or `organizer`. It
is an *invite* when `response` is `accepted`, `tentative` or `declined`. Events of 0
minutes or longer than 8 hours (all-day blocks) are ignored.

**Confidence** is `min(0.95, 0.35 + 0.6 × share × min(1, samples / 150))`.

**Merging rules for keyed claims:**

- **Pending claim:** text, numbers and evidence are all refreshed.
- **Approved claim:** evidence and confidence are refreshed. The text is re-rendered only
  while `origin` is still `derived`; once the owner edits the text, it is kept. The claim
  stays approved.
- **Rejected claim:** never touched and never resurrected. The finding is dropped and
  counted as `dropped_previously_rejected`. Archiving the claim frees the key.
- **Below threshold:** with no live claim, nothing is stored and that batch's statistics
  are discarded. With a live claim, its evidence is updated and the text is kept.

Category: the one listed below. If the owner deleted it, the nearest existing parent is
used; if none exists, the finding is skipped.

## `habit:meeting_buffer`

| | |
|---|---|
| Kind / category | `habit` / `work/meetings` |
| Input | attended events, grouped by local start date and sorted by start |
| Statistic | `gap_minutes_hist`: gaps between an event's start and the latest end so far that day, in buckets `[0,5) [5,10) [10,15) [15,30) [30,60) [60,120)`. Overlaps count as 0; gaps of 120 min or more are not "between calls" and are skipped. |
| Formula | for T in 15, 10, 5: share = gaps ≥ T / all gaps. The first T with share ≥ 0.75 wins. Otherwise, if gaps under 5 min ≥ 0.75: "back to back". |
| Example | "Keeps at least 10 min between calls (100% of 84 same-day gaps)." |

## `habit:meeting_length`

| | |
|---|---|
| Kind / category | `habit` / `work/meetings` |
| Statistic | `duration_5min_hist`: duration rounded to 5 min, 37 buckets (0 to 180+) |
| Formula | top bucket share ≥ 0.75 → one length; otherwise top two buckets ≥ 0.75 → two lengths |
| Example | "Meetings are typically 25 or 50 min (100% of 138)." |

## `habit:meeting_free_slots`

| | |
|---|---|
| Kind / category | `habit` / `work/meetings` |
| Statistic | `weekday_halfday_hist`: attended events by local weekday × half-day (morning before 13:00, afternoon from 13:00), 14 buckets |
| Formula | mean = average of the 10 Mon–Fri slots (must be ≥ 3). A slot is free when its count ≤ 0.25 × mean, i.e. share = 1 − count/mean ≥ 0.75. Up to two free slots are reported, emptiest first. |
| Example | "No meetings on Friday afternoons." / "Rarely has meetings on Monday mornings (2 of 120 meetings)." |

## `rule:accept_buffer`

| | |
|---|---|
| Kind / category | `decision_rule` / `work/meetings` |
| Input | invites with at least one other attended event the same local day |
| Statistic | gap to the nearest attended event that day (overlap = 0), in buckets `[0,5) [5,10) [10,15) [15,30) [30,∞)`, split into `declined_gap_hist` and `other_gap_hist` |
| Formula | for T in 15, 10, 5: tight = invites with gap < T. The rule holds when tight declines / tight ≥ 0.75, at least 10 invites on each side, and declines among the other invites ≤ 0.25. The first T that holds wins. |
| Example | "Declines invites that leave less than 10 min of buffer (100% of 30 such invites)." |

## `rule:accept_size`

| | |
|---|---|
| Kind / category | `decision_rule` / `work/meetings` |
| Statistic | `declined_by_size`, `other_by_size`: invites by attendee count `≤2, 3–4, 5–8, 9–15, 16+` |
| Formula | for S in 2, 4, 8, 15: big = invites with more than S attendees. The rule holds when big declines / big ≥ 0.75, at least 10 invites on each side, and declines among the smaller ones ≤ 0.25. The smallest such S wins. |
| Example | "Usually declines meetings with more than 8 attendees (100% of 24)." |

## `habit:active_hours`

| | |
|---|---|
| Kind / category | `habit` / `schedule/routine` |
| Input | owner message timestamps and attended event starts |
| Statistic | `weekday_halfhour_hist`: 7 × 48 half-hour buckets in local time |
| Formula | Over Mon–Fri combined (≥ 30 samples), drop the earliest and the latest 10% of activity. The remaining window (≥ 0.75 of activity) is the active window. A day is "rare" when its activity is ≤ 15% of the average weekday's. |
| Example | "Active 09:00–15:30 on weekdays, rarely on Saturday and Sunday (88% of 210 weekday events and messages)." |

## `habit:reply_latency:<audience>`

| | |
|---|---|
| Kind / category | `style` / `communication/style`, audiences `[<audience>]` |
| Input | messages of one audience, grouped by the batch-local `thread` label and sorted by `ts`, then `position`. The first unanswered non-owner message is paired with the next owner message. |
| Statistic | latency in buckets `[0,5) [5,15) [15,60) [60,240) [240,1440) [1440,∞)` minutes, split into `in_hours_hist` (the non-owner message arrived Mon–Fri 09:00–18:00 local) and `off_hours_hist` |
| Formula | Each side needs ≥ 10 replies; together they need ≥ 30. A side's bound is the smallest bucket edge E with replies < E ≥ 0.75. |
| Example | "Replies to work-slack within 5 min in work hours, within 4 hours otherwise (81% of 64 replies)." |

## `habit:message_length:<audience>`

| | |
|---|---|
| Kind / category | `style` / `communication/style`, audiences `[<audience>]` |
| Statistic | `chars_hist`: owner message length in buckets `[0,40) [40,100) [100,200) [200,400) [400,800) [800,∞)`. `lines_hist`: non-empty lines `1, 2, 3–5, 6+`. |
| Formula | The smallest edge E with messages shorter than E ≥ 0.75 gives the length. "usually one line" is added when 1-line messages ≥ 0.75; "usually 1–2 lines" when 1–2-line messages ≥ 0.75. |
| Example | "Writes to family in messages under 40 characters, usually one line (92% of 120 messages)." |

Unknown audiences in a batch are mapped to the default audience, so their messages count
toward the default audience's keys.
