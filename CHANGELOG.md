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
    deterministic post-processing (lowercase start, no final period, a cut to the
    owner's p90 sentence count (sentence ends found by the punctuation of any script) that keeps a cut question when it fits, emoji stripped
    when the owner rarely uses them; the shorten re-ask only when the cut reply is still
    over 3x the p90 length). Case and final-period lines that the rules enforce
    are left out of the style parameters.
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

- Ingest decisions pass: the model names the supporting decisions (`used_items`) instead
  of counting them; code counts distinct items and derives the claim's sources and period
  from them.
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
- predict: `confidence` and `used_claims` are computed in code (claim confidence ×
  evidence, agreement, guards) instead of being asked of the model; the deciding claim
  label is derived from `c_excluded_by` / the applicable boundary claim; a `ruled_out`
  answer that does not decline is re-asked once.

### Removed

- The typed ingest records `message`, `call_transcript`, `calendar_event`, `decision` and
  `note`.

### Fixed

- Compaction and `add_claims` no longer merge claims that differ only in a number ("at
  most 40 euros" / "60 euros"): only same-word duplicates (punctuation, case, spacing)
  merge without the judge; every other similar pair goes to the judge. This replaces the
  per-language negation word list. Ingest within-job dedupe and analyzer authoring keep
  their Jaccard rule but never merge texts whose numbers differ.
- Category split keeps partial groupings: a missed claim stays in the parent, a claim
  listed twice keeps its first group, a bad group is dropped; only a grouping with fewer
  than two valid groups is discarded.
- Category merge: code proposes candidate pairs (same or close leaf names, overlapping
  descriptions) and the model confirms each; the open synonym search now covers only the
  new categories no candidate covered (at most two calls per run).
- `predict`: the model no longer computes dates. It only extracts the situation's time
  in a canonical English form (routing's `when` field, or one small `when` call when
  routing is skipped); code resolves it in the persona timezone into one
  `When: Friday 2026-10-23 (working day)` line (date only, since the situation carries the time; every day is
  marked `(weekend)` or `(working day)`, a mixed range is unmarked), so "tomorrow" asked on a Thursday is
  no longer declined as a weekend day. New persona setting `weekend_days` (default
  Saturday and Sunday) in `get_settings`/`update_settings` and the persona export.
  A deadline or period ("by the end of the week", "by Friday", "this week") is a
  deadline from today, never "this weekend", and gets no `When:` line (it was read as a
  constraint); a week runs
  Monday to Sunday.
- LLM client: the predict-path calls keep their own small `max_tokens` (no 1024 floor),
  and a reply cut off at `max_tokens` is retried fresh with a "keep it short" note
  instead of being echoed back for repair, so a model stuck in a repetition loop costs
  seconds instead of a minute and an `llm_unavailable` escalation.
- `predict`: a refusal rule beats a boundary verdict: when a non-boundary claim excludes
  the request, the owner declines politely instead of escalating `boundary`; a boundary claim alone still
  escalates.
- `predict`: a settled answer now gets the analyze call's conclusion as an internal
  `Analysis:` line, so it no longer contradicts it ("3 hours is within 4" answered "too
  far"). A `Case:` line only when the stylometry profile decides it and a capital
  start post-fix for owners who start with a capital; the first-person rule now covers
  verb forms in every language, with a worked example outside English.
- Decision LLM calls (`route`, `analyze` and the eval `judge`) run at temperature 0, so
  the same situation gets the same decision; text-writing calls keep 0.2.
- `/revoke` accepts public clients with `client_id` only (RFC 7009); it is now our own
  handler. It also revokes wwyd API keys passed as `token`, and is throttled like `/token`.
- `predict` knows the current date: the answer prompt gets a `Now:` line (weekday, date,
  time) in the persona's timezone, so "tomorrow" and "on Saturday" resolve correctly.
- `predict` no longer accepts or declines while `depends_on` is set; `depends_on` lists
  only conditions from the owner's claims (no invented ones such as free time), the
  worked examples no longer mirror typical inputs and are spread over several areas, and a server-side guard re-asks once and otherwise escalates
  `low_confidence`.
- `predict` handles every language alike: the analyze call names the situation's
  language (`g_language`), the answer call gets it as the answer language, and the
  language guard re-asks once when the Unicode scripts of situation and answer differ
  or the answer's own `language` differs from it (this also catches German answers to
  English messages); no per-language word lists remain. A remaining mismatch is accepted
  so a claim such as "always writes in Russian" keeps working.
- `predict` runs two calls: an internal `analyze` call decides which claims apply and
  what they decide (none, boundary, ruled out, open, settled) and supplies `depends_on`;
  the answer call then only writes the message in the owner's voice under a server
  instruction for that verdict. `out_of_persona` and `boundary` escalate in code without
  the second call; the commit guard runs only for an open verdict; still at most one
  re-ask (of the answer call).
- `predict` escalates `out_of_persona` when no claim other than style is about the
  situation's topic (favours, plans and opinions included) instead of guessing.
- `predict` applies claims that are short labels for the owner (such as "Vegetarian.")
  as the behaviour they imply instead of escalating `out_of_persona`, and a decline may
  only suggest or ask about an alternative, never state that the owner is free or able
  to do it.
- `predict` answers use the language's normal capitalisation (German nouns, formal
  «Вы»/"Sie" where the style says so) and lowercase only when a style claim or the
  style parameters say so; they speak as the owner in the first person instead of
  copying the third-person claim text; and in an open answer a condition about the
  owner is one the owner will check, while only what the other person can know is
  asked of them. The answer examples now vary case with their style claims.
- The worked examples in the `predict` prompts are set in unusual areas (cacti,
  birdwatching, pottery, chess, gardening, boats) so the model cannot copy them onto
  common inputs.
- `predict` no longer applies a claim that limits one activity by day and hour to a
  request for a different activity at that time: the `analyze` scratch note first names
  what is asked and compares it only with claims about the same thing, backed by a
  worked example and a contrastive pair (same claim, one situation it limits and one it
  does not cover).
- Ingest, annotation, analyzer-authoring and upkeep prompts no longer illustrate their
  rules with common areas (salary, work calls, breakfast, loans, gifts, running, films,
  standups); their examples use unusual areas, so a copied example cannot become a
  fabricated claim or label.
- `boundary` now means a topic nobody may answer for the owner; refusal rules are
  `decision_rule` claims (ingest and analyzer prompts), and `predict` answers a stored
  refusal-style boundary with the decline instead of escalating, and ignores boundary
  claims about other topics.
- The LLM client retries a single call once after a short pause on a transient error
  (connection failure, HTTP 502/503/504; read timeouts only outside `predict`) before
  reporting `llm_unavailable`.

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
