# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/melnikaite/wwyd/commits/main
