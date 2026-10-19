# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - unreleased

First public release. Version 0.1.0 was never released: it was the version string of
development builds before the episode and client contract revisions, which changed tool
shapes (see Changed), so the first release bumps the minor version.

### Added

- SQLite store in WAL mode with plain SQL migrations applied on start.
- Seeded categories and a default audience per persona.
- Claim tools: `add_claims`, `list_claims`, `search_claims` (FTS), `update_claim`,
  `delete_claims`, `review_claims`.
- Audience tools: `list_audiences`, `upsert_audience`, `delete_audience`.
- Category tools: `list_categories`, `upsert_category`, `merge_categories`.
- Persona tools: `export_persona`, `import_persona` (replace or merge),
  `delete_persona`, `get_settings`, `update_settings`.
- Export format `schema/persona.v1.json` (JSON Schema).
- Verbatim guard against contact identities in claims.
- Logging filter that never records tool arguments.
- stdio transport: `wwyd stdio --user <login>`.
- M2: HTTP server (`wwyd`, `127.0.0.1:8740`) with Streamable HTTP `/mcp`, OAuth 2.1
  authorization server (dynamic client registration, client ID metadata documents, PKCE,
  consent page, revocation), API keys, scopes `owner`/`read`/`respond:<audiences>`,
  server-rendered sign-in, account and admin pages, setup/invite/reset links, auth
  throttling, `trusted_proxies`, `/healthz`.
- M3: `predict` with SQL visibility filtering, disclosure and escalation; `get_persona`; `predict` audit log and `list_audit`; OpenAI-compatible LLM
  client with presets and encrypted per-persona API keys; instance default backend via
  `[llm]` / `WWYD_LLM_*`.
- M4: `ingest_raw` and `job_status`, in-memory ingest jobs, habit detectors over calendar
  and message metadata (docs/habits.md), LLM style derivation, verbatim guard on derived
  claims.
- M5: compaction (`compact`, dedupe, absorb, contradictions with a batched LLM judge,
  decay, size budget, purge), `list_changes`, `undo_change`, nightly scheduler.
- M6: limits and usage (rate limits, spend cap, LLM concurrency), `wwyd eval`, Dockerfile,
  `deploy/` (docker-compose with Caddy, cloud-init), docs/install.md, docs/ecosystem.md,
  docs/eval.md.
- Release automation: PyPI package `wwyd` (`uvx wwyd`), multi-arch container image
  `ghcr.io/melnikaite/wwyd`, GitHub Releases from tags (RELEASING.md).
- Compaction size budget as per-user limits (`max_claims_per_category`,
  `max_approved_tokens`).
- Reserved audience alias `default`: every audience-taking tool resolves it to the
  persona's default audience, without a fallback hint. No audience can be named `default`.
- docs/clients.md: the contract for MCP client authors (auth, scopes, revocation, tool
  shapes, error codes, versioning).
- Ingest jobs flush derived claims every 20 LLM calls and when they end; a job stopped by
  the daily token limit, the spend cap or an unavailable LLM ends `partial` and
  `job_status` lists `remaining_records` (batch indices to resend) and `retry_after`.
  Habit detectors now run after the LLM phase, over the processed records only, so a
  resend counts each record once.
- `ingest_raw` returns `estimated_tokens` (a conservative estimate) and
  `remaining_tokens`; a call whose estimate does not fit the remaining daily budget is not
  started.
- Style saturation: once 5 style calls in a row of one audience add nothing new, the
  audience's remaining style chunks are skipped (`skipped_saturated`,
  `saturated_audiences`). Notes and decisions are never skipped.
- `ingest_raw` optional `source` with a per-source timestamp watermark
  (`skipped_seen`); new owner tools `list_sources` and `reset_source` (27 tools).
- M7 (SPEC D17–D22):
  - Episodes: one `ingest_raw` shape `{when, where, audience, category, situation,
    decision, said}` for any data source; only `said` counts as the owner's words.
  - Organic categories: unknown episode paths are created within `max_categories` and
    `new_categories_per_job` (else counted in `unmapped_categories`), described by one
    batched LLM call and undoable; renames and merges leave aliases that keep routing
    episodes; nightly `category_merge` (synonyms, also across languages) and
    `category_split` apply automatically with undo; `list_categories` shows aliases.
  - Analyzers with mergeable counters in `analyzer_state`: builtins `time_activity`,
    `time_duration`, `time_gaps`, `stylometry` and word-usage notes; host-side output
    contract and key filter; owner tools `list_analyzers`, `get_analyzer`,
    `update_analyzer` (30 tools).
  - A sandbox for LLM-written analyzers: wasmtime running CPython 3.15 for WASI, shipped in
    the wheel and built by `scripts/build_sandbox.py`; fuel, epoch, memory and output
    limits; CI runs its isolation tests on Linux, macOS and Windows.
  - Annotation (the LLM labels distinct values, code counts) and LLM analyzers proposed as
    hypotheses, tested on older episodes with a recent holdout, a binomial test and
    Benjamini–Hochberg; rejected ones are kept as negative memory.
  - Style in `predict`: replies get the audience's approved stylometry parameters and
    deterministic post-processing (lowercase start, no final period, one shorten re-ask).
  - Per-user limits `max_categories`, `new_categories_per_job`, `max_analyzers`,
    `sandbox_cpu_seconds_per_day`; instance `sandbox_concurrency`; config
    `analyzer_min_episodes`, `split_threshold`.
  - Export format: category aliases and labels, analyzers with their state, portable
    keyed-claim keys.
- M8 (SPEC D23, client contract revision):
  - Episodes may omit `category`: the server places them into an existing, aliased or new
    organic category (`unplaced_episodes` counts those that went to `other`).
  - Episode field `situation_at` (when the situation reached the owner); response latency
    is `when` − `situation_at` in any category.
  - `predict` answers come with `depends_on`: one or two plain sentences, from the
    owner's claims, on what the real answer depends on (time, weather, anything outside
    the persona); the answer itself is always sendable and never asserts what wwyd cannot
    know. A client may call again with what it found out in `context`.
  - Every `predict` result carries `prediction_id`; a correction episode
    (`corrects: <prediction_id>`) weighs more than an ordinary one and is judged against
    the claims the prediction used (`corrections`, `unknown_predictions`; migration
    `0010_corrections` adds `used_claims` to the audit row).
  - docs: episodes are text only (clients describe media); what never to send is stated as
    records versus behaviour, so money and health habits are welcome.

### Changed

- An episode with `said` and an explicit `audience` but no `situation` is an outgoing
  message the owner started and feeds the reply/style side for that audience; `said` alone
  stays a statement about self.
- `predict` latency: claims are shown to the model under short labels instead of claim IDs
  (fewer output tokens), and routing is skipped when the visible claims span at most three
  categories.
- The `unknown category` error of `add_claims` points to `list_categories` and
  `upsert_category`.
- Ingest jobs no longer fail with `spend_cap` / `rate_limited` / `llm_unavailable` after
  the detectors ran: they end `partial` and keep the claims derived so far (migration
  `0007_ingest_progress`).
- **Breaking:** `ingest_raw` takes `episodes` instead of typed `records`; the job counter
  `episodes` replaces `records` (migrations `0008_episodes`, `0009_analyzers`).
- Habit detectors became builtin analyzers whose counters accumulate across jobs, so small
  batches add to a habit until it passes the thresholds.
- Category paths accept Unicode letters (NFKC, casefolded).
- **Breaking:** `predict` no longer returns `type: "needs"` and no longer takes `facts`;
  `depends_on` and `context` replace them (SPEC D23), with no domain vocabulary such as
  need kinds.
- **Breaking:** an interval `when` always means the owner's span (a meeting, a call) and
  feeds lengths and gaps for every episode class; reply latency now comes from
  `situation_at` instead of an interval on a reply.
- Before 1.0 every breaking change on `main` bumps the minor version, also between
  releases (docs/clients.md § Versioning, RELEASING.md).

### Removed

- The typed ingest records `message`, `call_transcript`, `calendar_event`, `decision` and
  `note`.

### Fixed

- `/revoke` accepts public clients with `client_id` only (RFC 7009); it is now our own
  handler. It also revokes wwyd API keys passed as `token`, and is throttled like `/token`.

### Security

- `[auth] allow_insecure_redirects` / `WWYD_AUTH_ALLOW_INSECURE_REDIRECTS` (default `true`):
  when `false`, DCR and CIMD reject plain-http redirect URIs on non-loopback hosts. The
  consent page warns when a redirect goes over unencrypted http.

- Client ID metadata document fetches are pinned to the validated IP (no DNS rebinding).
- `[llm] allow_private_base_urls` / `WWYD_LLM_ALLOW_PRIVATE_BASE_URLS`: SSRF guard for a
  persona's own LLM `base_url` (the instance backend is exempt).
- LLM backend errors are reported generically; a stored API key is not reused when the
  backend host changes.
- Secret key from `WWYD_SECRET_KEY` / `WWYD_SECRET_KEY_FILE`; the key file is created
  atomically.
- Request body caps (64 KB for forms and OAuth, `/mcp` sized to the batch limit) and
  bounded tool parameters.
- Throttling: the token endpoint is keyed per client and IP and applies only to known
  clients; stale tokens no longer count toward the `/mcp` bearer throttle; IPv6 clients
  are throttled per /64; when every `X-Forwarded-For` hop is trusted, the peer address is
  used.
- Password reset can disconnect all apps and API keys, invalidates other reset links, and
  is throttled per login.
- `respond` tokens are revoked when their audience is deleted or renamed.
- The consent page marks self-registered client names as unverified.
- Identifier guard regexes run in linear time.

[Unreleased]: https://github.com/melnikaite/wwyd/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/melnikaite/wwyd/releases/tag/v0.2.0
