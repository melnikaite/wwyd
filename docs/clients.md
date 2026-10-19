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
breaking changes to tool shapes only bump the minor version (0.1 -> 0.2) and are listed in
`CHANGELOG.md`. Additive changes (new optional inputs, new output fields, new enum values
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
  facts?: [{ what: string, value: string | null, kind?: string }] (<= 20),
  kind?: "reply" | "decision",        // default "reply"
  include_private?: bool              // owner only
}
```

Output, one of:

```
{ type: "answer",   answer, confidence, disclosure, used_claims: [id], hints: [string] }
{ type: "needs",    needs: [{ kind, what, why }], used_claims: [id], hints }
{ type: "escalate", escalate: code, reason, hints }
```

- `answer`: for `kind: "reply"` the message text in the owner's style; for `"decision"` a
  short statement of what the owner decides. Show `disclosure` with any message you send on
  the owner's behalf.
- `escalate` codes: `low_confidence`, `out_of_persona`, `boundary`, `rate_limited`,
  `spend_cap`, `llm_unavailable`, `audience_not_permitted`. Hand the situation to the owner.

### Needs and the facts round trip

`needs` means a decision rule of the owner depends on facts the server cannot know. Each
need has:

- `kind`: `calendar.free_busy` (the owner's free/busy time, slots, other calls),
  `contact.relationship` (who the other person is to the owner) or `other`. The list may
  grow: **treat an unknown kind as `other`**.
- `what`: the fact to look up, in words, specific to the situation.
- `why`: which rule asked for it (`decision_rule clm_...`).

The server keeps no state. To continue, call `predict` again with the same `situation`,
`audience` and `kind`, plus `facts`: one entry per need, `what` copied from the need,
`value` the answer as text, `kind` optionally echoed. If you cannot find a fact out, send it
with **`value: null`**: the server then decides without it or escalates with
`low_confidence`, and never asks for that fact again. A client that keeps answering needs
therefore terminates; still cap the round trips (two is plenty).

```
-> predict {situation: "Call tomorrow at 3?", audience: "default"}
<- {type: "needs", needs: [{kind: "calendar.free_busy",
                            what: "owner's free slots tomorrow around 15:00", why: "..."}]}
-> predict {situation: "Call tomorrow at 3?", audience: "default",
            facts: [{what: "owner's free slots tomorrow around 15:00",
                     value: "14:00-16:00", kind: "calendar.free_busy"}]}
<- {type: "answer", answer: "3 works", ...}
```

Situations and facts go to the persona's LLM backend and are never stored or logged.

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
Episode = { when?: datetime | {start, end?},  // end >= start
            where?: string <= 100,            // coarse: "home", "online", "work Slack"; never an address
            audience?: string,                // audience name or "default"
            category: string,                 // required; a path, created if unknown
            situation?: string <= 8000,       // what happened / was offered / was said to the owner
            decision?: string <= 4000,        // what the owner did, in your words
            said?: string <= 20000 }          // the owner's verbatim words
```

Every episode (and `when`) has `additionalProperties: false` and needs `decision` or
`said`. Never put names, emails, phone numbers or addresses in any field, and keep names
out of category paths too. Datetimes are ISO 8601 (10-40 chars), converted to the persona's
timezone; without an offset they are read in it. An unknown `audience` maps to the default
audience and is counted in `unknown_audiences`. Validation errors name the episode index
and field path only.

Shapes:

| Shape | Fields | Example |
|---|---|---|
| statement about self | `said`, no `situation` | `{"category": "food", "said": "I never eat breakfast."}` |
| chat reply | `situation` = the incoming message(s), `said` = the reply | `{"when": {"start": "2026-10-02T10:00:00+02:00", "end": "2026-10-02T10:04:00+02:00"}, "audience": "colleagues", "category": "communication/chat", "situation": "Can you review this today?", "said": "sure, after lunch"}` |
| choice | `situation` + `decision` | `{"when": {"start": "2026-10-02T19:30:00+02:00", "end": "2026-10-02T20:00:00+02:00"}, "where": "online", "category": "work/meetings", "situation": "invite, 12 attendees", "decision": "declined"}` |
| call | `situation` = what the others said, `said` = the owner's turns | `{"when": "...", "category": "work/calls", "situation": "...", "said": "..."}` |

Only `said` counts as the owner's text (style, guard); `situation` may hold others' words
and `decision` is your description. A `decision` without `situation` is accepted (a
self-describing choice such as "watched to the end, 4/5") and goes to the decision pass.
An episode with both `said` and `decision` is a reply and a decision.

**Intervals.** An interval `when` is the episode's span: a meeting's length, or incoming
message -> reply (reply latency). Never mix the two meanings in one category (put replies
under `communication/...`, meetings under `work/meetings`). The episode's timestamp is
`when` or `when.start`.

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
counts each episode once. `failed` means a real error (do not blindly resend);
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
`unmapped_categories`, `skipped_seen` (at or below the source watermark),
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
