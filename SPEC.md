# wwyd — "what would you do"

Status: **approved** design (2026-10-18). Key decisions and their reasoning are in §17.
Revised and approved 2026-10-19: episode records, organic categories, analyzers (D17–D22),
implemented at M7 (7a–7c); client contract revision (D23, M8).
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
| P1 | **Raw input is never persisted.** `ingest_raw` episodes live only in process memory (the job object, and a sandboxed analyzer's stdin) until the job finishes or fails. They never reach the DB, logs, error reports, exports or the change log. What is persisted is derived: claims, analyzer sources and statements, analyzer state counters, category paths and descriptions, label vocabularies. | Episodes are held only by the job object and reach the sandbox as in-memory stdin bytes, never a file; no SQL table has a column for them. Every persisted derived text passes the **verbatim guard** (§7.7); analyzer state keys pass the host key filter (§7.2). Logging filter drops tool arguments at every level; tracebacks are logged without locals or argument reprs; the SDK's request logging is disabled; sandbox stderr is discarded. `PRAGMA secure_delete=ON`. Privacy canary test. |
| P2 | **No contact identities.** No names, phone numbers, emails, messenger IDs or hashes of them. | Episode free-text fields (`situation`, `decision`, `said`, `where`) are never stored; every persisted derived artefact passes the guard or the key filter (P1). Episodes have no identity fields (`additionalProperties: false`). Audiences are owner-defined names. `docs/ecosystem.md` tells clients to leave names out of episodes and category paths. Residual risk below. |
| P3 | **No live data.** An episode may describe live data (a meeting, a mail) for derivation, but none of it is kept. | Free-text fields are never stored, only guarded derived artefacts; `where` is coarse context by contract (never an address). Facts passed to `predict` are used for that call only and never stored. |
| P4 | **Third parties see only what the owner allowed.** | Claim filtering happens in SQL before the LLM call (§8.3). The prompt is never relied on to hide anything. |
| P5 | **Answers to third parties are disclosed as automated.** | `predict` always returns a server-built `disclosure` string (EU AI Act Art. 50). |
| P6 | **Audit without content.** Every `predict` call is logged with time, client, audience, outcome and confidence, never the situation text, context or answer. | Audit table has no text columns. |

**Residual risk (P2).** The key filter (§7.2) cannot recognise a name the owner always
writes in lowercase and uses often (≥ 5 episodes on ≥ 3 days): it can survive as a
stylometry or analyzer state key. Mitigations: every derived claim starts `pending` for
owner review, `get_analyzer` shows every state key, and the canary suite includes a
lowercase-but-concentrated name (§13). The same limit applies to the concentrated-word
check on derived texts (§7.7): a lowercase name used often and on ≥ 3 days passes it.
Category paths are client-supplied labels like
audience names: they pass the guard's regex part, and keeping names out of them is the
client's job (§4.4).

One flow leaves the server on purpose: **raw episodes and `predict` situations are sent to the
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
                   │      │                     ▲  raw episodes live only here (memory)     │
                   └──────┼─────────────────────┼────────────────────────────────────────────┘
                          ▼                     │
                 ~/.wwyd/wwyd.db (SQLite, WAL)   └── nightly scheduler (asyncio task)
```

- **Stack:** Python ≥ 3.12, uv, FastAPI, official MCP Python SDK `mcp>=2.3,<3`
  (`mcp.server.MCPServer`, formerly FastMCP), SQLite in WAL mode with plain SQL migrations,
  `argon2-cffi`, `httpx` for the OpenAI-compatible LLM client, `cryptography` (Fernet) to
  encrypt LLM API keys at rest, Jinja2 for the four pages, `wasmtime>=49,<50` with a
  shipped CPython 3.15 WASI build for the analyzer sandbox (§7.5).
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
  (`[limits]`), `[auth] allow_insecure_redirects` (§9.1), and optional SMTP.

### Source of truth per subsystem

| Subsystem | Source of truth | Derived from it |
|---|---|---|
| Persona content | `claims` rows (+ `claim_audiences`) | FTS index, `get_persona` text, exports, predict context |
| Habit and style statistics | `analyzer_state` counters (per analyzer, month, metric, key) | keyed claims' numbers and text, rendered by code |
| Categories | `categories` + `category_aliases` | routing paths, claim placement of old paths |
| Consolidation history | `changes` rows (with before/after snapshots of the claims touched) | undo, proposals |
| Access | `grants`, `api_keys` (hashed) | access/refresh tokens; every request re-checks the DB |
| Limits | admin ceilings + user overrides + config defaults | effective limit = min of the three |
| Usage | `usage_daily` counters | rate-limit decisions, spend cap, future billing |

---

## 4. Data model

IDs are random and kind-prefixed (`usr_`, `aud_`, `cat_`, `clm_`, `chg_`, `job_`, `key_`,
`grt_`, `anl_`), following MacPaw Portable Memory. Timestamps are ISO-8601 UTC with `Z`.
Field names are snake_case everywhere (SQL, MCP tools, export); see D3.

### 4.1 user
`id, login (email, unique), password_hash (argon2id), is_admin, disabled_at, created_at,
password_changed_at`. One persona per user, created with the user.

### 4.2 persona
`id, user_id (unique), display_name` (used in the disclosure), `timezone` (IANA, used by
analyzers and the nightly pass; the host converts every episode time to it first), `settings` (JSON: LLM backend, `llm_for_ingest`,
`escalate_below`, `disclosure_template`, own limit overrides, budget, `weekend_days`: a
plain list of English weekday names, default `["saturday", "sunday"]`, `[]` for none; it
marks the predict `When:` line, §8.1), `spend_capped_at`,
`created_at`.

### 4.3 audience
`id, persona_id, name, description, is_default, created_at, updated_at`.
- `name` is a slug (`[a-z0-9][a-z0-9_-]{0,39}`) because it appears in scopes
  (`respond:family,work-slack`) and in harness configuration.
- `description` is the owner's free text: "colleagues in the work Slack, peers, informal,
  Russian/English". There are no fixed dimensions.
- Exactly one audience per persona has `is_default = true`. A new persona starts with one
  default audience, `everyone` ("anyone not in another group"), which the owner can rename.
- `default` is a reserved alias, not a name: every tool that takes an audience name
  (`predict`, `get_persona`, `add_claims`, `list_claims`, `ingest_raw` episodes, …) resolves
  `default` to the persona's default audience, whatever it is called, with no fallback
  hint. Creating, renaming or importing an audience named `default` fails with `invalid`.
  Scopes are different: `respond:` scopes name real audiences, and `respond:default` is
  not a valid scope (rejected at registration, consent and key creation).
- Only owner-scoped calls create, change or delete audiences.

### 4.4 category
`id, persona_id, parent_id, name, description, path` (materialised, e.g.
`communication/style`), at most 3 levels. Seeded per persona as a starter vocabulary:

```
communication/style        communication/boundaries
work/meetings              work/decisions
schedule/routine
food  travel  money  health  family  hobbies  tech  other
```

- **Path normalisation** (every tool and episode): NFKC, casefold, spaces → `-`; segments
  of Unicode letters, digits, `-` and `_`, starting with a letter or digit, ≤ 40 chars;
  depth ≤ 3. This is a superset of the former ASCII rule `[a-z0-9][a-z0-9_-]{0,39}`, so
  existing paths stay valid. Audience names stay ASCII slugs (§4.3).
- **Organic creation.** An episode's `category` is resolved in this order: an existing
  path; an alias (longest matching prefix in `category_aliases`, the rest of the path
  appended); otherwise the path and its missing parents are created at ingest. One batched
  LLM call per job writes one-line descriptions for the new paths (routing sees only paths
  and descriptions); without an LLM step the description stays empty. Creations are one
  `category_create` change per job (undoable while the categories hold no claims).
- **Limits.** `max_categories` per persona (200, seeds included) and
  `new_categories_per_job` (20). Beyond either, the episode maps to the deepest existing
  ancestor of its path, or to `other` when none exists, and is counted in
  `unmapped_categories`.
- **Placement by the server (D23).** `category` is optional. Episodes without one are
  placed by the job's first LLM step: batched calls (≤ 25 episodes and ~6000 chars of
  episode text per call; `situation` and `said` cut to 300 chars, `decision` to 200) see
  the persona's category paths and descriptions and return one item per episode:
  `{"placements": [{"i", "path", "path_description"}]}`, an existing path or a new one,
  with `path_description` empty for an existing path and a one-line description for a new
  one; a new path left without a description that passes the guard gets one from a single
  batched description call after placement. Only paths used by an episode
  are created; when several episodes propose the same new path, the first description
  that passes the guard is kept. A returned path goes through the same resolution and limits
  as a client's path (over a limit it maps like one and counts in `unmapped_categories`).
  A new path is checked by the full verbatim guard, including the concentrated-word check
  (§7.7); a description by the guard's parts 1–3, like every category description (the
  concentrated-word check would reject ordinary words of a sentence in a small batch).
  An episode is counted in `unplaced_episodes` when the server could not place it as
  proposed: no LLM step (`llm_for_ingest: false`; it goes to `other`), no usable path in
  the call's output (`other`), or a new path that fails the guard (the deepest existing
  ancestor, else `other`). A description that fails is dropped and the path kept. Paths
  the client sends are never re-placed.
- **Aliases.** `category_aliases (persona_id, path, category_id)`: after a merge or rename
  the old path keeps routing incoming episodes to the new category.
- **Placement of derived claims.** A claim derived from an episode lands in the episode's
  category or one of its existing descendants (the model sees that subtree). It may
  *propose* anything else by putting `proposed_category: "<path>"` on the pending claim
  (stored under the closest existing parent); approving it with `create_category: true`
  creates it.
- **Guard vocabulary.** Seeded categories and those the owner creates with
  `upsert_category` count as the persona's own vocabulary for the guard (§7.7); categories
  created from episodes do not, so a client cannot whitelist a name by naming a category
  after it.
- Nightly compaction merges synonym categories and splits large ones (§10.6).

### 4.5 claim

| Field | Type | Notes |
|---|---|---|
| `id` | `clm_…` | |
| `kind` | `trait \| preference \| habit \| decision_rule \| style \| boundary` | |
| `category` | path | |
| `text` | string ≤ 500 chars | one statement, owner in third person or neutral ("Short, no greeting…"); behaviour, not an identity label |
| `audiences` | `"all"` or list of audience names | where the claim **applies** |
| `visibility` | `owner_only \| shared` | who may **see** it through `predict`: `shared` claims reach the audiences they apply to; import maps `audiences` and `anyone` to `shared` (D1) |
| `status` | `pending \| approved \| rejected \| archived` | `archived` is added for compaction |
| `confidence` | 0..1 | |
| `evidence` | object | aggregate only, see below |
| `key` | string, nullable | stable key of an analyzer's claim: `builtin:<name>:<scope_id>[:<facet>]` (builtin, §7.2; a facet when one builtin renders several statements) or `anl:<id>` (LLM analyzer, §7.4); unique per persona among non-archived claims |
| `origin` | `derived \| added \| imported \| owner` | |
| `proposed_category` | path, nullable | |
| `superseded_by` | `clm_…`, nullable | set on the loser of a contradiction |
| `archive_reason` | `merged \| superseded \| decayed \| budget \| owner`, nullable | |
| `created_at, updated_at, reviewed_at, archived_at, last_evidence_at` | timestamps | `last_evidence_at` drives decay |

`evidence` never grows as a list:

```json
{
  "sources": {"statement": 0, "reply": 0, "decision": 3, "correction": 1, "analyzer": 212},
  "share": 0.87,
  "period": "2026-09-01..2026-10-31",
  "stats": {"n": 71, "hits": 62, "n_by_month": [40, 31], "hits_by_month": [35, 27]}
}
```

`sources` count the episodes behind the claim by class (§7.1; `analyzer` = episodes an
analyzer counted; `correction` = episodes that correct a prediction, counted there instead
of under their own class, D23) and are summed on merge; `period` widens. The **evidence
weight** used by contradictions and the size budget is
`confidence × log(1 + Σ weighted sources)`, where `correction` counts
`correction_weight` (3) times and every other class once. Context ordering in `predict`
and `get_persona` uses `confidence × (1 + log10(1 + Σ weighted sources))`, so a claim
without sources (written by the owner) still ranks by its confidence. `stats` of a keyed claim holds
the numbers its text was rendered from, as numbers or lists of numbers only (for an LLM
analyzer `n`, `hits` and their per-month lists over `period`); the
counters themselves live in `analyzer_state` (§4.6), so a re-run re-renders the keyed claim
by code and never duplicates it. LLM-derived claims have `sources` and `period` only.

Examples:
- style, audiences `[work-slack]`: "Short, no greeting, answers within minutes during work
  hours."
- decision_rule, audiences `all`: "Accepts a meeting only if there is a free slot of at
  least 20 minutes and 10 minutes of buffer before and after other calls."
- habit, key `builtin:time_activity:cat_…` (`work/meetings`): "No meetings on Friday
  afternoons (0 of 212 meetings in 6 months)."
- boundary: "Never discusses salary in writing." A `boundary` is a topic the owner does
  not want anyone (an auto-responder included) to answer for them at all; a rule that
  makes the owner decline something under a condition ("Does not take work calls on
  weekends") is a `decision_rule`, and `predict` answers it with the decline.

**Rejected claims are kept as negative memory**: a new derivation that matches a rejected
claim is dropped instead of re-entering the review queue. They are text-only (evidence
cleared) and count against no budget.

### 4.6 Other tables
- `changes`: `id, persona_id, ts, op (merge|absorb|supersede|decay|archive_budget|purge|
  restore|category_create|category_merge|category_rename|category_split), state (applied|proposed|dismissed|undone|
  expired), reason, job_id, payload` (before/after snapshots of the claims touched; these
  are derived claims, never raw input; category changes also snapshot the categories,
  aliases, analyzers and analyzer state they touch). Purge turns the entries for that claim into
  `expired` (not undoable).
- `jobs`: `id, persona_id, status (queued|running|done|partial|failed|interrupted),
  created_at, finished_at, counts` (episodes by class, claims created/absorbed/dropped_by_guard,
  categories created, `unmapped_categories`, `unplaced_episodes`, `corrections`,
  `unknown_predictions`, analyzer counters, skip counters), `error_code`, `remaining_records` (JSON array of batch indices not yet
  processed; written when a job ends `partial`). No content.
- `ingest_sources`: `persona_id, source, last_ts, last_job_at, records_total` — the
  per-source watermark of §7.1 (an owner-chosen slug, the largest processed episode
  timestamp, when a job last ended for it, and how many episodes it has processed in
  total). No content.
- `category_aliases`: `persona_id, path, category_id` (§4.4).
- `category_labels`: `persona_id, category_id, field (decision|situation|tag), label` —
  the guarded label and tag vocabulary of §7.3 (label = one path segment). Value → label
  mappings are never stored.
- `analyzers`: `id (anl_…), persona_id, scope (category_id | audience_id), origin
  (builtin|llm), name ≤ 200, statement_template, kind, source, status
  (candidate|active|disabled|retired), runs, failures, created_at, last_run_at, claim_id`
  (§7.2, §7.4). `source` ≤ 20 KB, empty for builtins (shipped code, found by `name`) and for
  `retired` LLM analyzers.
- `analyzer_state`: `analyzer_id, month (YYYY-MM), metric, key, value` — numbers only,
  keys filtered (§7.2).
- `audit`: `id (adt_…), persona_id, ts, client_name, grant_id|api_key_id, scope,
  audience_id, audience_fallback (bool), outcome (answer|escalate:<code>; `needs` only on
  rows written before D23), confidence, used_claims (JSON array of claim IDs, no
  text), llm_tokens, latency_ms`. Kept 90 days. The `id` is returned to the caller as `prediction_id` (§6.5) and is what a
  correction episode references (D23).
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
  "persona": {"display_name": "…", "timezone": "Europe/Berlin", "weekend_days": ["saturday", "sunday"]},
  "audiences": [{"name": "family", "description": "…", "is_default": false}],
  "categories": [{"path": "work/meetings", "description": "…"}],
  "category_aliases": [{"path": "films", "category": "movies"}],
  "category_labels": [{"category": "work/meetings", "field": "decision", "label": "declined"}],
  "analyzers": [{"origin": "builtin", "name": "time_gaps", "scope": {"category": "work/meetings"},
                 "status": "active", "state": {"gap_min": {"2026-10": {"10_15": 31}}}},
                {"origin": "llm", "name": "…", "scope": {"category": "hobbies/films"},
                 "statement_template": "…", "kind": "preference", "source": "…",
                 "status": "active", "claim": "clm_…", "state": {…}}],
  "claims": [{"id": "clm_…", "kind": "habit", "category": "work/meetings", "text": "…",
              "audiences": "all", "visibility": "shared", "status": "approved",
              "confidence": 0.8, "evidence": {…}, "key": "builtin:time_gaps:cat_…",
              "origin": "derived", "superseded_by": null,
              "created_at": "…", "updated_at": "…", "last_evidence_at": "…"}]
}
```

Builtin analyzers are exported by name with their state, LLM analyzers with source,
statement, status and state; scopes and claim links are by path, audience name and claim ID.
Excluded: LLM keys, limits, audit, change log, auth data, and raw data (none is stored).
Archived claims are included with `status: "archived"`. Import validates analyzer state
against the output contract (§7.2) and its keys against the guard's regex part (there is
no batch to test frequency against); LLM analyzers keep their status, since they only ever
run in the sandbox. The format is still v1: it is unreleased and was extended in place. Import modes: `merge` (default; existing IDs are updated, new ones
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
- A `respond` client reads the answer and its `depends_on` (§6.5), which paraphrases the
  claims applicable to the situation. Those are claims already visible to its audience,
  so it learns nothing the SQL filter (§8.3) did not let into the context; `owner_only`
  claims never reach `depends_on` unless the owner asked for them with `include_private`.
- The admin role grants no persona access. Admin is a web-page role only (§9.6).

---

## 6. MCP tools

All tools return structured JSON (`structuredContent` plus a text mirror). Errors use MCP
tool errors with a stable `code` (`not_found`, `forbidden`, `invalid`, `rate_limited`,
`spend_cap`, `too_large`, `conflict`). Pagination: `limit` (default 50, max 200) and an
opaque `cursor`. 30 tools; read-only resources are a later option.

Shapes below are abbreviated JSON Schema. `?` marks optional fields.

### 6.1 Ingest

**`ingest_raw`** (owner) — accepts a batch of episodes, returns at once, derives in the
background, then drops the input.
```
in:  { episodes: Episode[1..1000], llm_for_ingest?: bool,   // batch ≤ 2 MB serialized
       source?: slug }                                      // [a-z0-9][a-z0-9_-]{0,39}
out: { job_id, accepted: int, estimated_tokens: int, remaining_tokens: int | null }

Episode = { when?: datetime | { start: datetime, end?: datetime },
                                       // when the owner acted: a moment or a span
            situation_at?: datetime,   // when the situation reached the owner; needs situation
            where?: string ≤ 100,      // coarse context only: "home", "online", "work Slack"; never an address
            audience?: string,         // audience name or "default"
            category?: string,         // a category path, created if unknown; placed by the server if absent (§4.4)
            situation?: string ≤ 8000, // what happened / was offered / was said to the owner
            decision?: string ≤ 4000,  // what the owner did, described by the client
            said?: string ≤ 20000,     // the owner's verbatim words
            corrects?: string }        // a prediction_id (§6.5): this is what the owner actually does
```
Every episode has `additionalProperties: false` and at least one of `decision` / `said`.
`situation_at` and `corrects` need `situation`.
An unknown `audience` is mapped to the default audience and counted in the job's
`unknown_audiences` counter. Shapes (one type for every data source; the domain lives in
`category`, D17):
- a statement about self (the former note): `said`, no `situation`, no `audience`;
- an outgoing message the owner started (a dictated "tell them I'll be late", the first
  message of a thread): `said` + `audience`, no `situation`; it is a reply to that
  audience for every purpose (style, passes, analyzers) except latency;
- a chat reply: `situation` = the incoming message(s), `said` = the reply;
- a choice (meeting invite, film, purchase): `situation` + `decision` ("declined",
  "watched to the end, 4/5");
- a call: `situation` = what the others said, `said` = the owner's turns.

`situation` may contain others' words; `decision` is written by the client, not the owner.
Only `said` counts as owner text (stylometry, guard; §7.7). Episodes are text only:
images, audio and other media are described in words by the client (a voice message
becomes its transcript in `said`, a photo a short description in `situation`).

Time has one meaning per field (D23): `when` is when the owner acted, a moment (reply sent,
choice made) or a span (a meeting attended, a call); `situation_at` is when the situation
reached the owner (message received, invitation sent). Spans feed lengths and gaps;
`when` − `situation_at` is the response latency, in any category. An episode's timestamp
is `when` or `when.start`.

A **correction** (`corrects`) is the owner saying what they would actually have said or
done instead of a prediction: `situation` is the situation the prediction was made for,
`said` / `decision` the owner's version. It is derived like any episode of its shape and
weighs more (§4.5, §7.6); a `prediction_id` that is unknown, older than the audit
retention or from another persona is ignored (`unknown_predictions`) and the episode is
processed as an ordinary one.

`accepted` is the number of valid episodes received. `estimated_tokens` is a conservative
estimate of the LLM tokens the job will use (0 when no LLM step runs; §7.1); `remaining_tokens` is
what is left of today's `llm_tokens_per_day` for the user (0 when the spend cap is reached,
`null` when unlimited). With `source`, episodes already covered by that source's watermark
are skipped (§7.1).

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

**`job_status`** (owner) — `in: { job_id }` → `out: { status, progress: 0..1, counts,
error_code?, remaining_records?: int[], retry_after?: int }`. `status` is `queued |
running | done | partial | failed | interrupted`. `partial`: the job stopped early on the
daily token limit (`rate_limited`), the spend cap (`spend_cap`) or LLM unavailability
(`llm_unavailable`) after storing everything derived so far and running the analyzers on
the processed episodes; `remaining_records` lists the
indices (into the original batch) that were not processed, so the harness resends exactly
those; `retry_after` is the seconds to the daily reset (`rate_limited`) or the month end
(`spend_cap`). An `interrupted` job is resent in full. Counters include `skipped_seen` (episodes at or below the source watermark),
`skipped_saturated` (replies skipped by saturation), `saturated_audiences`,
`categories_created`, `unmapped_categories`, `unplaced_episodes`, `corrections`,
`unknown_predictions`, `analyzers_proposed` / `_accepted` / `_rejected`, `sandbox_errors`
and `skipped_sandbox_budget`.

**`list_sources`** (owner, read-only) — `out: { sources: [{ source, last_ts, last_job_at,
records_total }] }`. **`reset_source`** (owner) — `in: { source }` → `out: { deleted: bool }`;
drops that watermark so the next batch with this `source` is processed in full.

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
- **`list_categories`** → tree with `claim_count` and `aliases`.
- **`upsert_category`** — `in: { path, description?, rename_to? }` (depth ≤ 3); a rename
  leaves the old path as an alias (§4.4).
- **`merge_categories`** — `in: { from: [path], into: path }`; recorded in the change log;
  the `from` paths become aliases and their analyzers move to `into` (§10.6).

### 6.4 Compaction and logs (owner)
- **`compact`** — `in: { apply?: bool = false, steps?: ["dedupe","contradictions","decay",
  "budget","purge","category_merge","category_split"] }` → `{ changes: [{ id?, op, state, claims, reason }] }`. With
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
in:  { situation: string ≤ 8000, audience: string | "default",
       context?: string ≤ 2000,     // what the client found out; never stored
       kind?: "reply"|"decision", include_private?: bool }
out: one of
  { type: "answer",   answer, depends_on: string | null, confidence, disclosure,
                      used_claims: [id], hints: [string] }
  { type: "escalate", escalate: "low_confidence"|"out_of_persona"|"boundary"|
                      "rate_limited"|"spend_cap"|"llm_unavailable"|"audience_not_permitted",
                      reason, hints }
```
`escalate` is a reason code rather than `true`, so `rate_limited` and the rest share one
field (D6). Every result also carries `prediction_id` (the audit row's `adt_…` ID, §4.6;
absent only when no audit row is written). An owner client that learns the owner would
have answered differently sends a correction episode with `corrects: <prediction_id>`
(§6.1, D23).

**Answer and `depends_on`** (D23). wwyd answers like someone who knows the owner well but
cannot see their calendar, the weather or anything else outside the persona. `answer` is
always ready to send in the owner's voice and never asserts what wwyd cannot know (it
says "let me check and get back to you", not "15:00 works", when it does not know the
owner's time). `depends_on` is one or two plain sentences, from the owner's applicable
claims, on what the owner's actual answer depends on ("talks to friends after 18:00 and
keeps at least 5 minutes between calls"); `null` when the owner's applicable claims put
no condition on the answer, or the situation or `context` settles it. A `context` that
says the request cannot happen as asked ("tomorrow 15:00–15:30 is taken") is a fact the
owner goes by even when no claim names it: the answer is a final decline with `depends_on`
`null`, and one that also names what is possible ("only 16:30–17:00 is free") gives a
decline that suggests it; a request for a span that holds the free part ("between 15
and 17?") is accepted for that part (2026-10-27, §8.1 step 6). wwyd models the
person, not the world: `depends_on` lists only conditions that come from the owner's
claims, and wwyd never adds conditions of its own (free time, money, health) when no
claim makes the answer conditional. When no claim other than style is about the situation's
topic at all, the result is `escalate: "out_of_persona"` rather than a guess (§8.1 step
6). A `null` `depends_on` therefore does not mean the
client's own facts agree: a client that can see the calendar or other facts checks them
before sending `answer` as is, or passes what it found in `context`. While `depends_on` is not `null`, `answer` neither accepts nor
declines; the server checks this (§8.1 step 7) rather than trusting the prompt alone.
There is no structure in it and no domain vocabulary: the client decides how to
use it. A simple client sends `answer` as is; a client with its own LLM and tools refines
the answer from `depends_on`; a client that wants the final wording in the owner's voice
calls `predict` again with the same situation and what it found out in `context`, as a
conclusion in plain words, never raw data such as a schedule. Best is the direct answer
to the condition in `depends_on` ("14:55–15:25 tomorrow is free", "rain all day on
Saturday"): small models settle that reliably, while working the condition out from a
wider window ("free 14:00–16:00") they may get wrong or leave open.
The server keeps no state between calls.

### 6.6 Persona lifecycle and settings (owner)
- **`export_persona`** → `{ document }` (§4.7). Large personas are still small (budgeted).
- **`import_persona`** — `in: { document, mode?: "merge"|"replace" }` → counts. Validated
  against `persona.v1.json`.
- **`delete_persona`** — `in: { confirm: "<login>" }`. Deletes all persona content, audit,
  changes and jobs; the account and grants stay (a fresh empty persona is created).
- **`get_settings`** → display name, timezone, `weekend_days`, LLM backend (key masked), `llm_for_ingest`,
  `escalate_below`, disclosure template, effective limits and ceilings, today's usage,
  `spend_capped` flag, the plain-language note about where raw text goes.
- **`update_settings`** — any of the above that the user may change; limits only downward
  from the admin ceilings.

### 6.7 Analyzers (owner)
- **`list_analyzers`** — `in: { scope?: path | audience, status?, limit?, cursor? }` →
  `{ analyzers: [{ id, origin, name, scope, kind, status, runs, failures, last_run_at,
  claim_id }], next_cursor? }`
- **`get_analyzer`** — `in: { id }` → the row with `statement_template`, `source` (LLM
  analyzers) and a state summary: metrics, months, key count per metric, and the keys.
- **`update_analyzer`** — `in: { id, status: "active"|"disabled"|"retired" }` → `{ analyzer }`.
  Retiring clears the source and keeps the statement as negative memory (§7.4).

---

## 7. Ingest and derivation

### 7.1 Jobs
`ingest_raw` validates and size-checks the batch, writes a `jobs` row (counts only), puts
the episodes on an in-memory queue and returns. One worker per persona (limit
`concurrent_jobs`, default 1). The episodes are released when the job ends, in `finally`.
On restart, `running`/`queued` jobs become `interrupted` and the harness resends; there is
nothing to resume because nothing was saved (P1).

- **Preparation.** Before anything else the host converts every time to the persona
  timezone, resolves audiences and category paths (§4.4) and classifies each episode:
  *statement* (`said`, no `situation`, no `audience`), *reply* (`said` + `situation`, or
  `said` + an explicit `audience` and no `situation`: an outgoing message; `"default"` and
  an unknown name count as explicit), *decision* (`decision` + `situation`); an episode with both `said` and `decision` is a reply and a decision. New
  categories get their descriptions in one batched call first (§4.4). A `corrects` ID is
  resolved against the persona's audit rows here (D23); a resolved episode is also a
  *correction*, counted in `corrections`.
- **Order and progress.** 0. placement of episodes without a category (§4.4); 1.
  annotation (§7.3); 2. the LLM passes (§7.6): statements, replies per audience in
  chronological order, decisions; 3. analyzer authoring (§7.4); 4. the analyzers (builtin
  and active LLM, §7.2) over the *processed* episodes only. Step 4 always runs (also with
  no LLM step, no budget left, or after an LLM error stopped the LLM phase). Every parsed
  episode keeps its batch index, and each call knows the indices it covers. An episode is
  processed when every placement, annotation and pass call that contains it has
  completed; episodes in no such call (every episode when no LLM step runs) are
  processed. Description and authoring calls cover no episodes (authoring sees only
  processed ones). Remaining episodes therefore reach analyzer state only through their
  resend: each episode is counted once across a job and its resends. An `interrupted` job
  ran no analyzers and is resent in full.
- **Flush.** Candidates are deduplicated within a window and stored with absorb
  (`origin: derived`) every 20 LLM calls and once more when the job ends, including when an
  LLM error ends it; duplicates across windows are left to absorb and compaction.
  Compaction runs on all touched claims whatever the outcome.
- **Budget.** Each call's cost is estimated as (chars of system + user) / 3 (conservative
  for Cyrillic and most non-English text) plus a fixed output allowance per purpose. This
  is a conservative estimate, not a bound: the job stops before a call whose estimate does
  not fit, so the daily cap can be overrun by at most one call's actual excess. Before each
  call the job compares the estimate with what is left of today's token budget and with
  the spend state, besides the limiter's own check; a call that would not fit is not
  started. The job then ends `partial` (`rate_limited` / `spend_cap`) with
  `remaining_records`; so does an LLM error raised by the backend (`llm_unavailable`
  included, after the client's single transient retry, §12; it carries no `retry_after`,
  and the server never retries a job itself). A batch submitted with no budget left is still accepted: analyzers run and the
  job ends `partial` before the first call. Authoring and word-usage calls cover no
  episodes, so one that does not fit or fails is skipped (`skipped_llm_budget`,
  `authoring_failed`, `word_usage_failed`) and the job goes on. `failed` is kept for real
  errors. Sandbox time
  has its own daily budget (§7.5); running out of it skips LLM analyzers for the rest of
  the job (`skipped_sandbox_budget`) without ending it `partial`.
- **Saturation (replies only).** When 5 consecutive reply calls of one audience produce no
  new candidate (each was a duplicate of one derived earlier in the job or of an existing
  pending/approved/rejected claim: same kind and category; within the job equal normalised
  text or token Jaccard ≥ 0.6 with no differing numbers, against stored claims the same
  words in the same order (§10.1); no LLM judge), the audience's remaining reply calls are skipped.
  Their episodes count as processed (`skipped_saturated`, `saturated_audiences`).
  Statements and decisions are never skipped.
- **Source watermark.** With `source`, episodes whose timestamp (`when` or `when.start`)
  is ≤ the stored `last_ts` are dropped before derivation (`skipped_seen`); episodes
  without a timestamp are never skipped. When the job ends: `done` → `last_ts` = the
  largest processed timestamp; `partial` → the largest processed timestamp strictly below
  the earliest remaining episode's timestamp (so resending the remaining episodes is not
  skipped); `failed`/`interrupted` leave it alone. It never moves backwards.
  `records_total` adds the processed episodes. Only timestamps and counts are stored.

### 7.2 Analyzers
An analyzer is a stored program bound to a category subtree (or to an audience, for style)
that runs over the processed episodes of each job and produces only mergeable counters
(D19). Two origins:
- **builtin** — shipped, trusted code; runs natively in the host process. Rows are created
  lazily, one per (name, scope) the first time a job has episodes for that scope; they do
  not count against `max_analyzers`. A category-scoped builtin reads the episodes of exactly
  its category (not the subtree), so each episode is counted once per builtin; an
  audience-scoped one reads one audience, and episodes naming no audience count for the
  default audience.
- **llm** — written by the LLM (§7.4); runs only in the sandbox (§7.5).

**Input.** The processed episodes of the job in the analyzer's scope, as JSON (on stdin in
the sandbox), with host-computed fields: `local_start`, `weekday`, `hour`, `month`,
`duration_s` (the span of `when`), `latency_s` (`when` − `situation_at`), `labels` (§7.3),
plus `category`, `audience`, `where` and the text fields. A
shipped helper module `wwyd_helpers` (tokenize, sentences, emoji, punctuation, word and
bigram counting) keeps LLM code short; builtins use the same module.

**Output contract** (both origins): `{metric: {"YYYY-MM": {key: number}}}` with finite
numbers, keys ≤ 64 chars, ≤ 20 metrics, ≤ 200 keys per metric per month, ≤ 1 MB. The host
validates it, filters the keys, and merges the result into `analyzer_state` by addition.
Anything invalid fails the run (counted in `sandbox_errors` for LLM analyzers).

**Host key filter** (analyzer code is never trusted with privacy). Each metric declares a
key class; compound keys join segments with `.`, each checked by its class:
- `numeric` — buckets such as hours or histogram bins: only digits and `:_-.`;
- `vocab` — a value from host-known vocabulary: audience names, weekday names, the
  category's labels (§7.3), the reserved key `_`;
- `text` (the default, and the only class besides `_` an LLM analyzer may use for words) —
  kept only if it passes the guard's regex part (no emails, phones, URLs, handles), is not
  name-like (capitalised in a non-initial position in ≥ 50% of its non-initial occurrences
  in the batch; mixed case; contains a digit), and occurs in ≥ 5 distinct episodes on ≥ 3
  distinct days in the batch.
Dropped keys are counted, never logged. The filter's residual risk is stated in §2.

**Claims.** Each builtin produces keyed claims (`builtin:<name>:<scope_id>`, IDs so that
renames keep the key) whose text is rendered by code from the state (templated numbers).
Thresholds (initial, tunable in code): ≥ 30 samples, share ≥ 0.75 for a rule-like
statement, period ≥ 3 weeks. Re-runs update the numbers and text without the LLM; a keyed
claim whose numbers no longer pass weakens like a contradiction (approved → proposal,
§10.2). Each builtin is documented in `docs/habits.md` with its formula, thresholds and a
synthetic test.

| Builtin | Scope | State | Claim (example) |
|---|---|---|---|
| `time_activity` | category | weekday × hour histogram per audience | habit: "No `work/*` episodes on weekends", "Active 08:30–19:00 on weekdays." |
| `time_duration` | category | histogram of span lengths; response latency (`when` − `situation_at`) per audience | habit: "Meetings are typically 25 or 50 min." / style: "Responds within ~4 min." |
| `time_gaps` | category | same-day gaps between consecutive intervals | habit: "Keeps ≥10 min between calls (87% of 212 gaps)." |
| `stylometry` | audience | over `said`: length histograms (chars, sentences), share starting lowercase, sentence-ending punctuation (incl. `)))` and none), emoji frequency, word and bigram frequency (`text` keys) | style: "Writes to family in 1–2 short lines, lowercase, no final period." Keyness of the audience vs the owner's overall usage names favourite words. |

**Word usage notes.** When the favourite-words set of an audience changes, the host
extracts ≤ 5 short snippets (±5 words, from `said` only, in memory) per word and one LLM
call writes a usage-note style claim (`builtin:word_usage:<audience_id>`), guarded (§7.7).

### 7.3 Annotation (LLM labels, code counts)
Per category per job, run before the LLM passes (D20):
- **Few distinct values.** If `decision` has ≤ 50 distinct normalised values (or
  `situation` ≤ 200), one LLM call maps the distinct values to labels (decision →
  `accepted` / `declined` / `other`; situation → a type such as `1-1`, `standup`,
  `interview`).
- **Free text** with many distinct values (film synopses): batched tagging, ≤ 20 episodes
  per call, ≤ 5 tags each, reusing the category's tag vocabulary.
- Label and tag *names* are kept per category in `category_labels` (guarded; one path
  segment each) for consistency across jobs. The value → label mappings are not stored
  (they could contain names): annotation is redone per batch, cheap after dedupe. Labels
  are in-memory fields for the analyzers and the LLM passes.

### 7.4 LLM analyzers (hypothesis loop)
1. **Trigger:** a category with ≥ `analyzer_min_episodes` (100) processed episodes in the
   job; at most one authoring round per category per job; ≤ `max_analyzers` (50) LLM
   analyzers per persona, `retired` ones excluded. Since no episodes are kept, discovery
   needs a batch that is large on its own (a first import, or an occasional larger
   export); small increments only update the analyzers that already exist.
2. **Authoring:** one LLM call sees a host-computed profile of the category's episodes
   (counts, field fill rates, distinct counts, label/tag distributions, time histograms,
   length stats; aggregates only), ≤ 20 truncated sample episodes, the helper API, the
   names of existing analyzers (statements of retired ones included) and the category's
   claims. It returns ≤ 5 hypotheses, each `{statement_template` (placeholders such as
   `{share}`, `{n}`)`, kind, source}`. An LLM analyzer tests one hypothesis and outputs
   metrics `n` and `hits` per month (optionally `base_n` / `base_hits`), key `_`. Source
   ≤ 20 KB; source and statement pass the verbatim guard against the batch before
   storage, since the model saw samples (§7.7).
3. **Test:** each runs in the sandbox on the older 70% (by time) of the category's
   processed episodes and on the recent 30% holdout; test runs write no state. Accept if
   n ≥ 30, hits/n ≥ 0.75, a one-sided binomial test vs the base rate (base_hits/base_n,
   else 0.5) passes after Benjamini–Hochberg at q = 0.1 across the round's hypotheses, and
   the holdout share is ≥ 0.75 too.
4. **Accepted** → status `active` and a keyed claim `anl:<id>` (`pending`, as every derived
   claim) whose `evidence.stats` are n/hits per month and whose text is the template filled
   by code; it joins step 4 of this job (§7.1), which counts every processed episode once.
   **Rejected** → stored `retired` with its statement only (no source) as negative memory,
   so it is not proposed again.
5. **Later jobs** run every active analyzer over the new processed episodes, merge state,
   recompute the share over the last 12 months and re-render the claim. If the last 3
   months fall below the threshold, the claim weakens like a contradiction (approved →
   proposal, §10.2). 3 consecutive sandbox errors → `disabled`.

### 7.5 Sandbox
LLM analyzers run in wasmtime (`wasmtime>=49,<50`, wheels for Windows, macOS and Linux)
executing CPython 3.15 built for WASI (upstream `brettcannon/cpython-wasi-build` v3.15.0,
sha256-pinned) (D18).
- **Shipped in the py3-none-any wheel:** `python.wasm` with custom sections stripped
  (~7.8 MB) and a stdlib subset as guest-compiled `.pyc` in `python315.zip` (ZIP_STORED;
  the wasm has no zlib), plus `wwyd_helpers`: ~3.6 MB compressed. A build script fetches,
  verifies, strips and packs them at release/CI build time, never at install; the
  artifacts are gitignored. Tests needing the sandbox skip when they are absent locally;
  CI builds them.
- **Module cache:** the compiled module is cached in the data dir, keyed by (wasmtime
  version, engine config, wasm sha256). The first start compiles once (~0.7 s); later
  loads take ~2 ms.
- **Per run:** epoch timeout 10 s (ticker thread), fuel 5e9, memory 256 MiB, output 1 MB.
  One read-only preopen (the stdlib) and nothing else; no network; env only `PYTHONHOME`.
  Input is in-memory stdin bytes, never disk. Stdout is captured with the cap; stderr is
  discarded (tracebacks may contain input); only the exception type name (validated
  `^[A-Za-z_]\w{0,63}$`) is surfaced. No zoneinfo inside: the host does timezones.
- **Budgets:** global `sandbox_concurrency` (4); `sandbox_cpu_seconds_per_day` per persona
  (600, measured as run wall time) (§11).
- **Measured** (prototype, Apple M-series): trivial run 24 ms, stylometry over 5,000
  messages ~150 ms, fuel overhead ~17%.

### 7.6 LLM passes
Which episodes go to which pass (the token-saving switch):
- **(a) Statements** → claims of any kind (the former notes). Never skipped.
- **(b) Replies** → `style` / `boundary` / `decision_rule` / `preference` claims per audience: `said` with
  its `situation` as context, chunked to ~2500 chars per call, sampled with saturation
  (§7.1).
- **(c) Decisions** → `decision_rule` claims when the same pattern shows up at least twice
  in the batch, single decisions become low-confidence `preference` claims; only for
  categories with < `analyzer_min_episodes` episodes in the job. Larger volumes go through
  annotation and analyzers instead (§7.3, §7.4).
  Decisions are numbered in the prompt; for each claim the model returns `used_items`
  (the numbers of the decisions it follows, a key sorting after `text` so the model writes
  the rule first), and code counts the distinct valid numbers as the support. A claim's
  `sources` count and `period` come from those items' timestamps; a missing or
  non-list `used_items` is support 1 with the call's period. The prompt ends with "write the claims in the
  language of the input, not in the example's language": without it (and with an inline
  `e.g. [1, 3]`) a small model wrote English input's claims in the Russian of the worked
  example (2026-10-23).
- The model chooses categories within the episode's subtree or sets `proposed_category`
  (§4.4); labels (§7.3) are passed along.
- **Corrections** (D23) go to the pass of their shape, marked in the prompt as the owner's
  own correction of a prediction, and are never skipped by saturation. Their candidates
  count under `correction` in `sources`. The claims the corrected prediction used (the
  audit row's `used_claims`, still non-archived) are paired with each candidate derived
  from the correction and sent to the LLM judge (§10.1) whatever their Jaccard score,
  kind and category; `contradicts` resolves by evidence weight (§10.2), where the
  correction weighs `correction_weight` times. `same` merges or absorbs only when kind
  and category match; across them it is treated as `different`.
- Claim text (passes and LLM-analyzer statements alike) states what the owner does or
  prefers, never a label or identity ("Does not eat meat or fish.", not "Vegetarian."):
  small predict models misread labels as conditions.
- Results are deduplicated among themselves within a flush window (§7.1), then absorbed
  against existing claims (§10.1); only genuinely new ones become `pending`.
- With `llm_for_ingest: false` there is no description call, annotation, authoring or LLM
  pass; builtin and active LLM analyzers still run.

### 7.7 Verbatim guard
LLMs copy input. Every persisted derived text — claim texts, category descriptions, label
and tag names, analyzer sources and statements — is checked against the batch before it is
stored, and dropped (counted as `dropped_by_guard`) if it contains:
1. an email address, phone number, URL or `@handle` (regex);
2. any 5-word sequence that appears verbatim in a non-owner text in the batch;
3. a token that appears in a non-owner text of the batch **and** looks like an identifier
   or a name: contains a digit or mixed case, or is capitalised in a non-initial position,
   and is not in the persona's own vocabulary (audience names, seeded and owner-created
   category names (§4.4), display name) or a small built-in list (weekdays, months,
   languages);
4. for label and tag names, analyzer sources and statements and word-usage notes also: a
   word of the batch that occurs in < 5 episodes or on < 3 days (it may be a lowercase
   name; residual risk in §2).

Owner text is `said`; `situation`, `decision` and `where` are non-owner text (`decision`
is the client's description, not the owner's words). The guard is deliberately crude and
strict (D7). The privacy canary test (§13) runs both "token-like" and "name-like" canaries
through it.

---

## 8. `predict`

### 8.1 Flow
1. **Limits** (§11): per token, per persona, per audience. Over a limit → `escalate:
   "rate_limited"` with no LLM call.
2. **Resolve audience.** `default` → the default audience (§4.3). Unknown name → the
   default audience, `audience_fallback` in the audit row, and `hints` says so. For a
   `respond` token the resolved audience must be within its scope, otherwise
   `escalate: "audience_not_permitted"`.
3. **Load the visible set** in SQL (§8.3). If its total size is under ~1500 tokens, or it
   spans at most 3 categories (routing could not narrow it), skip routing and use it all;
   then one small decision call (purpose `when`, temperature 0, one-field JSON
   `{"when": "…"}`, max 64 tokens) extracts the situation's time instead (step 6). A failed
   call only costs the `When:` line.
4. **Route:** one cheap LLM call that sees only category paths and descriptions plus the
   situation, and returns 1–3 paths (JSON) plus `when`, the situation's time in canonical
   English (see step 6). If the call fails or returns nothing, fall back
   to FTS5 over the visible set with terms from the situation.
5. **Load context:** visible claims in the routed categories, plus the always-on core:
   `style` and `boundary` claims for that audience. Capped at a token budget, highest
   confidence × evidence first.
6. **Analyze, then answer** (two LLM calls; 2026-10-20). Every claim is shown under a
   short label (`c1`, `c2`, …), never its ID. Both calls get the current weekday, date and
   time in the persona's `timezone` (`Now: Monday 2026-10-19 18:40 (Europe/Berlin)`;
   default and fallback UTC; set it with `update_settings`), against which relative dates
   in the situation and context are resolved; the time is not stored or audited.
   **The model never computes dates; code does** (a small model miscounted "tomorrow" and
   "the day after tomorrow" into the wrong weekday, also with a lookup table of the next
   days; 2026-10-22/23). The model only extracts the time the situation refers to and
   translates it, from any language, into a small canonical English form (`today`,
   `tonight`, `tomorrow 17:00`, `day after tomorrow`, `sat`, `next thu 15:00`,
   `this weekend`, `next weekend`, `next week`, `this week`, `by fri`, `by next fri`,
   `in 3 days`, `in 2 weeks`, `2026-10-30`, optional `HH:MM`, `5 pm`, `noon`, `midnight`, `morning`/`afternoon`/`evening`/`night`, or a range of two such points (`15:00-17:00`, `between 3 and 5 pm`, `from 3pm to 5pm`, `10:00 to 12:00`, `15:00 17:00`; bare hours allowed inside one, `15-17`; an am/pm on one end goes to a bare other end unless that reverses the range; stored as `15:00-17:00`). When the time part fails but a leading day parses and the rest is a time attempt (a digit, am/pm or part-of-day word) with no day word and no `or`, the day is kept without a time (2026-10-27: an interval dropped the whole line and the model then read a Wednesday as a weekend);
   `""` when there is none; `overmorrow` is accepted for `day after tomorrow`). The
   routing call carries it as `when`; when routing is skipped, the small `when` call of
   step 3 does (an analyze field was tried first: the analyze call could not see its own
   result and kept miscounting, 78% vs 95% on the date cases, 2026-10-23). Code (`predict/when.py`)
   resolves it in the persona timezone against today: a bare weekday and
   `this <weekday>` are its next occurrence counting today (`sat` asked on a Saturday is
   today); `next <weekday>` is the next occurrence strictly after today (on a Thursday
   `next thu` is a week later, on other days it equals `thu`); `this weekend` is the
   coming block of weekend days (starting today when today is one), `next weekend` the
   block after it; `next week` is the following Monday to Sunday. A week always runs
   Monday to Sunday, whatever `weekend_days` says. A deadline or period is never a
   weekend: `this week` (also for "by the end of the week") is a deadline on this
   week's Sunday, `by <day>` a deadline on the single day the rest resolves to; a
   deadline covers today to that day and shows no `When:` line (a `by <day>` line was
   read as a constraint, 2026-10-26) ("до кінця тижня" had become
   `this weekend`, and the analyze call looped on "a free week is not a weekend" until
   it ran out of tokens, 2026-10-23). Anything outside this
   grammar gives no line (never a wrong date). The result is one line,
   `When: Saturday 2026-10-24 (weekend)`, the date or range only: the time is parsed but
   not shown, since the situation carries it and a second canonical time confused the
   model's comparisons (2026-10-26). Every day is marked per the persona's
   `weekend_days`, `(weekend)` or `(working day)`: with weekend-only marks the model read
   an unmarked named weekday ("on Friday" asked on a Monday) as a weekend (2026-10-26).
   A range is marked `(weekend)` or `(working days)` when all its days are alike and
   carries no mark when mixed. The prompts keep their one-sided wording ("only a day
   marked (weekend) is one"): rewording them two-sided made the analyze call rule a
   Tuesday call out under a weekend-only claim it had itself called inapplicable
   (2026-10-26). The line goes to both the
   analyze and the answer call. The prompts say to trust it
   for the day and whether it is a weekend, never to recount, that it only dates the
   situation (whether a claim applies depends on what the situation asks, never on the
   day), and the answer never gives
   a reason it contradicts. The
   client's `context` line comes after the situation. Both system prompts are static and
   byte-identical across calls (prefix cache); everything variable is in the user
   message. Their worked examples are set in unusual areas so a small model cannot copy
   them onto common inputs.
   - **Call 1, `analyze`** (internal, never returned, stored or logged): input the
     claims, `Now`, situation and context. Strict JSON whose keys sort into generation
     order for alphabetical JSON grammars: `a_check` (a one-line scratch note that first
     names what the situation asks the owner to do and then compares it only with claims
     about that same thing; a limit on another activity at the same day and hour does not
     apply, which one worked example and one contrastive pair show), `b_applicable` (labels of the claims about the situation's topic, style
     claims excluded; a claim that is a short label for the owner, such as "Early
     riser.", is read as the behaviour it implies), `c_excluded_by` (the label of an applicable claim that excludes the
     request, else empty; asked before the verdict because one excluding claim settles
     it even when another would make the answer conditional), `d_verdict` (`none` |
     `boundary` | `ruled_out` | `open` | `settled`), `e_label` (ignored by code, see
     below) and `f_depends_on` (only for `open`: the applicable claims' conditions applied to this
     situation; this becomes the public `depends_on`) and `g_language` (the ISO 639-1 code
     of the situation's language as the model reads it, late so it does not disturb the
     generation order of the others; normalised in code to a 2-3 letter code, otherwise
     dropped). Conditions come only from the
     claims; the model never adds its own (free time, money, health) (D23, 2026-10-20
     note). The context is a checked fact the owner goes by, even when no claim names
     it: when it says the request cannot happen as asked (that time is taken, the thing
     is gone, the place is closed), the verdict is `settled` with `a_check` concluding a
     decline, and when it also says what is possible instead, a decline suggesting that;
     a time the context calls taken is never accepted; when the request itself names a
     span of two times and the context calls only a part of it free, it is possible as
     asked and accepted for that part (2026-10-27: without the span sentence "between 3
     and 5 pm" with "only 16:00–17:00 is free" was declined; without "the rest is taken"
     in its example "only 16:30–17:00 is free" read as "free except 16:30–17:00"; and
     without "never accepted" and "two times" the claim's own 10–19 window was taken for
     the span and a taken 15:00 accepted);
     a condition the context already answers is not `open`. This is a static `Note:` paragraph
     appended to the analyze user message after the `Context:` line only when a context
     is given; the system prompt and every call without context stay unchanged, because
     editing the system prompt shifted unrelated no-context scenarios (2026-10-27: before
     this the model only let context interact with a claim's condition, so "15:00 is
     taken" either was ignored and the answer accepted, or became an invented `open`
     condition "if free").
   - **Code decides:** the deciding label is never taken from the model: for `ruled_out`
     it is `c_excluded_by` (else the first applicable claim), for `boundary` the first
     applicable `boundary` claim (a `settled` verdict with a stated `f_depends_on` stays
     `settled`: turning it into `open` cost holdout, 2026-10-23). Labels not shown and style claims are dropped from the applicable
     set; a valid `c_excluded_by` makes the verdict `ruled_out`. This holds for a
     `boundary` verdict too when the excluding claim is not itself a `boundary` claim (only
     `c_excluded_by` counts; an `e_label` alone does not, since it may name a rule that
     allows the request), and also when the model names the boundary claim itself as
     excluding while an applicable non-boundary claim in the same category is listed (the
     model tends to pick the boundary claim even when the refusal rule sits next to it): a
     refusal rule beats a boundary verdict, and the owner declines politely (e.g. salary
     is a boundary topic, but "does not discuss salary with recruiters" as a decision
     rule is answered with the decline; 2026-10-22).
     No applicable claim (or verdict `none`) → `escalate: "out_of_persona"`: wwyd
     models the person, and a situation no claim other than style covers (an opinion, a
     favour, a plan) is not guessed. `boundary` → `escalate: "boundary"` when the
     deciding claim (or another applicable one) is a `boundary` claim on the topic: a
     boundary claim that is really a refusal rule is a `ruled_out` verdict and is answered
     with the decline. Otherwise (no boundary claim) it is treated as `settled`. An `open`
     verdict without a condition is `settled`. Neither escalation makes the second call.
   - **Call 2, `answer`:** the owner's display name, the audience description, the style
     claims plus the applicable claims, the kind, `Now`, `Answer language: <code>`
     from the analyze call's `g_language` (no line when it gave none; there is no
     per-language code or word list, so every language is handled alike; 2026-10-22), situation, context, for `open` a `Depends on:` line, and a server
     instruction by verdict: `open` → non-committal in the owner's voice, neither accept
     nor decline (a condition about the owner is one the owner will check; only what the
     other person can know is asked of them); `ruled_out` → decline in the owner's voice (may suggest or ask about what
     the claims allow, never state that the owner is free or able to do it: the
     owner's calendar and plans are unknown); `settled` → answer as the claims and context decide,
     with the analyze call's `a_check` passed as an internal `Analysis:` line the answer
     must follow (only for `settled`; never returned, stored or logged). A `Case:` line
     comes from the stylometry profile only and appears only when the profile decides it
     (lowercase start when the profile's lowercase-start share is ≥ 0.8, a capital start
     when it is ≤ 0.2); otherwise there is no line, the answer uses the language's normal
     capitalisation unless a style claim says lowercase (style claim text is never parsed).
     The answer is in the owner's first person in every language (claims are third
     person). For `kind: reply` the
     approved stylometry claims visible to the resolved audience also contribute explicit
     parameters: typical length, lowercase-start share, ending punctuation, emoji habits,
     favourite words and phrases (D22). Strict JSON: `answer`, `language`, `z_commits` (boolean: the answer accepts, agrees, promises or
     declines; internal, never returned, stored or logged).
7. **Post-process:** first the re-ask guards on call 2, at most one re-ask in total,
   carrying the note of each guard that fired (tokens count): the **commit guard**
   (verdict `open` and `z_commits: true`; if the re-asked answer still commits, the
   result is `escalate: "low_confidence"`, "the answer committed although it depends on
   something unknown"), the **decline guard** (verdict `ruled_out` and `z_commits:
   false`: the answer did not decline; the re-asked answer is accepted either way) and
   the **language guard**, from universal signals only: the
   dominant Unicode script of situation and answer differ (Latin, Cyrillic, CJK with
   kana, Hangul, Greek, Arabic ..., from `unicodedata`; no verdict under 4 letters or
   without an 80 % majority), or the answer's `language` field differs from
   `g_language`. After the re-ask a remaining language mismatch is accepted (a claim
   such as "always writes in Russian" keeps working). Call 1 is never re-asked. Then an
   empty answer → `escalate: "low_confidence"`; `used_claims` is set by code: the applicable
   claims, then the style claims shown, then the stylometry claim whose profile was
   applied; `confidence` is computed by code (the model gives neither):
   `0.5 + 0.5 × min(1, rank)`, `rank` the context rank (confidence × (1 + log10(1 + Σ
   weighted sources))) of the deciding claim (the excluding one for `ruled_out`, else the
   strongest applicable one), +0.05 when two or more claims apply and none excludes,
   −0.05 for `open`,
   −0.1 when a guard re-asked, clamped to 0..1 and rounded to 2 decimals (a claim at the
   default 0.5 without evidence gives 0.75; one below 0.2 without evidence escalates at
   the default threshold); `confidence < escalate_below`
   (default 0.6) → `escalate: "low_confidence"`; add the `disclosure` (by the answer's
   `language`, else `g_language`; templates exist for en, ru, de, uk, es, fr, any other
   language gets the English one unless the owner set `disclosure_template`); write the audit row (tokens of all calls summed,
   no text); count usage.
8. **Style post-processing** (reply only, only where the audience's profile is strong,
   share ≥ 0.8): lowercase the first letter if the owner starts lowercase, capitalise it if
   the owner starts with a capital (lowercase-start share ≤ 0.2); drop a trailing
   period if the owner rarely ends with one. Before that, shorten in code: strip emoji
   (Unicode emoji detection) when the owner's emoji share is ≤ 0.05, and keep at most the
   owner's p90 sentence count (when no kept sentence is a question, the first cut question
   is kept too if the result fits the p90 length). Only if the result is still longer than
   3× the owner's p90 length, ask once to shorten (counts against limits), else keep it.
   The style parameters (step 6) leave out the case and final-period lines where these
   rules enforce them (a decisive case is stated by the `Case:` line). Typos are never
   injected.

`depends_on` is how a decision rule reaches the client: the rule "accepts a call only with
5 minutes free before and after" becomes "agrees if 14:55–15:25 is free; otherwise offers
the nearest slot with the same margin" for this situation. It is not styled (§8.1 step
8) and is cut to 400 chars.

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
  `/register` (DCR) and `/.well-known/oauth-authorization-server`, with S256 PKCE
  verification done by the SDK.
- `/revoke` (RFC 7009) is our own handler, replacing the SDK's (which demands a
  `client_secret` field even from public clients). Clients authenticate per their
  registered method (`none`: `client_id` only; `client_secret_post`/`_basic`: with the
  secret, else 401 `invalid_client`); an access or refresh token is revoked only if it
  belongs to that client. A `token` that is a wwyd API key revokes that key, with no client
  authentication needed (possession proves the right; constant-time check). Unknown tokens,
  garbage keys and other clients' tokens get 200 with nothing revoked. Throttled like
  `/token`.
- **Redirect URIs:** plain `http` redirect URIs on non-loopback hosts are accepted by
  default (LAN deployments), and the consent page then warns that the authorization travels
  unencrypted. With `[auth] allow_insecure_redirects = false`
  (`WWYD_AUTH_ALLOW_INSECURE_REDIRECTS`), DCR rejects them with `invalid_redirect_uri` and
  CIMD documents containing them are rejected; only `https` and loopback `http`
  (`127.0.0.1`, `::1`, `localhost`) remain. Recommended for public hosted instances.
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
  `Authorization: Bearer`. A key can also revoke itself: `POST /revoke` with `token=<key>`.
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
- Duplicate without the judge only when both texts have the same words in the same order
  (casefolded `\w+` tokens; they differ only in punctuation, case or spacing). Any other
  difference may flip the meaning (a negation, a number, swapped words) in any language,
  and no per-language word list decides it: every other pair with Jaccard ≥ 0.3 → one
  batched LLM judge call per category: `same | contradicts | different`. `add_claims` has
  no LLM, so it absorbs (or drops against a rejected claim) only such same-word
  duplicates: anything else lands as pending and compaction judges the pair later. On any
  LLM error the pairs are skipped (counted).
- The LLM-free Jaccard ≥ 0.6 duplicate tests elsewhere (within one ingest job, §7.1;
  authored analyzer statements, §7.5) never merge two texts when a word with a digit
  (an amount, a decimal, an `HH:MM` part) occurs in only one of them.
  `compact(apply=false)` does not call the judge (it would record usage); it reports the
  pairs it would send as `judge_pending`.
- Keyed analyzer claims (§7.2, §7.4) merge by `key` without any of this.
- **pending + pending:** merge into one pending claim (newer text, summed evidence).
- **new derivation + approved:** **absorb**: the approved claim keeps its text, evidence is
  summed, `last_evidence_at` and confidence go up (`c = max + (1 − max) × min × 0.5`,
  capped at 0.99). The review queue only grows with new information.
- **new derivation + rejected:** drop it.
- **approved + approved:** a `proposed` merge the owner approves via `review_claims`.

### 10.2 Contradictions
Pairs judged `contradicts`: the higher evidence weight (`confidence × log(1 + Σ weighted
sources)`, §4.5) wins; on a tie, the newer `last_evidence_at` wins. The loser is
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

### 10.6 Categories (nightly)
Both steps apply automatically, each with a `changes` row, and are undoable (D21).
- **`category_merge`:** for the categories created since the previous run, code proposes
  candidate pairs against all categories: the same leaf name under different parents, leaf
  names one edit apart (two for leaves of 8+ characters; leaves under 4 characters only
  when equal), or description token Jaccard ≥ 0.5 (both with ≥ 3 tokens); pairs the merge
  rules refuse are not proposed; at most 50. One batched call confirms them (`same:
  true|false` per pair). One open-search LLM call over paths and descriptions then finds
  synonyms code cannot see, including across languages (`films` / `movies` / `кино`),
  but only for the new categories no candidate pair covered, so a run makes at most two
  calls and the open search is skipped when every new category was proposed. The merged category's claims, analyzers
  and analyzer state move into the target (same-name builtins add their state), and its
  path becomes an alias.
- **`category_split`:** a category with more than `split_threshold` (20) non-archived
  claims, below depth 3: one LLM call groups its claims into 2–5 subcategories (guarded
  names and descriptions) within depth 3, and the claims move there. Partial answers are
  kept: a group with an invalid name or description is dropped, a claim listed twice keeps
  its first group, unknown labels are ignored, and unassigned claims stay in the parent;
  only a grouping with fewer than two valid non-empty groups (or more than five) is
  discarded (`discarded_splits`). Analyzers stay bound
  to the parent (their subtree still covers the claims).
- `merge_categories` and renames by the owner write aliases the same way. Undo restores
  the snapshot (categories, aliases, claims, analyzers, state) and deletes categories the
  change created if they are empty.

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
| batch size | per call | 1000 episodes / 2 MB |
| concurrent jobs | per persona | 1 |
| spend cap | per persona per calendar month | $5 on paid backends; none on local |
| compaction size budget (§10.4) | per persona | 30 claims per category / 6000 approved tokens |
| categories (`max_categories`) | per persona | 200 |
| new categories per job (`new_categories_per_job`) | per persona | 20 |
| LLM analyzers (`max_analyzers`) | per persona | 50 |
| sandbox CPU seconds (`sandbox_cpu_seconds_per_day`) | per persona per day | 600 |
| LLM concurrency | global | 2, queue timeout 60 s, call timeout 120 s |
| sandbox concurrency (`sandbox_concurrency`) | global | 4 |

The four new per-persona rows are per-user limits (admin ceilings, own lower values, like
the compaction budget); the concurrency rows are global config. `analyzer_min_episodes`
(100) and `split_threshold` (20) are instance config thresholds, not limits.

- RPM uses in-memory token buckets; daily counters live in `usage_daily`.
- Over a limit, `predict` returns `escalate: "rate_limited"` with no LLM call; other tools
  return a `rate_limited` error with `retry_after`.
- **Spend:** cost per call is taken from the backend's reported cost when it returns one
  (OpenRouter), otherwise from per-1M-token prices configured on the backend. Reaching the
  cap stops LLM calls for the persona (`escalate: "spend_cap"`, ingest jobs end `partial`
  with `spend_cap`) and sets a flag shown on the account page and in `get_settings`.
- **Ingest and the token budget.** The token budget is checked before every LLM call of an
  ingest job, and a job that runs out stops `partial` with `remaining_records` (§7.1)
  instead of failing. The default byte limit (20 MB/day) is far above what 500 000
  tokens/day can derive (a 2 MB batch of replies is roughly 1M tokens), so harnesses
  should size batches from `ingest_raw`'s `estimated_tokens` and `remaining_tokens`.
- **Usage counters** per user per day (calls per tool, LLM tokens in/out, cost, ingest
  bytes and episodes, sandbox seconds) are kept for future billing and shown to the admin.

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
wrap bare arrays; sanitise chat-template markers in input; generous `max_tokens` (at
least 1024) off the predict path, while the predict calls (`route`, `when`, `analyze`,
`answer`, `shorten`) get exactly the small budget their caller sets (analyze 384, a reply
384, a decision 640, `when` 64), so a greedy repetition loop is cut after seconds; a reply
that does not parse gets one repair retry, which echoes the bad reply back, except when
the reply was cut off at `max_tokens` (`finish_reason: "length"`): then the request is
sent again without the echo and with a "keep every text field short" note, since the
changed input breaks the loop and an echo would feed it (JSON-schema `maxLength` is not
enforced by LocalAI's grammar, measured 2026-10-23, so it is not relied on); every
field the model must emit is `required` in the JSON shape we ask for; a few worked examples
per prompt, including in Russian, since rules leak language through examples. Preflight
`GET /models` when a backend is saved. Decision calls (`route`, `analyze`, `judge`) run at
temperature 0 so the same input gets the same decision; all other calls use 0.2.

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
codes. A transient failure (connection error, HTTP 502/503/504) is retried once after a
~2 s pause taken outside the concurrency gate; read timeouts are retried the same way only
for ingest-side purposes, never for predict (`route`, `analyze`, `answer`, `shorten`, `judge`), where
a second 120 s wait costs more than failing. Only then does the call raise
`llm_unavailable`.

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
- **Builtin analyzers:** synthetic episodes with known ground truth (meetings, replies,
  statements), including timezone and DST edges, below-threshold samples, and state
  merging across runs and across a partial job and its resend.
- **Sandbox isolation** (on Linux, macOS and Windows in CI): file read, write and preopen
  escape, sockets, processes, env, infinite loop stopped by fuel and by epoch, memory
  bomb, oversized and invalid output, forged exception names.
- **Output contract and key filter:** limits, non-finite numbers, key classes, canary names
  both capitalised and lowercase-but-concentrated.
- **Hypothesis loop** with a fake LLM: trigger, holdout split, binomial test and
  Benjamini–Hochberg as unit tests, acceptance, retired negative memory, weakening,
  disabling after 3 errors.
- **Annotation:** few-distinct mapping, batched tagging, vocabulary reuse, no mapping
  persisted.
- **Organic categories:** normalisation, create (with limits and `unmapped_categories`),
  alias routing, merge, split, undo.
- **Style in predict:** parameters in the answer prompt, each post-processing rule and its
  threshold, the shorten re-ask.
- **Compaction:** dedupe, absorb, rejected-as-negative-memory, contradictions, decay,
  budget, proposals for approved claims, undo, purge.
- **Privacy canary:** ingest episodes containing unique strings (token-like
  `zq7x-canary-41b9`, name-like `Velmorine`, and a lowercase name used often) in every
  field, with a fake LLM that deliberately echoes its input (also into analyzer source,
  statements, category descriptions and labels) and with the real local model; then assert
  none of them appears in the DB file (including `-wal`; claims, analyzer source, state and
  statements, category descriptions, label vocabularies), the logs at DEBUG level,
  `export_persona`, `get_persona`, `get_analyzer`, or any `predict` output.
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
  `docs/habits.md` (builtin analyzers); `docs/ecosystem.md` (guidance for harness authors: messengers verified
  against primary sources, contact → audience mapping, transcription, how to shape
  episodes without names); `docs/eval.md`.
- **Sandbox artifacts:** built by `scripts/build_sandbox.py` at release/CI time into
  `src/wwyd/sandbox/assets/` (gitignored), packed into the wheel (§7.5).
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
  ingest/      jobs.py episodes.py guard.py passes.py annotate.py
  analyzers/   contract.py keyfilter.py state.py authoring.py hypothesis.py builtin/*.py
  sandbox/     runner.py cache.py helpers/wwyd_helpers.py assets/ (built, gitignored)
  compact/     dedupe.py contradictions.py decay.py budget.py categories.py scheduler.py
  limits/      limiter.py spend.py
schema/persona.v1.json
docs/  deploy/  scripts/  tests/
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

7. **Episodes and analyzers** (design revision D17–D22):
   - **7a:** episode schema, organic categories (create, aliases, merge, split, undo),
     builtin time analyzers natively, `analyzer_state`, analyzer tools. Demo: a synthetic
     calendar and chat export as episodes produce keyed habit claims and new categories.
   - **7b:** sandbox with the build script and CI matrix, stylometry, style in `predict`.
     Demo: a chat export yields a style profile and a reply draft that follows it.
   - **7c:** annotation, LLM analyzers and the hypothesis loop. Demo: a film-watching log
     yields an accepted analyzer with a pending claim, plus a retired hypothesis.
8. **Client contract revision** (D23): optional `category` with server placement,
   `situation_at`, `prediction_id` and correction episodes, `depends_on` and `context`
   instead of `needs` and `facts`, a minor version bump for breaking changes. Demo:
   uncategorised episodes land in organic categories; a correction of a prediction
   supersedes the claim that produced it; a reply draft comes with `depends_on`.

Outward-facing steps (GitHub repo, PyPI, container registry, domains, OAuth app
registrations) wait for the maintainer's go-ahead each time.

---

## 17. Decision log

Design decisions taken when this spec was approved (2026-10-18), with the reasoning.
D16 is a later revision; D17–D22 are the 2026-10-19 revision (approved the same day); D23
is the client contract revision of the same day.

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

**D16. Ingest jobs stop `partial` and persist progress (2026-10-19).** A large batch can
cost more tokens than a day's budget; failing it threw away everything derived so far.
Jobs now flush candidates every 20 calls and on the way out, and report which batch
indices were not processed. A job is accepted even with no budget left (detectors still
run, the harness gets `remaining_records` and `retry_after`) rather than rejected, so one
resend loop handles every case. Only indices, timestamps and counts are stored (P1): the
remaining records are resent by the harness, never kept by the server, so the server
never retries a job; only a single LLM call is retried once on a transient error (§12).
Saturation is
style-only because style repeats across thousands of messages, while each note and
decision can carry a rule seen nowhere else; it uses the cheap absorb similarity (no LLM
judge) so it costs no tokens. Detectors run after the LLM phase and only over processed
records, so a partial job plus the resend of its remaining records adds every record to
the habit stats exactly once (running them first would count the resent ones twice). The
token estimate uses 3 chars per token and is called conservative, not an upper bound:
tokenisation varies by model and language, and the one-call overrun it allows is small.
The per-source watermark lets a harness re-export
incrementally without re-deriving what was already seen. (§7.1 restates this in episode
terms; the detectors became builtin analyzers, D19.)

**D17. Episodes replace typed records (2026-10-19).** Typed records (`message`,
`call_transcript`, `calendar_event`, `decision`, `note`) covered a fixed set of sources; a
film log or a purchase history fitted none. One `Episode` shape takes any data type and
moves the domain into `category`. `said` separates the owner's verbatim words from the
client's descriptions (`decision`) and others' words (`situation`), so stylometry and the
guard know exactly which text is the owner's. Intervals in `when` replace the batch-local
`thread` pairing for reply latency.

**D18. Sandbox: wasmtime + CPython for WASI.** LLM-written code must run with no file,
network, process or env access, on every platform the server installs on, without extra
infrastructure. wasmtime is a pip dependency with wheels for Windows, macOS and Linux and
runs in-process with fuel, epoch and memory limits. Rejected: RestrictedPython (not a
security boundary); hosted sandboxes (the data would leave the machine); Docker, nsjail,
gVisor (platform-specific or extra infrastructure); NVIDIA OpenShell (gateway plus a
container runtime, aimed at long-running agents, Windows only via WSL).

**D19. Counters only, keys filtered by the host.** Analyzer output is numbers keyed by
short strings, merged by addition, so state stays small, mergeable across partial jobs and
resends, and holds no text. Privacy never depends on analyzer code: the host validates the
contract and filters every key (regex, name-likeness, spread across episodes and days).
The residual risk of lowercase names is stated in §2.

**D20. The LLM annotates, code counts; hypotheses are tested.** Small models count badly
but label well, so the LLM maps distinct values to labels and code does the statistics.
LLM-proposed regularities are treated as hypotheses: tested on older data and confirmed on
a recent holdout, with a binomial test against the base rate and Benjamini–Hochberg
false-discovery control across each round, following standard practice for multiple
hypothesis testing. Rejected hypotheses are remembered so they are not re-proposed.
Discovery runs only on batches large enough by themselves, because P1 rules out keeping
episodes to test against later; established analyzers then need only increments.

**D21. Organic categories, applied automatically with undo.** Clients know their domain
better than a fixed tree, so unknown paths are created; limits keep the tree bounded.
Merges and splits apply without review because they move claims without changing them,
every step is a `changes` row and undoable, and aliases keep old paths routing.

**D22. Style in `predict` as numbers plus deterministic post-processing.** Small models
ignore "write briefly"; explicit parameters and a few code rules (lowercase or capital
start, no final period, one shorten re-ask) are reliable and testable. Typos are never injected: they would
imitate the owner's mistakes, not their style.

**D23. Client contract revision (2026-10-19, M8).** Feedback from the first client after
the move to episodes: anything that makes a client know wwyd's domains, or invent its own,
works against one generic model for every area of life.
- *Optional category.* Requiring `category` made every client invent a taxonomy, which
  small local models do badly. The server already has an LLM for derivation, so it places
  uncategorised episodes into the same organic tree (§4.4) under the same limits and guard.
- *`situation_at`.* An interval used to mean either a span or reply latency, told apart by
  the episode's shape, so a call (situation + said) with a span was misread as latency.
  Each time field now has one meaning: `when` is the owner's action, `situation_at` the
  arrival of the situation, and latency becomes general (replies, invitations, any
  category).
- *Corrections.* The owner correcting a prediction is the most informative signal there
  is, but it could only arrive as an unlinked sample. `prediction_id` (the audit row ID,
  which already existed and carries no text) lets an episode reference the prediction; the
  audit row gains the used claim IDs, so the correction is judged against exactly the
  claims that misled, and it weighs more than a fresh sample.
- *Versioning.* Before 1.0 every breaking change on `main` bumps the minor version, also
  between releases, so a client's version gate catches it. 0.1.0 was never released; the
  first release is 0.2.0.
- *`depends_on` instead of `needs`.* Asking the client for facts made both sides share
  a domain vocabulary (`needs[].kind`, then category hints, conditions and slots: each
  one formalised a piece of the world) and gave no answer in the owner's voice until a
  second call. Between two LLM agents the generic interface is plain language: wwyd always
  returns a sendable answer that asserts nothing it cannot know, plus one or two sentences
  on what the real answer depends on, and the client resolves them with whatever it has.
  `facts: [{what, value}]` becomes a single `context` string for a client that wants the
  final wording in the owner's voice.
- *No invented conditions (2026-10-20).* A prompt rule "a request for a specific time
  always depends on whether the owner is free" was tried after live tests and dropped as
  domain-specific: `depends_on` carries only the conditions of the owner's applicable
  claims, and the client checks its own facts (calendar, balance, ...) before sending
  `answer` as is.
- *No claim, no guess; analyze before writing (2026-10-20).* With no applicable claim a
  small model invented reasons for favours ("help me move"); such a situation is now
  `out_of_persona`. Live runs on an 8B model also showed one call doing both the
  decision and the wording copying worked examples, answering English in German, and
  escalating `out_of_persona` despite an applicable claim. predict therefore splits into
  an internal `analyze` call (applicable claims and a verdict, internal fields only) and
  an `answer` call that only writes the message under a server instruction; the
  escalations are decided in code, and the answer language is detected in code (since 2026-10-22 the analyze call
  names it and code only compares Unicode scripts, §8.1). The
  client contract is unchanged.
- *Exclusions* are stated in the storage model's terms: behaviour and preferences from any
  area of life (money, health included) are welcome, including thresholds the owner
  applies; records about the owner (diagnoses, balances, amounts, account data) are not
  (`docs/ecosystem.md` §5).
- *Outgoing messages.* A message the owner started had no shape: clients sent it as a reply
  with an invented placeholder situation. `said` + `audience` without `situation` is now
  read as an outgoing message to that audience (reply class); `said` alone stays a
  statement about self. A classification rule over existing fields, no new field.

### Implementation notes

Deviations and details recorded while implementing M2–M6:

- **Guard on owner texts.** Guard part 3 also scans the owner's own texts (`said`), not
  only third parties' (P2).
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
- **Self-service resets** do not show the admin-reset notice on the account page.
- **`/healthz`** is an unauthenticated liveness endpoint that does not touch the DB.
- **Answer shape** (until D23; the answer call now returns `depends_on`, see the M8
  notes). The answer call's JSON has an `absent_facts: [{kind, what, why}]` field
  (replacing `needs`) that the model fills first: the facts the applicable decision rules
  need but `facts` lacks. The name sorts before `answer` on purpose, since some backends'
  JSON grammars emit keys alphabetically and the field only helps if it is generated
  before the model commits to an answer. Post-processing turns `type: "escalate"` with a
  non-empty `absent_facts` into `needs` (small models list the gap, then escalate),
  except `escalate: "boundary"`, which always stands. A missing or unknown `kind` becomes
  `other`. Needs for a fact the call already received as `value: null` are dropped; if none
  remain the result is `low_confidence`. Facts with `value: null` reach the model as
  "not available". An answer that relies on a `decision_rule` claim while any fact of the
  call is `null` also becomes `low_confidence`: only decision rules ask for facts, and
  small models otherwise confidently decline ("not possible tomorrow") without the fact.
- **Prompt layout for latency.** The answer system prompt (instructions, worked examples,
  schema) is static and byte-identical across calls; owner, audience, claims, context and
  the situation follow in the user message, so a llama.cpp-style prompt prefix cache
  covers the long part. Claims are shown under short labels (`c1`, `c2`, …) and mapped back
  to IDs in post-processing (`used_claims`): claim IDs cost ~10 output
  tokens each, and output tokens dominate latency on small local models.

Deviations and details recorded while implementing M7:

- **Preparation at submit.** Episode categories are resolved and created in `submit`, in
  the job row's transaction, because `estimated_tokens` depends on the call list. The
  `category_create` change carries the `job_id`. The description call is the job's first
  LLM call: budgeted, it covers no episodes, never overwrites a non-empty description
  (≤ 200 chars, guarded); a backend error ends the job `partial` with the categories kept.
- **Episode classes.** `decision` without `situation` (a self-describing choice) goes to
  the decision pass; with `said` it is also a statement. The job counter `episodes` is
  `{total, statement, reply, decision}`. Pass grouping: statements and decisions by
  (audience, category), replies by audience with the reply's `situation` cut to 600 chars.
- **Category creation.** All-or-nothing per path (missing parents and leaf together). A
  path matching the guard's regex part is never created (counted unmapped), nor one an
  alias would push deeper than 3 levels. Normalisation trims whitespace around segments;
  an invalid path is rejected, never truncated. A model-chosen category outside the call's
  subtrees becomes `proposed_category`. Undoing `category_create` checks live state
  (conflict on claims, foreign sub-categories or LLM analyzers) rather than change order.
- **Builtin state and claims.** State keys hold audience IDs (renames keep state), weekday
  keys `mon`…`sun`; `time_activity` stores `wd_hour` and `audience_weekday` (whole hours) to
  stay within 200 keys per metric and month; every builtin has a `days` metric for the
  period. Facets: `time_activity` `:hours`, `:slots`, `:weekends`; `time_duration`
  `:length`, `:latency:<audience_id>`; `time_gaps` none. Since D23 latency comes from
  `situation_at` and every interval of 0–8 h, whatever the episode class, feeds lengths
  and gaps (before D23 an interval on a reply was read as received → replied); gaps are
  computed within one job. Evidence is replaced from state on every run. Weakening: a
  pending claim is archived (applied `decay` change, reason `analyzer_weakened`); an
  approved one keeps its text and gets one proposed archive; an owner-edited text is never
  re-rendered.
- **Analyzer tools.** Builtins can only be `active` or `disabled`; `retired` is final for
  LLM analyzers. `list_analyzers(scope)` matches a category path and an audience name;
  `get_analyzer` also returns `claim_ids`. For LLM analyzers `failures` counts consecutive
  failed runs; for builtins it is cumulative.
- **Sandbox.** The helper module and the analyzer source are passed as argv strings to a
  `-c` driver that builds `wwyd_helpers` in memory, so changing the helpers needs no asset
  rebuild; the guest sees `sys.argv == ["-c"]`. Guest flags `-S -B -s` (`-I` would ignore
  `PYTHONHOME`). The stdlib subset has 69 modules (no zoneinfo, socket, subprocess,
  ctypes). The assets are in the sdist too, since `uv build` builds the wheel from it.
  Measured on Apple M-series: first compile 0.8 s, cached load 8 ms, trivial run 32 ms;
  the wheel grows by 3.1 MB.
- **Stylometry.** Keyness is Dunning's log-likelihood (G² ≥ 6.63, ≥ 5 uses) against all
  other audiences, so a single audience has no favourite words. Percentiles are the upper
  edge of the histogram bucket. The claim needs ≥ 30 messages over ≥ 3 weeks and one
  rule-like part with share ≥ 0.75, names ≤ 5 favourite words, and its share is the lowest
  part's. In `predict`, words come from the state behind the approved claim's key (only
  those still in the text when the owner edited it); "rarely ends with a period" is
  `1 − period_share ≥ 0.8`; acronyms and a lone "I" are not lowercased; the rules run after
  the shorten re-ask, whose failure keeps the original.
- **Annotation and authoring.** A category is annotated only with ≥ `analyzer_min_episodes`
  episodes of its own in the job or an active LLM analyzer on it or an ancestor (≤ 60 label
  names per category and field). The authoring trigger counts a category's own episodes;
  the analyzer reads the subtree, and the test uses its timed episodes. Acceptance also
  needs a non-empty holdout; hypotheses beyond the `max_analyzers` capacity are dropped
  before testing. LLM analyzer output keeps only `n`, `hits`, `base_n`, `base_hits` under
  `_`; the row's `name` is the template cut to 200 chars. Its claim covers the 12 months to
  the latest month with data and weakens when the last 3 months hold ≥ 10 samples with a
  share below 0.75.
- **Word-usage notes.** The last noted favourite-word set is the metric `noted` of a
  lazily created builtin row `word_usage` per audience (disabling it turns notes off).
- **Category upkeep.** The nightly merge looks at episode-created categories since
  `compaction_runs.last_nightly_at`; two seed or owner categories are never merged
  automatically, and between episode categories the newer goes into the older. A split
  keeps keyed claims in the parent, takes ≤ 200 claims per call, discards an invalid
  grouping whole and is skipped when it would exceed `max_categories`. Step failures are
  counted in `skipped_steps`, never fail the run. Undoing a merge recreates the categories
  with their original IDs.
- **Export.** Keyed-claim keys are exported in a portable form (`{category:<path>}`,
  `{audience:<name>}`, `{analyzer:<index>}`) and mapped back to the target's IDs on import;
  an unresolvable key is dropped (`keys_dropped`). A `merge` import replaces an existing
  analyzer's state (adding would double-count); unknown builtins and scopes are skipped and
  counted; new LLM analyzers beyond `max_analyzers` are skipped. Category paths in the
  schema accept Unicode; the importer requires normalised paths.

Deviations and details recorded while implementing M8:

- **Placement in the job.** The client's paths are still resolved in `submit`; episodes
  without a category are placed by the job (the `place` purpose), before annotation. Their
  annotation and pass calls are planned after placement. Placement creates categories
  through its own resolver, whose `new_categories_per_job` allowance is what the client's
  paths left; it records its own `category_create` change with the `job_id` (a job can
  have two), whatever ended the LLM phase. The system prompt is static; the category list
  and the episodes are in the user message.
- **Place reply shape.** One item per episode, keys `i`, `path`, `path_description`, in
  that (alphabetical) order: JSON-schema grammars emit keys alphabetically, and a separate
  list of new categories generated before the placements made small local models invent
  categories in a loop until the reply was cut. For the same reason a call holds at most
  25 episodes. The prompt's example covers several areas of life and tells the model to
  place by what the owner is doing, not by who is involved.
- **Estimate for unplaced episodes.** `estimated_tokens` includes the place calls and,
  for the episodes still to be placed, stand-in pass calls: all of an audience's unplaced
  episodes in one category, decisions always included (never left to annotation), and
  each stand-in call charged the full category list on top. Their annotation is not
  estimated. The stand-in calls also keep those episodes pending, so an LLM error during
  placement leaves them in `remaining_records`.
- **Placement counters.** A path or description failing the guard counts in
  `dropped_by_guard` too; a failing path, a missing or invalid placement and
  `llm_for_ingest: false` count in `unplaced_episodes`. A placed path that hits
  `max_categories` / `new_categories_per_job` maps like a client path and counts in
  `unmapped_categories` only. The concentrated-word check ignores the words of existing
  category paths. A description that fails is dropped (empty description), the path kept.
- **Corrections.** A correction episode that has a `decision` goes to the decision pass
  whatever its category's size (corrections bypass the `analyzer_min_episodes` rule). Its
  candidates keep the corrected prediction's claim IDs in memory; after the candidate is
  stored (created or absorbed into an existing claim), each (stored claim, used claim)
  pair is forced into the job's compaction run and judged even across kinds and
  categories, as long as both claims are pending or approved. The marker line is the
  first line of the user message (`passes.CORRECTION_MARKER`).
- **Evidence weight.** `store/evidence.py` holds both formulas of §4.5 over the same
  weighted sources: the evidence weight for compaction and the context rank for `predict`
  and `get_persona`. `correction_weight` is an instance
  threshold (`WWYD_CORRECTION_WEIGHT`, ≥ 1, default 3).
- **Latency texts.** `time_duration` renders "responds within …" (`… of N responses`);
  the work-hours side is decided by the local `situation_at`. Analyzer items and the
  sandbox input carry `latency_s`.
