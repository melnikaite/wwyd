# Writing an MCP client for wwyd

This page is for authors of MCP clients and harnesses (auto-responders, voice assistants,
plugins) that call a wwyd server. It describes the parts of the contract you can rely on.
`SPEC.md` is the full design document; where the two disagree, `SPEC.md` wins and this page
is a bug.

The MCP endpoint is Streamable HTTP at `/mcp` (for example `https://wwyd.example.org/mcp`).
A local single-user server also speaks stdio (`wwyd stdio --user <login>`, owner scope, no
auth).

## Authentication

Pick one:

- **OAuth 2.1** (interactive clients). The server is its own authorization server:
  discover it through `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-authorization-server`. Register with **DCR** (`/register`) or use a
  **CIMD** client ID (an `https` URL of your client ID metadata document). PKCE with `S256`
  is mandatory. Public clients use `token_endpoint_auth_method: "none"`. Access tokens live
  1 hour; refresh tokens 30 days and rotate on every use (reusing a rotated refresh token
  revokes the whole grant, so store the new one atomically).
- **API keys** (scripts, headless harnesses). The owner creates a key on the account page;
  send it as `Authorization: Bearer wwyd_<prefix>_<secret>`.

### Scopes

| Scope | What the client can do |
|---|---|
| `owner` | every tool; the owner's own clients |
| `read` | `get_persona` only (personalising another assistant) |
| `respond:<a>,<b>` | `predict` only, and only for the listed audiences |

Request the scope in the OAuth `scope` parameter; the owner confirms it (and, for
`respond`, ticks the audiences) on the consent page. `tools/list` only shows the tools your
scope allows. `respond:` scopes name real audiences: `respond:default` is not a valid scope.
Renaming or deleting an audience revokes the grants and keys whose scope names it.

### Revocation

`POST /revoke` (RFC 7009, form-encoded):

- OAuth tokens: send `token` and your `client_id`. Public clients (`none`) need nothing
  else; confidential clients authenticate with their secret. A token is only revoked if it
  belongs to your client.
- API keys: send the key itself as `token`; no client authentication is needed.

The response is `200` whether or not something was revoked.

## Versioning

`serverInfo.version` in the MCP `initialize` result is a semantic version. Before 1.0,
every breaking change to tool shapes bumps the minor version (0.2 -> 0.3) as soon as it
lands on `main`, also between releases, so a version gate in your client catches it;
breaking changes are listed in `CHANGELOG.md`. 0.1.0 was never released: the first release
is 0.2.0. Additive changes (new optional inputs, new output fields, new enum values
where this page says the list may grow) can appear in any release: **ignore unknown fields**
and treat unknown values as documented below.

## Errors

Tool failures come back as an MCP tool result with `isError: true` and structured content
`{"error": {"code", "message", ...details}}` (for example `field` for `invalid`). Codes:
`not_found`, `forbidden`, `invalid`, `conflict`, `too_large`, `rate_limited` (carries
`retry_after` in seconds), `spend_cap`, `llm_unavailable`. Messages are for humans; branch on
`code`.

`predict` is different: it does not fail for model or limit problems, it answers with
`type: "escalate"` and a code (below).

## Audiences and the `default` alias

An audience is a group of people the owner talks to (`work`, `family`, ...). Names match
`[a-z0-9][a-z0-9_-]{0,39}`. `list_audiences` (owner) returns
`{audiences: [{name, description, is_default, claim_count}]}`.

Every persona has exactly one default audience. Its name is the owner's choice (`everyone`
initially), so do not hard-code it: pass **`default`** wherever a tool takes an audience
name and the server uses the default audience, whatever it is called. No audience can be
named `default`. An unknown audience name in `predict` also falls back to the default
audience, but then `hints` says so; `default` itself produces no hint.

## `predict` (scopes `owner`, `respond`)

Input:

```
{
  situation: string (1..8000),        // the message or situation as the owner would see it
  audience: string,                   // audience name, or "default"
  context?: string (<= 2000),         // what you found out, in plain words; never stored
  kind?: "reply" | "decision",        // default "reply"
  include_private?: bool              // owner only
}
```

Output, one of:

```
{ type: "answer",   answer, depends_on: string | null, confidence, disclosure, used_claims: [id],
                    prediction_id, hints: [string] }
{ type: "escalate", escalate: code, reason, prediction_id, hints }
```

`prediction_id` (`adt_...`) is the ID of the call's audit row; keep it if the owner may
later say what they would actually have done (see corrections under `ingest_raw`).

- `answer`: for `kind: "reply"` the message text in the owner's style; for `"decision"` a
  short statement of what the owner decides. Show `disclosure` with any message you send on
  the owner's behalf.
- `escalate` codes: `low_confidence`, `out_of_persona`, `boundary`, `rate_limited`,
  `spend_cap`, `llm_unavailable`, `audience_not_permitted`. Hand the situation to the owner.
  `boundary` means a topic the owner does not let anyone answer for them (for example
  salary); a rule such as "no calls on weekends" is answered with the decline instead.
  `out_of_persona` also means the persona has nothing on the topic: when no claim other
  than style covers the situation (a favour, a plan or an opinion the owner never
  expressed), wwyd does not guess; ask the owner or answer it yourself.
- Language: the answer is in the situation's language unless a claim says which language
  the owner uses.
- Dates and times: the server tells the model the current weekday, date and time in the
  persona's timezone, so "tomorrow" or "on Saturday" in `situation` and `context` resolve
  correctly. Set the timezone with `update_settings` (`timezone`, an IANA name; default
  UTC).

### `depends_on` and `context`

wwyd answers like someone who knows the owner well but cannot see their calendar, the
weather or anything else outside the persona. `answer` is always ready to send in the
owner's voice and never asserts what wwyd cannot know: when the owner's real answer
depends on such things it stays non-committal ("let me check and get back to you").
`depends_on` then says, in one or two plain sentences from the owner's claims and in the
answer's language, what the answer depends on; it is `null` when nothing outside the
persona matters. `depends_on` is `null` when the owner's claims put no condition on the
answer: wwyd models the person, not the world, and never adds conditions of its own such
as free time. So a `null` `depends_on` does not mean your facts agree: if you can see the
owner's calendar (or other facts), check them before sending `answer`, or call `predict`
again with what you found in `context`. While `depends_on` is not `null`, `answer` neither accepts nor
declines (the server re-asks once and otherwise escalates `low_confidence`). It has no structure and no fixed vocabulary, is not styled, and is at
most 400 characters. A non-empty `depends_on` is not an escalation.

`context` is taken as a checked fact the owner goes by, even when no claim mentions it.
A `context` saying the asked time is taken ("tomorrow 15:00–15:30 is busy") gives a final
decline with `depends_on: null`; one that names the free slot instead ("within 15:00–17:00
tomorrow only 16:30–17:00 is free") gives a counter-offer: a decline of the asked time
that suggests the free one.

What to do with it is up to you:

- send `answer` as is (simple auto-responders);
- refine the answer yourself from `depends_on` with your own LLM and tools;
- or look things up and call `predict` again with the same `situation`, `audience` and
  `kind`, plus `context`: what you found out, as a conclusion in plain words, never raw
  data such as a whole schedule. Best is the direct answer to the condition in
  `depends_on` ("14:55–15:25 tomorrow is free", "rain all day on Saturday"): small local
  models settle that reliably, while working the condition out from a wider window ("free
  14:00–16:00") they may get wrong or leave open. The answer then comes in the owner's
  wording. The server keeps no state between calls.

```
-> predict {situation: "созвонимся завтра в 15:00?", audience: "default"}
<- {type: "answer", answer: "гляну календарь и отпишусь",
    depends_on: "Согласится, если завтра свободно с 14:55 до 15:25; иначе предложит
                 ближайшее время с тем же запасом.", ...}
-> predict {situation: "созвонимся?", audience: "default"}
<- {type: "answer", answer: "давай, скину время чуть позже",
    depends_on: "С друзьями созванивается после 18:00 и оставляет минимум 5 минут между звонками.", ...}
-> predict {situation: "пойдём на озеро?", audience: "default"}
<- {type: "answer", answer: "звучит здорово, посмотрю по погоде",
    depends_on: "Ходит на озеро только в тёплую погоду без дождя.", ...}
-> predict {situation: "созвонимся завтра в 15:00?", audience: "default",
            context: "завтра с 14:55 до 15:25 свободно"}
<- {type: "answer", answer: "давай, в 15:00 норм", depends_on: null, ...}
```

Situations and context go to the persona's LLM backend and are never stored or logged.

### Corrections

When an owner client learns that the owner would have answered or decided differently,
send a correction episode to `ingest_raw`: the same `situation` the prediction was made
for, the owner's version in `said` (reply) or `decision`, and `corrects: <prediction_id>`.
It is derived like any episode of its shape and weighs more. An unknown or expired
`prediction_id` (older than the 90-day audit retention, or from another persona) is
ignored, counted in `unknown_predictions`, and the episode counts as an ordinary one.

```
{"situation": "Call tomorrow at 3?", "said": "can't tomorrow, Thursday?",
 "audience": "default", "corrects": "adt_..."}
```

## `add_claims` (owner)

```
in:  { claims: [{ kind, category, text, audiences?: "all" | [name], visibility?: "shared" | "owner_only",
                  confidence?, evidence?, proposed_category?, status?: "pending" | "approved" }] (<= 200),
       absorb?: bool = true }
out: { created: [id], absorbed: [{ index, into }],
       dropped: [{ index, reason: "guard" | "previously_rejected" | "unknown_audience" | "invalid", detail? }] }
```

Claims land as `pending` for the owner's review; `status: "approved"` is honoured for owner
callers and makes them usable by `predict` at once. `category` must be an existing path:
read them with `list_categories` and create new ones with `upsert_category`. `audiences`
takes names from `list_audiences` (or `default`). Text must not contain emails, phone
numbers, URLs or handles.

## `list_categories` (owner)

Returns `{categories: [...]}`, the category tree (paths such as `work/meetings`, depth <= 3)
with descriptions, `claim_count` (claims directly in the node) and `total_claim_count`
(including sub-categories), and `aliases`: old paths that still route here.
`upsert_category {path, description?, rename_to?}` creates or renames one; a rename leaves
every old path of the moved subtree as an alias, so episodes sent with the old path keep
landing in the right place. `merge_categories {from, into}` moves the claims, analyzers and
analyzer state of `from` (with sub-categories) into `into`, and the `from` paths become
aliases. Creating a real category at an alias path drops the alias.

The nightly run (and `compact` with `steps: ["category_merge", "category_split"]`) keeps
the tree tidy on its own: it merges synonym categories created since the previous night
(`films` / `movies` / `кино`; an episode-created category always merges into a seeded or
owner-created one, never the reverse) and splits a category with more than 20 claims into
2-5 sub-categories. Both apply automatically, one change each in `list_changes`
(`op: category_merge | category_split`); `undo_change` restores the categories, aliases,
claims and analyzers. `compact` with `apply: false` makes no LLM call and lists the
candidates in `category_pending`.

## `ingest_raw` (owner)

Sends raw material for background derivation; returns at once, poll `job_status
{job_id}`. Raw episodes are never stored. `{episodes, source?, llm_for_ingest?}`: 1-1000
episodes, <= 2 MB serialized. One shape covers every data source; the domain lives in
`category`:

```
Episode = { when?: datetime | {start, end?},  // when the owner acted: a moment or a span; end >= start
            situation_at?: datetime,          // when the situation reached the owner; needs situation
            where?: string <= 100,            // coarse: "home", "online", "work Slack"; never an address
            audience?: string,                // audience name or "default"
            category?: string,                // a path, created if unknown; placed by the server if absent
            situation?: string <= 8000,       // what happened / was offered / was said to the owner
            decision?: string <= 4000,        // what the owner did, in your words
            said?: string <= 20000,           // the owner's verbatim words
            corrects?: string }               // a prediction_id: this is what the owner actually does
```

Every episode (and `when`) has `additionalProperties: false` and needs `decision` or
`said`; `situation_at` and `corrects` need `situation`. Never put names, emails, phone
numbers or addresses in any field, and keep names out of category paths too. Datetimes are
ISO 8601 (10-40 chars), converted to the persona's timezone; without an offset they are
read in it. An unknown `audience` maps to the default
audience and is counted in `unknown_audiences`. Validation errors name the episode index
and field path only.

Shapes (the `category` in the examples is optional):

| Shape | Fields | Example |
|---|---|---|
| outgoing message the owner started | `said` + `audience`, no `situation` (counts as a reply to that audience) | `{"audience": "colleagues", "said": "running 15 min late, sorry"}` |
| statement about self | `said`, no `situation`, no `audience` | `{"category": "food", "said": "I never eat breakfast."}` |
| chat reply | `situation` = the incoming message(s), `said` = the reply | `{"situation_at": "2026-10-02T10:00:00+02:00", "when": "2026-10-02T10:04:00+02:00", "audience": "colleagues", "category": "communication/chat", "situation": "Can you review this today?", "said": "sure, after lunch"}` |
| choice | `situation` + `decision` | `{"when": {"start": "2026-10-02T19:30:00+02:00", "end": "2026-10-02T20:00:00+02:00"}, "where": "online", "category": "work/meetings", "situation": "invite, 12 attendees", "decision": "declined"}` |
| call | `situation` = what the others said, `said` = the owner's turns | `{"when": "...", "category": "work/calls", "situation": "...", "said": "..."}` |

Only `said` counts as the owner's text (style, guard); `situation` may hold others' words
and `decision` is your description. A `decision` without `situation` is accepted (a
self-describing choice such as "watched to the end, 4/5") and goes to the decision pass.
An episode with both `said` and `decision` is a reply and a decision.

**Text only.** Episodes carry text only: describe images, audio and other media in words
(a voice message becomes its transcript in `said`, a photo a short description in
`situation`).

**Time.** Each field has one meaning: `when` is when the owner acted, a moment (reply sent,
choice made) or a span (a meeting attended, a call); `situation_at` is when the situation
reached the owner (message received, invitation sent). Spans feed lengths and gaps;
`when` - `situation_at` is the response latency, in any category. Do not encode latency
as a `when` span. The episode's timestamp is `when` or `when.start`.

**Placement.** `category` is optional. Without it the server places the episode in an
existing path, an alias or a new organic category (one batched LLM step). An episode the
server cannot place as proposed is counted in `unplaced_episodes`: without an LLM step
(`llm_for_ingest: false`) or without a usable path in the placement output it goes to
`other`; a proposed new path that fails the guard goes to its deepest existing ancestor,
else `other`. A placed path over the category limits counts in `unmapped_categories`
instead. Paths you send are never re-placed.

**Categories.** Paths are normalised (NFKC, lower case, spaces -> `-`; segments of Unicode
letters, digits, `-`, `_`, <= 40 chars, <= 3 levels), so `Кино/Сериалы` is fine. An unknown
path is created with its missing parents, and one batched LLM call writes one-line
descriptions for the new ones (none with `llm_for_ingest: false`). Limits:
`max_categories` (200 per persona, seeds included) and `new_categories_per_job` (20).
Beyond either, an episode goes to the deepest existing ancestor of its path, or to
`other`, and is counted in `unmapped_categories`. Old paths of merged or renamed
categories keep routing to the new one. Each job's creations are one `category_create`
change, undoable with `undo_change` while the categories hold no claims.

The call returns `{job_id, accepted, estimated_tokens, remaining_tokens}`. `accepted` is
the number of valid episodes received. `estimated_tokens` is a conservative estimate of the
LLM tokens the job will spend (0 when no LLM step runs); the job stops before a call whose
estimate does not fit, so the daily cap can be overrun by at most one call's actual excess.
`remaining_tokens` is what is left of today's `llm_tokens_per_day` (`null` when unlimited, 0 when the spend cap is reached). The
daily byte limit (20 MB by default) is far above what 500 000 tokens a day can derive (a
2 MB batch of replies is roughly 1M tokens), so size batches so that `estimated_tokens` fits
`remaining_tokens`. Decisions in a category with >= 100 episodes in one batch are left to
the analyzers rather than sent to the LLM.

**Partial jobs and resending.** `job_status` returns `status: queued | running | done |
partial | failed | interrupted`. A job stopped by the daily token limit (`error_code:
rate_limited`), the spend cap (`spend_cap`) or an unavailable LLM (`llm_unavailable`) ends
`partial`: everything derived so far is stored, and `remaining_records` lists the indices
(into the batch you sent) that were not processed. Keep the batch until the job ends, then
resend exactly those episodes, after `retry_after` seconds when it is given (daily reset or
month end):

```
out = ingest_raw({episodes, source})
loop: s = job_status({job_id})  until s.status not in (queued, running)
if s.status == "partial":
    wait s.retry_after (if any); ingest_raw({episodes: [episodes[i] for i in s.remaining_records], source})
```

Analyzers run last, over the processed episodes only, so resending `remaining_records`
counts each episode once.

The server retries a single LLM call once on a transient error (connection refused, HTTP
502/503/504, a read timeout during ingest) before it stops the job; jobs themselves are
never retried by the server. `llm_unavailable` has no `retry_after`: resend
`remaining_records` with a growing pause (e.g. 30 s, 60 s, 120 s), then give up and tell
the owner. The usual cause is a busy single-slot local backend (e.g. LocalAI answering
another request); raising its parallelism or ingesting smaller batches helps.

`failed` means a real error (do not blindly resend);
`interrupted` (server restart) means resend the whole batch.

**Incremental sync with `source`.** Pass an owner-chosen slug (`[a-z0-9][a-z0-9_-]{0,39}`,
e.g. `telegram`, `gcal`). The server keeps per source the largest processed episode
timestamp; episodes with a timestamp at or below it are skipped. Episodes without a
timestamp are never skipped. After a `partial` job the watermark stays below the earliest
remaining episode, so resending `remaining_records` is not skipped. `list_sources` returns
`{sources: [{source, last_ts, last_job_at, records_total}]}`; `reset_source {source}`
forgets one watermark (for a re-export from scratch). Send episodes in chronological order
where you can.

**Counters** in `job_status.counts`: `episodes` (valid episodes received: `total`,
`statement`, `reply`, `decision`; one episode can be in two classes), `categories_created`,
`unmapped_categories`, `unplaced_episodes` (uncategorised episodes not placed as proposed: no LLM step, no
usable path, or a new path that failed the guard; see Placement),
`corrections` (episodes whose `corrects` matched a prediction), `unknown_predictions`
(`corrects` IDs ignored as unknown or expired), `skipped_seen` (at or below the source watermark),
`skipped_saturated` (replies not sent to the LLM because the audience was saturated: 5
reply calls in a row produced only claims the persona already has), `saturated_audiences`,
`claims_created`, `claims_updated`, `absorbed`, `dropped_by_guard`,
`dropped_previously_rejected`, `dropped_invalid`, `unknown_audiences`, `llm_calls`,
`llm_calls_failed`, the analyzer counters `analyzers_run`, `analyzer_errors`,
`analyzer_keys_dropped` (state keys refused by the key filter), `analyzers_skipped_disabled`,
`analyzer_claims_created` / `_updated` / `_weakened` / `_proposed`. Annotation, LLM analyzers and usage
notes add: `annotation_calls` (of `llm_calls`), `labels_created` / `labels_dropped` (new
label or tag names stored / refused over the per-category limit), `analyzers_proposed`
(hypotheses the model returned), `analyzers_accepted`, `analyzers_rejected` (stored
`retired`), `analyzers_duplicate` (restated an existing or retired analyzer: dropped),
`analyzers_skipped_limit` (`max_analyzers` reached), `analyzers_disabled` (3 failed runs
in a row), `sandbox_errors` (failed or invalid sandbox runs, tests included),
`skipped_sandbox_budget` (analyzer runs or hypothesis tests skipped because today's
`sandbox_cpu_seconds_per_day` was spent; the job still ends `done`),
`sandbox_unavailable` (sandbox assets not built on this server: LLM analyzers and authoring
skipped), `word_usage_notes`, `skipped_llm_budget` (an authoring or usage-note call did not
fit the token budget and was skipped; these calls cover no episodes, so the job is not
`partial`), `authoring_failed` / `word_usage_failed` (the backend failed on such a call).

## Analyzers (owner)

Analyzers turn processed episodes into counters (never text) and render keyed claims from
them: builtins (`time_activity`, `time_duration`, `time_gaps`, `stylometry`, word-usage
notes; see `docs/habits.md`) and LLM-written ones that passed the hypothesis test.
`list_analyzers {scope?, status?, limit?, cursor?}` lists them (`scope` is a category path
or an audience name); `get_analyzer {id}` adds the statement template, the source of an LLM
analyzer and a summary of its state (metrics, months, keys); `update_analyzer {id, status}`
sets `active`, `disabled` or (LLM analyzers only, final) `retired`, which drops the source
and keeps the statement so the hypothesis is not proposed again. Their claims start
`pending` like every derived claim.
