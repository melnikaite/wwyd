# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - unreleased

First public release.

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
- M3: `predict` with SQL visibility filtering, disclosure, needs round trip and
  escalation; `get_persona`; `predict` audit log and `list_audit`; OpenAI-compatible LLM
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
- `predict` needs carry a machine-readable `kind` (`calendar.free_busy`,
  `contact.relationship`, `other`; unknown kinds are to be read as `other`).
- `predict` facts accept `value: null` ("the harness cannot find this out"); the server
  then answers without it or escalates with `low_confidence` (always when the answer
  relies on a decision rule), and never asks for it again.
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

### Changed

- `predict` latency: claims are shown to the model under short labels instead of claim IDs
  (fewer output tokens), and routing is skipped when the visible claims span at most three
  categories.
- The `unknown category` error of `add_claims` points to `list_categories` and
  `upsert_category`.
- Ingest jobs no longer fail with `spend_cap` / `rate_limited` / `llm_unavailable` after
  the detectors ran: they end `partial` and keep the claims derived so far (migration
  `0007_ingest_progress`).

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

[Unreleased]: https://github.com/melnikaite/wwyd/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/melnikaite/wwyd/releases/tag/v0.1.0
