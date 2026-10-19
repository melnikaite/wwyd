# wwyd — "what would you do"

Status: **approved** design (2026-10-18). Key decisions and their reasoning are in §17.
Implementation progress is tracked in §16 and CHANGELOG.md.

wwyd is an MCP server that holds one person's character and ways of acting: how they
think and decide, their habits, and how they talk to different kinds of people. Any AI
harness can use it over MCP for two things:

1. **Personalise** the harness to its owner (`get_persona`).
2. **Answer "how would the owner reply or act here?"** (`predict`), e.g. for an
   out-of-office auto-responder or for reply drafts.

The project ends at the answer. Deciding whether to reply and to whom, classifying
contacts, transcribing calls, sending messages and reading calendars are the harness's job.

**Guiding principle: the lowest possible barrier to entry.** One process, one SQLite file,
one command to start. No integrations, importers, vector stores or storage choices. When in
doubt, leave it out and list it under [Later options](#15-later-options).

---

## 1. Goals and non-goals

### Goals
- Self-hosted on the owner's machine or VM. The data stays under the owner's control.
- Multi-user from day one: a family or a team shares one server, each user with an
  isolated persona. A paid hosted version is the intended business model, so limits and
  usage counters exist from the start (billing itself does not).
- Works with any MCP client that supports tools and OAuth (Claude, ChatGPT, Claude Code,
  OpenClaw, Hermes, custom harnesses), plus API keys for scripts that cannot do OAuth.
- The store stays small by itself (compaction), so a persona fits a small prompt budget.

### Non-goals
- Messenger plugins, importers, contact classification, transcription, sending anything.
- Storing raw conversations, contact identities, or live data (calendar contents, mail,
  documents).
- A general-purpose memory or RAG store. Embeddings and vector stores are out.
- A full identity provider. Auth exists only to protect this server.
- Billing.

---

## 2. Privacy invariants

These are the rules the whole design is checked against. Each one has a test (§13).

| # | Invariant | How it is enforced |
|---|---|---|
| P1 | **Raw input is never persisted.** `ingest_raw` records live only in process memory until the job finishes or fails. They never reach the DB, logs, error reports, exports or the change log. | Raw records are held only by the job object; no SQL table has a column for them. Logging filter drops tool arguments at every level; tracebacks are logged without locals or argument reprs; the SDK's request logging is disabled. `PRAGMA secure_delete=ON`. Privacy canary test. |
| P2 | **No contact identities.** No names, phone numbers, emails, messenger IDs or hashes of them. | Record schemas have no such fields (`additionalProperties: false`). Audiences are owner-defined names. Derived claims pass the **verbatim guard** (§7.4) before they are stored. |
| P3 | **No live data.** Calendar events carry times, attendee counts and the owner's response, never titles, attendees or locations. | Record schemas reject unknown fields. Facts passed to `predict` are used for that call only and never stored. |
| P4 | **Third parties see only what the owner allowed.** | Claim filtering happens in SQL before the LLM call (§8.3). The prompt is never relied on to hide anything. |
| P5 | **Answers to third parties are disclosed as automated.** | `predict` always returns a server-built `disclosure` string (EU AI Act Art. 50). |
| P6 | **Audit without content.** Every `predict` call is logged with time, client, audience, outcome and confidence, never the situation text, facts or answer. | Audit table has no text columns. |

One flow leaves the server on purpose: **raw input and `predict` situations are sent to the
LLM backend the persona is configured with.** With a local backend (LocalAI, Ollama,
LM Studio) nothing leaves the machine. With OpenRouter or the instance's shared default the
provider sees that text. The settings tool and the account page state this plainly, and a
persona can switch `ingest_raw` to code-only derivation (`llm_for_ingest: false`).

---

## 3. Architecture

```
                   ┌───────────────────────── one process (uvicorn) ─────────────────────────┐
 MCP client ──────▶│ /mcp  Streamable HTTP ── RequireAuth ── MCPServer tools                  │
 (Claude, ChatGPT, │ /authorize /token /register /revoke /.well-known/*   (SDK auth routes)  │
  harness, script) │ /login /consent /account /admin /setup /invite /reset   (4 HTML pages)  │
                   │                                                                         │
                   │ store (SQL)   predict   ingest jobs   compaction   limits   LLM client  │
                   │      │                     ▲  raw records live only here (memory)      │
                   └──────┼─────────────────────┼────────────────────────────────────────────┘
                          ▼                     │
                 ~/.wwyd/wwyd.db (SQLite, WAL)   └── nightly scheduler (asyncio task)
```

- **Stack:** Python ≥ 3.12, uv, FastAPI, official MCP Python SDK `mcp>=2.3,<3`
  (`mcp.server.MCPServer`, formerly FastMCP), SQLite in WAL mode with plain SQL migrations,
  `argon2-cffi`, `httpx` for the OpenAI-compatible LLM client, `cryptography` (Fernet) to
  encrypt LLM API keys at rest, Jinja2 for the four pages.
- **Transports:** Streamable HTTP at `/mcp` (multi-user, OAuth or API key) and stdio
  (`wwyd stdio --user <login>`: local, single user, owner scope, no OAuth, per the MCP
  spec's guidance for stdio). Limits still apply on stdio. If the login does not exist yet,
  stdio creates it without a password (the first user becomes admin) and says so on
  stderr; `wwyd reset-password <login>` gives it a password later. Anyone who can run
  commands on the host can already read the DB file, so this grants nothing new.
- **Mounting:** our FastAPI app owns the lifespan and runs `mcp.session_manager.run()`
  inside it (a mounted sub-app's lifespan never runs). `transport_security` is configured
  from `public_url`, otherwise every request behind a real hostname gets HTTP 421.
- **Single process by design.** In-process rate-limit buckets, the job queue and the LLM
  semaphore all rely on it. Horizontal scaling is a later option.
- **Data dir:** `~/.wwyd/` by default (`/data` in the container), override with
  `WWYD_DATA_DIR`. It holds `wwyd.db`, `secret.key` (Fernet key for stored backend API
  keys, generated atomically on first use) and optional `wwyd.toml`. The key can instead
  come from `WWYD_SECRET_KEY` (the key itself) or `WWYD_SECRET_KEY_FILE` (a path outside
  the data dir); otherwise a backup of the data dir contains the key next to the values it
  protects. An empty or corrupt key is a startup error.
- **Config:** built-in defaults → `wwyd.toml` → `WWYD_*` env vars. The important keys are
  `public_url` (OAuth issuer; required for anything but localhost), `host`/`port` (default
  `127.0.0.1:8740`), `trusted_proxies` (peers whose `X-Forwarded-For` is trusted for the
  client IP; empty by default — behind a reverse proxy on the same host, e.g. Caddy, set it
  to the proxy's address such as `127.0.0.1`, or every client shares the proxy's IP and the
  per-IP auth throttles act globally; the server logs a one-time warning when it sees
  `X-Forwarded-For` from a loopback/private peer with `trusted_proxies` empty), the instance default LLM (`[llm]`, including
  `allow_private_base_urls`, see §12), default limits
  (`[limits]`), and optional SMTP.

### Source of truth per subsystem

| Subsystem | Source of truth | Derived from it |
|---|---|---|
| Persona content | `claims` rows (+ `claim_audiences`) | FTS index, `get_persona` text, exports, predict context |
| Habit statistics | per-claim sufficient statistics in `evidence.stats` (fixed-size histograms) | the claim's numbers and wording |
| Consolidation history | `changes` rows (with before/after snapshots of the claims touched) | undo, proposals |
| Access | `grants`, `api_keys` (hashed) | access/refresh tokens; every request re-checks the DB |
| Limits | admin ceilings + user overrides + config defaults | effective limit = min of the three |
| Usage | `usage_daily` counters | rate-limit decisions, spend cap, future billing |

---

## 4. Data model

IDs are random and kind-prefixed (`usr_`, `aud_`, `cat_`, `clm_`, `chg_`, `job_`, `key_`,
`grt_`), following MacPaw Portable Memory. Timestamps are ISO-8601 UTC with `Z`.
Field names are snake_case everywhere (SQL, MCP tools, export); see D3.

### 4.1 user
`id, login (email, unique), password_hash (argon2id), is_admin, disabled_at, created_at,
password_changed_at`. One persona per user, created with the user.

### 4.2 persona
`id, user_id (unique), display_name` (used in the disclosure), `timezone` (IANA, used by
detectors and the nightly pass), `settings` (JSON: LLM backend, `llm_for_ingest`,
`escalate_below`, `disclosure_template`, own limit overrides, budget), `spend_capped_at`,
`created_at`.

### 4.3 audience
`id, persona_id, name, description, is_default, created_at, updated_at`.
- `name` is a slug (`[a-z0-9][a-z0-9_-]{0,39}`) because it appears in scopes
  (`respond:family,work-slack`) and in harness configuration.
- `description` is the owner's free text: "colleagues in the work Slack, peers, informal,
  Russian/English". There are no fixed dimensions.
- Exactly one audience per persona has `is_default = true`. A new persona starts with one
  default audience, `everyone` ("anyone not in another group"), which the owner can rename.
- Only owner-scoped calls create, change or delete audiences.

### 4.4 category
`id, persona_id, parent_id, name, description, path` (materialised, e.g.
`communication/style`), at most 3 levels. Seeded per persona:

```
communication/style        communication/boundaries
work/meetings              work/decisions
schedule/routine
food  travel  money  health  family  hobbies  tech
```

Each seeded category has a one-line description, because the routing LLM sees only paths
and descriptions. Derivation must reuse existing categories. It may *propose* a new one by
putting `proposed_category: "<path>"` on a pending claim (stored under the closest existing
parent); approving the claim with `create_category: true` creates it.

### 4.5 claim

| Field | Type | Notes |
|---|---|---|
| `id` | `clm_…` | |
| `kind` | `trait \| preference \| habit \| decision_rule \| style \| boundary` | |
| `category` | path | |
| `text` | string ≤ 500 chars | one statement, owner in third person or neutral ("Short, no greeting…") |
| `audiences` | `"all"` or list of audience names | where the claim **applies** |
| `visibility` | `owner_only \| shared` | who may **see** it through `predict`: `shared` claims reach the audiences they apply to; import maps `audiences` and `anyone` to `shared` (D1) |
| `status` | `pending \| approved \| rejected \| archived` | `archived` is added for compaction |
| `confidence` | 0..1 | |
| `evidence` | object | aggregate only, see below |
| `key` | string, nullable | stable key of a code detector, e.g. `habit:meeting_buffer`; unique per persona among non-archived claims |
| `origin` | `derived \| added \| imported \| owner` | |
| `proposed_category` | path, nullable | |
| `superseded_by` | `clm_…`, nullable | set on the loser of a contradiction |
| `archive_reason` | `merged \| superseded \| decayed \| budget \| owner`, nullable | |
| `created_at, updated_at, reviewed_at, archived_at, last_evidence_at` | timestamps | `last_evidence_at` drives decay |

`evidence` never grows as a list:

```json
{
  "sources": {"calendar_event": 212, "message": 0, "decision": 3, "note": 0},
  "share": 0.87,
  "period": "2026-04..2026-10",
  "stats": {"gap_minutes_hist": [0, 4, 31, 120, 57]}
}
```

`sources` are counters that are summed on merge; `period` widens; `stats` holds the
detector's fixed-size sufficient statistics (histogram buckets, counts), so two runs merge
by adding buckets and recomputing the numbers. LLM-derived claims have `sources` and
`period` only.

Examples:
- style, audiences `[work-slack]`: "Short, no greeting, answers within minutes during work
  hours."
- decision_rule, audiences `all`: "Accepts a meeting only if there is a free slot of at
  least 20 minutes and 10 minutes of buffer before and after other calls."
- habit, key `habit:meeting_free_slots`: "No meetings on Friday afternoons."
- boundary: "Never discusses salary in writing."

**Rejected claims are kept as negative memory**: a new derivation that matches a rejected
claim is dropped instead of re-entering the review queue. They are text-only (evidence
cleared) and count against no budget.

### 4.6 Other tables
- `changes`: `id, persona_id, ts, op (merge|absorb|supersede|decay|archive_budget|purge|
  restore|category_merge|category_rename), state (applied|proposed|dismissed|undone|
  expired), reason, job_id, payload` (before/after snapshots of the claims touched; these
  are derived claims, never raw input). Purge turns the entries for that claim into
  `expired` (not undoable).
- `jobs`: `id, persona_id, status (queued|running|done|failed|interrupted), created_at,
  finished_at, counts` (records by type, claims created/absorbed/dropped_by_guard),
  `error_code`. No content.
- `audit`: `id, persona_id, ts, client_name, grant_id|api_key_id, scope, audience_id,
  audience_fallback (bool), outcome (answer|needs|escalate:<code>), confidence,
  llm_tokens, latency_ms`. Kept 90 days.
- Auth tables: `web_sessions, one_time_links, oauth_clients, pending_authorizations,
  oauth_codes, grants, tokens, api_keys` (§9).
- Limits/usage: `limit_overrides (user_id, set_by admin|user, json)`,
  `usage_daily (user_id, day, metric, value)`.
- `claims_fts`: FTS5 external-content table over `claims.text`, kept in sync by triggers,
  `unicode61 remove_diacritics 2` tokenizer.

### 4.7 Export format: `schema/persona.v1.json`

A versioned JSON Schema (draft 2020-12). One JSON document:

```json
{
  "format": "wwyd.persona",
  "version": 1,
  "exported_at": "2026-10-18T20:00:00Z",
  "persona": {"display_name": "…", "timezone": "Europe/Berlin"},
  "audiences": [{"name": "family", "description": "…", "is_default": false}],
  "categories": [{"path": "work/meetings", "description": "…"}],
  "claims": [{"id": "clm_…", "kind": "habit", "category": "work/meetings", "text": "…",
              "audiences": "all", "visibility": "shared", "status": "approved",
              "confidence": 0.8, "evidence": {…}, "key": "habit:meeting_buffer",
              "origin": "derived", "superseded_by": null,
              "created_at": "…", "updated_at": "…", "last_evidence_at": "…"}]
}
```

Excluded: LLM keys, limits, audit, change log, auth data. Archived claims are included with
`status: "archived"`. Import modes: `merge` (default; existing IDs are updated, new ones
added, audiences and categories matched by name/path) and `replace` (wipe persona content
first).

Alignment with other formats (verified 2026-10-18): the W3C AI Agent Memory
Interoperability CG has no data model of its own yet (it references an encryption-envelope
draft), so there is nothing to align with. MacPaw Portable Memory's `fact` is the closest
schema; we use its names where they mean the same thing (`id` prefixes, `text`,
`confidence`, `superseded_by` ≙ `supersededBy`, `created_at` ≙ `createdAt`). A
`--format portable-memory` exporter is a later option.

---

## 5. Scopes and who sees what

| Scope | Granted to | Tools visible and callable | Claims visible |
|---|---|---|---|
| `owner` | the owner's own clients | all | everything in the persona |
| `read` | the owner's harness for personalisation | `get_persona` | `approved`, all visibilities |
| `respond:<a>,<b>` | an auto-responder or plugin acting for the owner | `predict` | `approved` and visible to the requested audience (§8.3); the audience must be one of `<a>,<b>` |

- `tools/list` is filtered by scope, so a `respond` client never even sees the owner tools.
  Every tool call checks the scope again.
- `predict` called with an `owner` token uses the **same audience view** as a `respond`
  token by default, so an owner-token auto-responder cannot leak `owner_only` claims.
  `include_private: true` (owner only) adds `owner_only` claims, for drafts the owner will
  read before sending.
- The admin role grants no persona access. Admin is a web-page role only (§9.6).

---

## 6. MCP tools

All tools return structured JSON (`structuredContent` plus a text mirror). Errors use MCP
tool errors with a stable `code` (`not_found`, `forbidden`, `invalid`, `rate_limited`,
`spend_cap`, `too_large`, `conflict`). Pagination: `limit` (default 50, max 200) and an
opaque `cursor`. 25 tools; read-only resources are a later option.

Shapes below are abbreviated JSON Schema. `?` marks optional fields.

### 6.1 Ingest

**`ingest_raw`** (owner) — accepts a batch, returns at once, derives in the background, then
drops the input.
```
in:  { records: Record[1..1000], llm_for_ingest?: bool }      // batch ≤ 2 MB serialized
out: { job_id, accepted: int }

Record = one of
  { type: "message", from_owner: bool, audience?: string, text: string ≤ 4000,
    ts: datetime, thread: string ≤ 64, position?: int }
      // thread is an opaque batch-local label; it is used to pair a non-owner message
      // with the owner's reply (latency) and is discarded with the batch
  { type: "call_transcript", audience?: string, ts: datetime,
    turns: [{ speaker: "owner"|"other", text: string }] }
  { type: "calendar_event", start: datetime, end: datetime, attendees: int ≥ 0,
    response: "accepted"|"declined"|"tentative"|"organizer"|"none",
    recurring?: bool, online?: bool }                  // no title, no attendees, no location
  { type: "decision", situation: string, choice: string, audience?: string, ts?: datetime }
  { type: "note", text: string, ts?: datetime }        // owner's free text about themselves
```
Every record object has `additionalProperties: false`. An unknown `audience` is mapped to
the default audience and counted in the job's `unknown_audiences` counter.

**`add_claims`** (owner) — claims derived elsewhere (a local model, another AI's memory).
```
in:  { claims: [{ kind, category, text, audiences?: "all"|string[] = "all",
                  visibility?: = "shared", confidence?, evidence?, proposed_category?,
                  status?: "pending"|"approved" }] (≤ 200),
       absorb?: bool = true }
out: { created: [id], absorbed: [{ index, into }],
       dropped: [{ index, reason: "guard"|"previously_rejected"|"unknown_audience"|"invalid", detail? }] }
```
Claims land as `pending`; `status: "approved"` is honoured for owner callers. With
`absorb`, a claim that restates an existing one strengthens it instead (§10).
`origin` is `added`. Text passes the verbatim guard's regex part (no emails, phones, URLs).

**`job_status`** (owner) — `in: { job_id }` → `out: { status, progress: 0..1, counts, error_code? }`.

### 6.2 Claims
- **`list_claims`** — `in: { category?, kind?, audience?, status?: status|"any" = "approved",
  include_archived?: bool, limit?, cursor? }` → `{ claims: Claim[], next_cursor? }`
- **`search_claims`** — `in: { query, status?, kind?, audience?, limit? }` → FTS5 with bm25
  ranking. `{ claims: [Claim & { score }] }`; `score` is relative to the best hit of this
  result (1.0 = best, others in [0, 1]), not comparable across queries.
- **`update_claim`** — `in: { id, text?, kind?, category?, audiences?, visibility?,
  confidence?, status? }` → `{ claim }`. Editing an approved claim's text keeps it approved
  (`origin` becomes `owner`). Archived claims can be restored with `status: "approved"` or
  `"pending"` within 30 days.
- **`delete_claims`** — `in: { ids: [..] }` → `{ deleted: int }`. Hard delete (owner intent),
  recorded nowhere but the counter.
- **`review_claims`** — bulk approve/reject; also accepts proposed change IDs.
  `in: { approve?: [id], reject?: [id], create_category?: bool }` →
  `{ approved: int, rejected: int, applied_changes: int, dismissed_changes: int }`.
  Approving a `chg_…` applies a proposed consolidation; rejecting it dismisses it.

### 6.3 Audiences and categories (owner)
- **`list_audiences`** → `{ audiences: [{ name, description, is_default, claim_count }] }`
- **`upsert_audience`** — `in: { name, description?, is_default?, rename_to? }`
- **`delete_audience`** — `in: { name, reassign_to?: string }`. Refused for the default
  audience. Claims that applied only to it are reassigned or, without `reassign_to`, the call
  fails with `conflict` and the count.
- **`list_categories`** → tree with `claim_count`.
- **`upsert_category`** — `in: { path, description?, rename_to? }` (depth ≤ 3).
- **`merge_categories`** — `in: { from: [path], into: path }`; recorded in the change log.

### 6.4 Compaction and logs (owner)
- **`compact`** — `in: { apply?: bool = false, steps?: ["dedupe","contradictions","decay",
  "budget","purge"] }` → `{ changes: [{ id?, op, state, claims, reason }] }`. With
  `apply=false` nothing is written.
- **`list_changes`** — `in: { state?, op?, limit?, cursor? }` → `{ changes }`
- **`undo_change`** — `in: { id }` → restores the before-snapshot; fails with `conflict` if a
  later change touched the same claims (undo that first).
- **`list_audit`** — `in: { since?, audience?, outcome?, limit?, cursor? }` → audit rows.

### 6.5 Persona use
**`get_persona`** (owner, read) — compact text for a system prompt.
```
in:  { audience?: string, categories?: [path], max_tokens?: int = 1500, format?: "text"|"json" }
out: { text: string, claims_used: int, truncated: bool }
```
Order: boundaries, style for the audience, then other kinds by confidence × evidence
weight, grouped by category, cut at `max_tokens` (estimated as chars/4).

**`predict`** (owner, respond) — see §8.
```
in:  { situation: string ≤ 8000, audience: string, facts?: [{ what, value }] ≤ 20,
       kind?: "reply"|"decision", include_private?: bool }
out: one of
  { type: "answer",   answer, confidence, disclosure, used_claims: [id], hints: [string] }
  { type: "needs",    needs: [{ what, why }], used_claims: [id], hints }
  { type: "escalate", escalate: "low_confidence"|"out_of_persona"|"boundary"|
                      "rate_limited"|"spend_cap"|"llm_unavailable"|"audience_not_permitted",
                      reason, hints }
```
`escalate` is a reason code rather than `true`, so `rate_limited` and the rest share one
field (D6).

### 6.6 Persona lifecycle and settings (owner)
- **`export_persona`** → `{ document }` (§4.7). Large personas are still small (budgeted).
- **`import_persona`** — `in: { document, mode?: "merge"|"replace" }` → counts. Validated
  against `persona.v1.json`.
- **`delete_persona`** — `in: { confirm: "<login>" }`. Deletes all persona content, audit,
  changes and jobs; the account and grants stay (a fresh empty persona is created).
- **`get_settings`** → display name, timezone, LLM backend (key masked), `llm_for_ingest`,
  `escalate_below`, disclosure template, effective limits and ceilings, today's usage,
  `spend_capped` flag, the plain-language note about where raw text goes.
- **`update_settings`** — any of the above that the user may change; limits only downward
  from the admin ceilings.

---

## 7. Ingest and derivation

### 7.1 Jobs
`ingest_raw` validates and size-checks the batch, writes a `jobs` row (counts only), puts
the records on an in-memory queue and returns. One worker per persona (limit
`concurrent_jobs`, default 1). The records are released when the job ends, in `finally`.
On restart, `running`/`queued` jobs become `interrupted` and the harness resends; there is
nothing to resume because nothing was saved (P1).

### 7.2 Habit detectors (code, statistics)
Habits nobody puts into words are computed in code. Each detector has a stable `key`, a
minimum sample size, and produces at most one or two claims with numbers in `evidence`.
Re-runs update the keyed claim by merging `evidence.stats` (§4.5), so they never duplicate.
Each is documented in `docs/habits.md` with its formula, thresholds and a synthetic-calendar
test.

| Key | Input | Output claim (example) |
|---|---|---|
| `habit:meeting_buffer` | accepted calendar events, same-day gaps | "Keeps ≥10 min between calls (87% of 212 gaps)." |
| `habit:meeting_length` | accepted event durations | "Meetings are typically 25 or 50 min." |
| `habit:meeting_free_slots` | weekday × half-day acceptance density | "No meetings on Friday afternoons." |
| `rule:accept_buffer` | responses vs. the gap the event would leave | decision_rule: "Declines invites that leave <10 min buffer." |
| `rule:accept_size` | response by attendee count | decision_rule: "Usually declines meetings with >8 attendees." |
| `habit:active_hours` | owner message timestamps, accepted events | "Active 08:30–19:00 on weekdays, rarely on Sunday." |
| `habit:reply_latency:<aud>` | thread pairs other → owner | style: "Replies to work-slack within ~4 min in work hours, next morning otherwise." |
| `habit:message_length:<aud>` | owner message lengths | style: "Writes to family in 1–2 short lines." |

Thresholds (initial, tunable in code): ≥ 30 samples, share ≥ 0.75 for a rule-like
statement, period ≥ 3 weeks. Times are evaluated in the persona's timezone; event offsets
are honoured.

### 7.3 LLM derivation (style and wording only)
- **Style per audience:** owner messages (with the preceding non-owner message as context)
  and owner turns of call transcripts, grouped by audience, chunked to ~2500 chars per call.
  The model returns style/boundary/preference claims as JSON, choosing categories from the
  existing list or setting `proposed_category`.
- **Notes:** extracted into claims of any kind.
- **Decisions:** extracted into `decision_rule` claims when the same pattern shows up at
  least twice in the batch; single decisions become low-confidence `preference` claims.
- Results from all chunks are first deduplicated among themselves, then absorbed against
  existing claims (§10.1); only genuinely new ones become `pending`.
- With `llm_for_ingest: false` only the code detectors run.

### 7.4 Verbatim guard
LLMs copy input. Every derived claim text is checked against the batch before it is
stored, and dropped (counted as `dropped_by_guard`) if it contains:
1. an email address, phone number, URL or `@handle` (regex);
2. any 5-word sequence that appears verbatim in a non-owner text in the batch;
3. a token that appears in a non-owner text of the batch **and** looks like an identifier
   or a name: contains a digit or mixed case, or is capitalised in a non-initial position,
   and is not in the persona's own vocabulary (audience names, category names, display
   name) or a small built-in list (weekdays, months, languages).

The guard is deliberately crude and strict (D7). The privacy canary test
(§13) runs both "token-like" and "name-like" canaries through it.

---

## 8. `predict`

### 8.1 Flow
1. **Limits** (§11): per token, per persona, per audience. Over a limit → `escalate:
   "rate_limited"` with no LLM call.
2. **Resolve audience.** Unknown name → the default audience, and `hints` says so. For a
   `respond` token the resolved audience must be within its scope, otherwise
   `escalate: "audience_not_permitted"`.
3. **Load the visible set** in SQL (§8.3). If its total size is under ~1500 tokens, skip
   routing and use it all.
4. **Route:** one cheap LLM call that sees only category paths and descriptions plus the
   situation, and returns 1–3 paths (JSON). If the call fails or returns nothing, fall back
   to FTS5 over the visible set with terms from the situation.
5. **Load context:** visible claims in the routed categories, plus the always-on core:
   `style` and `boundary` claims for that audience. Capped at a token budget, highest
   confidence × evidence first.
6. **Answer:** one LLM call with the owner's display name, the audience description, the
   claims (with IDs), the facts and the situation. The model returns strict JSON:
   `{"type":"answer","answer","confidence","used_claims","language"}` or
   `{"type":"needs","needs":[{"what","why"}]}` or
   `{"type":"escalate","escalate":"out_of_persona"|"boundary","reason"}`.
7. **Post-process:** drop `used_claims` IDs not in the context; `confidence <
   escalate_below` (default 0.6) → `escalate: "low_confidence"`; add the `disclosure`;
   write the audit row; count usage.

`needs` lets a decision rule ask the harness for facts: "free slots on Tuesday ≥ 20 min with
10 min buffers", why: "decision_rule clm_…". The harness looks them up and calls again with
`facts`. The server keeps no state between the two calls.

### 8.2 Disclosure
Server-built, localised by the answer's language, e.g. en: "This is {owner}'s
auto-responder; most likely {owner} would answer:". Built-in templates for en, ru, de, uk,
es, fr; English fallback; the owner can override the template (must contain `{owner}`).
It is returned for every answer, owner token or not; the harness decides how to show it
for drafts the owner sends personally.

### 8.3 Visibility filter (SQL, before any LLM call)
```sql
status = 'approved'
AND (audiences_all OR :aud IN claim_audiences)              -- applies to this audience
AND (visibility = 'shared'
     OR (:include_private AND :caller_is_owner))            -- owner_only only on request
```

---

## 9. Auth

### 9.1 Shape
The process is both the OAuth 2.1 authorization server and the resource server. We use the
MCP SDK's auth building blocks, not Authlib:
- `create_auth_routes()` mounted in our FastAPI app gives `/authorize`, `/token`,
  `/register` (DCR), `/revoke` and `/.well-known/oauth-authorization-server`, with S256 PKCE
  verification done by the SDK.
- Our `OAuthAuthorizationServerProvider` implementation stores clients, codes and tokens in
  SQLite (`get_client`, `register_client`, `authorize`, `load_authorization_code`,
  `exchange_authorization_code`, `load_refresh_token`, `exchange_refresh_token`,
  `load_access_token`, `revoke_token`).
- `MCPServer(token_verifier=…)` checks every `/mcp` request against the DB (separated AS/RS
  wiring, as the SDK docs now recommend), and `/.well-known/oauth-protected-resource` is
  served via `AuthSettings.resource_server_url`. `validate_token_resource=True`, with the
  RFC 8707 `resource` carried from the code into the token.
- We post-process the AS metadata to add `token_endpoint_auth_methods_supported: ["none",
  …]`, `scopes_supported` and, once implemented, `client_id_metadata_document_supported`.
  We do **not** advertise `offline_access` in protected resource metadata (the spec says
  SHOULD NOT) but do issue refresh tokens.
- Claude's hosted callback (`https://claude.ai/api/mcp/auth_callback`), ChatGPT's
  (`https://chatgpt.com/connector/oauth/…`) and loopback redirects (Claude Code, any port on
  `localhost`/`127.0.0.1`) must all work; tests cover each pattern.
- **Client registration:** DCR in M2, because both Claude and ChatGPT fall back to it.
  **CIMD** (client ID metadata documents; the current spec's SHOULD, preferred by ChatGPT)
  is also in M2 if `get_client` can fetch it cleanly: HTTPS only, no private IP targets,
  5 s timeout, 16 KB cap, cached. Claude registers a new DCR client on every connection, so
  clients with no grant are purged after 24 h.

### 9.2 Accounts
- Login (email) + password, argon2id. Login rate-limited per IP and per login.
- **First run:** if there are no users, the server prints a one-time setup URL
  (`/setup?t=…`, 24 h, regenerated on restart while there are no users). It creates the
  admin.
- **Invites:** the admin creates a one-time invite link on the users page or with
  `wwyd invite`; the invitee chooses login and password.
- **Change password:** current + new on the account page; all other web sessions end;
  OAuth grants and API keys stay valid unless revoked. Setting a password (change or reset)
  invalidates the user's outstanding reset links.
- **Forgotten password:** the admin generates a one-time reset link (page or
  `wwyd reset-password <login>`); with SMTP configured users can also request one. A reset
  ends all web sessions and is shown on the user's account page ("password reset by admin
  at …"). The reset form has "Also disconnect all apps and API keys", ticked by default,
  which revokes every grant and key. A new reset link replaces the user's older ones.
  Self-service requests are limited per IP and per login (3/hour, silently: the answer is
  the same whether or not the login exists).
- **Lost admin access:** `wwyd reset-password <login>` on the host.

### 9.3 Consent
When a client connects, the user signs in and picks one scope: `owner`, `read`, or
`respond:` with checkboxes for audiences. The redirect host is the most prominent element.
For DCR clients the name is marked as self-declared and unverified; CIMD clients also show
the host of their client ID URL. If the client requested a scope, it is preselected.

### 9.4 Tokens, grants, keys
- Opaque 256-bit random strings, stored as SHA-256 hashes, looked up on every request, so
  revocation is immediate.
- Access tokens 1 h; refresh tokens 30 days, rotated on every use; reuse of a rotated
  refresh token revokes the whole grant; a bad one returns `invalid_grant`.
- **API keys** (`wwyd_<8-char prefix>_<secret>`) for scripts and plugins, created on the
  account page with the same scopes, shown once, revoked the same way. Sent as
  `Authorization: Bearer`.
- `respond:` scopes bind audience *names*: deleting (or renaming) an audience revokes the
  grants and keys whose scope names it (`tokens.revoke_for_audience`), so a later audience
  with the same name inherits nothing.
- **Abuse limits** (in memory, per process): per-IP keys use the IPv6 /64. `/token`
  failures count per IP and per (client_id, IP), the latter only for registered clients;
  `/mcp` counts per IP only bearers that match no token or key ever issued (stale ones do
  not count). Request bodies are capped: 64 KB outside `/mcp`; on `/mcp` the largest
  `batch_bytes` any user may have (instance default or highest admin ceiling) + 1 MB.
  Oversized bodies get 413, also when chunked.
- The **Connected apps** section lists every grant and key (client name, scope, created,
  last used) with a Revoke button.

### 9.5 Web pages (server-rendered, no build step)
1. **Sign in & consent** (`/login`, `/consent`).
2. **Account** (`/account`): change password, connected apps and API keys with Revoke, the
   connection snippets for Claude Desktop, Claude Code and ChatGPT (shown first right after
   setup), the spend-cap flag.
3. **Users & invites** (`/admin`, admin only): users, invite links, reset links, disable,
   per-user limit ceilings, usage counters.
4. **Landing** (`/setup`, `/invite`, `/reset`): choose login/password from a one-time link.

One small stylesheet, system font stack, no JavaScript beyond copy-to-clipboard. Cookies
`HttpOnly; Secure (except localhost); SameSite=Lax`; CSRF tokens on every form.

### 9.6 Admin boundary
The admin manages users, links and limits. No admin path reads persona content. Honest
limitation (documented): whoever runs the host can read the SQLite file, and an admin who
resets a user's password can sign in as them, which the user will see. Per-persona
encryption at rest is a later option.

---

## 10. Compaction

Runs after every ingest job (for the claims that job touched), nightly at 03:30 in the
persona's timezone (in-process scheduler, no cron), and on `compact`. Every write is a
`changes` row.

### 10.1 Dedupe and absorb
- Candidates: same persona, same `kind`, same category, status `pending`/`approved`. For a
  new or changed claim, query FTS5 with its terms (top 5), then compute token-set Jaccard.
- Jaccard ≥ 0.6 → duplicate, unless the token symmetric difference contains a polarity
  marker (negations such as never/not/rarely/avoids and their equivalents in the supported
  languages; the set lives in `compact/constants.py`). 0.3–0.6, and ≥ 0.6 pairs with such a
  marker → one batched LLM judge call per category: `same | contradicts | different`.
  `add_claims` has no LLM, so it never absorbs a marker pair: the claim lands as pending
  and compaction judges the pair later. On any LLM error the pairs are skipped (counted).
  `compact(apply=false)` does not call the judge (it would record usage); it reports the
  pairs it would send as `judge_pending`.
- Keyed detector claims merge by `key` without any of this.
- **pending + pending:** merge into one pending claim (newer text, summed evidence).
- **new derivation + approved:** **absorb**: the approved claim keeps its text, evidence is
  summed, `last_evidence_at` and confidence go up (`c = max + (1 − max) × min × 0.5`,
  capped at 0.99). The review queue only grows with new information.
- **new derivation + rejected:** drop it.
- **approved + approved:** a `proposed` merge the owner approves via `review_claims`.

### 10.2 Contradictions
Pairs judged `contradicts`: the higher evidence weight (`confidence × log(1 + Σ sources)`)
wins; on a tie, the newer `last_evidence_at` wins. The loser is
archived with `superseded_by`. If the loser is owner-approved, it becomes a `proposed`
change instead.

### 10.3 Decay
Nightly: confidence halves for every 6 months without fresh evidence (continuous:
`c × 0.5^(Δmonths/6)`, applied from the stored value and `last_evidence_at`). Below 0.2 the
claim is archived (`decayed`); approved claims become proposals. Claims with `origin:
owner` do not decay.

### 10.4 Size budget
Per persona: at most 30 non-archived claims per category, and approved claims totalling at
most 6000 tokens (chars/4). Over budget, the lowest evidence-weight claims are archived
first; approved claims are proposed for archiving instead. Both numbers are per-user limits
(§11: `max_claims_per_category`, `max_approved_tokens`): defaults live in config, the
admin can set ceilings, and the owner can set lower values; compaction uses the persona
owner's effective values.

### 10.5 Purge and undo
Archived claims are restorable for 30 days, then hard-deleted (`secure_delete` on).
`undo_change` restores the before-snapshot of any applied change that no later change
depends on.

---

## 11. Limits and usage

The same code runs hosted and self-hosted. Effective limit = min(admin ceiling for the
user if set, else the config default; the user's own setting). An admin ceiling replaces
the default and may be higher than it (larger plans); the user's own setting can only
lower the effective value, never raise it.

| Limit | Scope | Default |
|---|---|---|
| requests per minute | per token/key and per persona | 60 / 120 |
| `predict` per day | per persona | 500 |
| `predict` per day | per audience | 150 |
| LLM tokens per day | per persona | 500 000 |
| `ingest_raw` bytes per day | per persona | 20 MB |
| batch size | per call | 1000 records / 2 MB |
| concurrent jobs | per persona | 1 |
| spend cap | per persona per calendar month | $5 on paid backends; none on local |
| compaction size budget (§10.4) | per persona | 30 claims per category / 6000 approved tokens |
| LLM concurrency | global | 2, queue timeout 60 s, call timeout 120 s |

- RPM uses in-memory token buckets; daily counters live in `usage_daily`.
- Over a limit, `predict` returns `escalate: "rate_limited"` with no LLM call; other tools
  return a `rate_limited` error with `retry_after`.
- **Spend:** cost per call is taken from the backend's reported cost when it returns one
  (OpenRouter), otherwise from per-1M-token prices configured on the backend. Reaching the
  cap stops LLM calls for the persona (`escalate: "spend_cap"`, ingest jobs fail with
  `spend_cap`) and sets a flag shown on the account page and in `get_settings`.
- **Usage counters** per user per day (calls per tool, LLM tokens in/out, cost, ingest
  bytes and records) are kept for future billing and shown to the admin.

---

## 12. LLM backend

One OpenAI-compatible client (`httpx`, chat completions, JSON responses), configured per
persona: `base_url`, `api_key` (encrypted at rest), `model`, optional prices. Presets:

| Preset | Default |
|---|---|
| `instance` | the server's shared default (config); used when the persona sets nothing |
| `localai` | `http://127.0.0.1:1240/v1`, `qwen3-vl-8b-instruct` |
| `ollama` | `http://127.0.0.1:11434/v1` |
| `lmstudio` | `http://127.0.0.1:1234/v1` |
| `openrouter` | `https://openrouter.ai/api/v1`, owner's own key |

Robustness kit for small local models, applied to every call: `reasoning_effort: "none"`;
fall back to `reasoning_content` when `content` is empty; strip Markdown fences around JSON;
wrap bare arrays; sanitise chat-template markers in input; generous `max_tokens`; every
field the model must emit is `required` in the JSON shape we ask for; a few worked examples
per prompt, including in Russian, since rules leak language through examples. Preflight
`GET /models` when a backend is saved.

**Private addresses (SSRF).** `[llm] allow_private_base_urls` /
`WWYD_LLM_ALLOW_PRIVATE_BASE_URLS` (default `true`, since a self-hosted instance talks to
LocalAI on localhost). Set it to `false` on multi-user hosted instances: a persona's own
`base_url` must then resolve only to public unicast addresses, checked when it is saved and
again on every request, and the connection goes to the validated IP (hostname kept in `Host`
and TLS SNI) so DNS rebinding cannot redirect it; redirects are not followed. The admin's
instance backend is exempt. Changing a backend's host or preset without sending `api_key`
again is rejected rather than forwarding the stored key to the new host. Errors returned to
callers say only "LLM backend unreachable or rejected the request" (never the upstream
status or reachability detail); rate-limit (429) and credit (402) responses keep their
codes.

---

## 13. Testing and evaluation

- **Tool contracts:** every tool's input/output schema, error codes and scope filtering of
  `tools/list`.
- **Auth and scope matrix:** each scope × each tool; DCR and (if built) CIMD; PKCE failure
  cases; redirect URI patterns for Claude, ChatGPT and loopback; refresh rotation and reuse
  detection; revocation of a grant or key takes effect on the very next request; password
  change ends other web sessions but not grants; setup/invite/reset links are single use.
- **Visibility:** a `respond` token never receives an `owner_only` claim or a claim for
  another audience, asserted on the context sent to a fake LLM, not on its output.
- **Limits:** each limit, the per-audience isolation, spend cap, concurrency queue timeout.
- **Habit detectors:** synthetic calendars and message streams with known ground truth,
  including timezone and DST edges, below-threshold samples, and evidence merging across
  runs.
- **Compaction:** dedupe, absorb, rejected-as-negative-memory, contradictions, decay,
  budget, proposals for approved claims, undo, purge.
- **Privacy canary:** ingest records containing unique strings (token-like
  `zq7x-canary-41b9`, and name-like `Velmorine`) in every field type, with a fake LLM that
  deliberately echoes its input and with the real local model; then assert none of them
  appears in the DB file (including `-wal`), the logs at DEBUG level, `export_persona`,
  `get_persona`, or any `predict` output.
- **Fake LLM** fixture for deterministic tests; a small `@pytest.mark.llm` suite runs
  against LocalAI when available.
- **Eval script** (`wwyd eval pairs.jsonl`): the owner prepares lines
  `{situation, audience, real_reply}` locally; the script calls `predict`, shows the real
  reply and the prediction in random order, and the owner guesses which is real. It prints
  the detection rate (50% = indistinguishable) and stores nothing but the score. Results
  reported in `docs/eval.md` for `qwen3-vl-8b-instruct` (local) and one OpenRouter model
  (chosen at M3, see D12).

---

## 14. Packaging, install, repo layout

- **Local:** `uvx wwyd` starts the server on `127.0.0.1:8740` and prints the setup URL.
  `docker run -v wwyd:/data -p 8740:8740 <image>` does the same in a container (Docker is an
  optional distribution path; development and the default install need only uv).
- **Cloud:** `deploy/docker-compose.yml` (wwyd + Caddy with automatic HTTPS for a domain)
  and `deploy/cloud-init.yaml` for a fresh Ubuntu VM.
- **Docs:** `docs/install.md` for a non-technical person (numbered steps, what you should
  see, what to do if you don't), verified by following it literally on a clean VM;
  `docs/habits.md`; `docs/ecosystem.md` (guidance for harness authors: messengers verified
  against primary sources, contact → audience mapping, transcription); `docs/eval.md`.
- **CLI:** `wwyd` (= `wwyd serve`), `wwyd stdio --user <login>`, `wwyd invite`,
  `wwyd reset-password <login>`, `wwyd eval`. Migrations run automatically on start.

```
pyproject.toml           # [project.scripts] wwyd = "wwyd.cli:main"
src/wwyd/
  cli.py config.py app.py
  db/          connection.py migrations/0001_init.sql …
  store/       claims.py audiences.py categories.py changes.py audit.py usage.py
  auth/        provider.py tokens.py passwords.py links.py web.py scopes.py
  web/         templates/*.html static/style.css
  mcp/         server.py tools/*.py
  llm/         client.py presets.py prompts/
  predict/     route.py answer.py disclosure.py
  ingest/      jobs.py records.py guard.py style.py detectors/*.py
  compact/     dedupe.py contradictions.py decay.py budget.py scheduler.py
  limits/      limiter.py spend.py
schema/persona.v1.json
docs/  deploy/  tests/
CLAUDE.md  .claude/agents/worker.md      # orchestration conventions, added at M1
```

---

## 15. Later options
Embeddings (only if FTS + categories measurably fail); a Portable Memory exporter;
read-only MCP resources mirroring `get_persona`; TOTP and passkeys; per-persona encryption
at rest; horizontal scaling (shared limiter, external queue); durable ingest jobs (would
contradict P1 unless encrypted and short-lived); billing; a web UI for claim review.

---

## 16. Milestones

Each ends with tests passing and a short demo.

1. **Store and CRUD:** schema and migrations, seeded categories, default audience, claim /
   audience / category tools, FTS search, export/import with `persona.v1.json`, stdio
   transport. Demo: Claude Code over stdio adds, searches, reviews and exports claims.
2. **Auth and limits:** setup link, invites, login/password, change and reset, OAuth (SDK
   routes, DCR, CIMD if clean), consent with scopes, API keys, Connected apps with Revoke,
   the four pages, all limits and usage counters. Demo: connect Claude Desktop over HTTP on
   localhost; revoke and watch the next call fail.
3. **`predict`:** routing, visibility filter, `needs`/`facts`, disclosure, audit,
   `get_persona`, the privacy canary (for predict paths), eval script. Demo: an
   auto-responder-style call with a `respond:` key, a `needs` round trip.
4. **`ingest_raw`:** jobs, record schemas, habit detectors, LLM style derivation, verbatim
   guard, full privacy canary. Demo: a synthetic calendar and chat export produce pending
   habit claims with numbers.
5. **Compaction:** dedupe, absorb, contradictions, decay, budget, purge, proposals, change
   log and undo, nightly scheduler.
6. **Install:** packaging, Dockerfile, compose + Caddy, cloud-init, `docs/install.md`
   verified on a clean VM, `docs/ecosystem.md`, eval results.

Outward-facing steps (GitHub repo, PyPI, container registry, domains, OAuth app
registrations) wait for the maintainer's go-ahead each time.

---

## 17. Decision log

Design decisions taken when this spec was approved (2026-10-18), with the reasoning.

**D1. Visibility is `owner_only | shared`.** `audiences` already says where a claim
applies, and a claim is never fed to an audience it does not apply to, so a separate
`anyone` value would behave exactly like `audiences`. Import maps the legacy values
`audiences` and `anyone` to `shared`.

**D2. Name.** Package, CLI and repository are all `wwyd` ("what would you do").

**D3. Field casing.** snake_case everywhere (SQL, MCP tools, export), with field names
aligned to MacPaw Portable Memory where they mean the same thing. Casing conversion
belongs in the later Portable Memory exporter.

**D4. Client registration.** DCR first, because Claude and ChatGPT both fall back to it
even though the current MCP spec deprecates it. CIMD lands in M2 if it is small, otherwise
right after.

**D5. `predict` defaults to the audience view for every token,** owner tokens included;
`include_private: true` adds `owner_only` claims. An owner-token auto-responder therefore
cannot leak private claims by accident.

**D6. `escalate` is a reason code string** rather than `true` plus a reason, so
`rate_limited`, `spend_cap` and the rest share one field.

**D7. The verbatim guard is deliberately strict.** It will sometimes drop a legitimate
claim (e.g. one that mentions a product name the other side also used). Drops are counted
in job status and the thresholds are revisited after the eval.

**D8. Ingest jobs are memory-only,** so a restart loses running jobs. This follows from P1.

**D9. Remote LLM backends see raw text.** This is allowed, stated plainly in settings and
on the account page, and avoidable with a local backend or `llm_for_ingest: false`. A
hosted instance should use a provider with a no-retention policy as its default; that is a
deployment choice, not code.

**D10. An admin password reset makes impersonation possible.** Accepted, made visible to
the user on the account page, and documented in the threat model.

**D11. Licence: AGPL-3.0-or-later.** MCP clients and harnesses that talk to an unmodified
server over the network are unaffected; anyone offering a modified server as a service
must publish their changes.

**D12. Eval models.** Local: `qwen3-vl-8b-instruct` on LocalAI. Hosted: one current
mid-price OpenRouter model, chosen at M3.

**D13. Default port 8740.**

**D14. Proposals reuse `review_claims`.** Merges of two approved claims and other proposed
changes are approved or dismissed with their `chg_…` IDs, which keeps the tool count at 25.

**D15. Container images** will be published to GitHub Container Registry at M6.

### Implementation notes

Deviations and details recorded while implementing M2–M6:

- **Guard on owner texts.** Guard part 3 also scans the owner's own texts, not only third
  parties' (P2).
- **Job columns.** `jobs` has two extra numeric columns, `progress` and `started_at`.
- **Audience before limits.** `predict` resolves the audience before the limit check.
- **Empty visible set.** When no claim is visible to the audience, `predict` escalates
  `out_of_persona` without an LLM call.
- **Usage accounting.** `record_predict` runs only when the LLM was actually called.
- **Audit IDs** use the `adt_` prefix.
- **LLM settings shape.** Persona setting `llm` is `{preset, base_url, model, api_key,
  price_in_per_m, price_out_per_m}`; `api_key` is write-only and encrypted at rest. The
  instance default is the `[llm]` config table / `WWYD_LLM_*` with the same fields.
- **Preflight.** A failed `GET /models` preflight when saving a backend is rejected with
  `invalid`.
- **HTTP 402** from the LLM backend maps to `spend_cap`.
- **Revocation.** `/revoke` needs an empty `client_secret` for public clients in this MCP
  SDK version.
- **Self-service resets** do not show the admin-reset notice on the account page.
- **`/healthz`** is an unauthenticated liveness endpoint that does not touch the DB.
- **Answer shape.** The answer call's JSON has an `absent_facts: [{what, why}]` field
  (replacing `needs`) that the model fills first: the facts the applicable decision rules
  need but `facts` lacks. The name sorts before `answer` on purpose, since some backends'
  JSON grammars emit keys alphabetically and the field only helps if it is generated
  before the model commits to an answer. Post-processing turns `type: "escalate"` with a
  non-empty `absent_facts` into `needs` (small models list the gap, then escalate),
  except `escalate: "boundary"`, which always stands.
