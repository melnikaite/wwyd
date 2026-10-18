# wwyd

MCP server that holds a person's character and ways of acting and answers "what would the
owner do here?". `SPEC.md` is the design document and the source of truth: read the
sections relevant to your task before writing code, and when a feature does not fit the
spec's data model, update the spec first (and say so) instead of hacking around it.

Public open-source project: code, comments, docs and commit messages in English.

## Commands

- `uv sync` — install (uv only; no pip, Homebrew or system packages).
- `uv run pytest` — tests. `uv run pytest -m llm` runs the few tests that need a live
  LLM (LocalAI on `http://127.0.0.1:1240/v1`); they are skipped by default.
- `uv run ruff check . && uv run ruff format --check .` — lint/format.
- `uv run wwyd` — HTTP server on `127.0.0.1:8740`; `uv run wwyd stdio --user <login>` — stdio.
- Data dir: `~/.wwyd/` (override with `WWYD_DATA_DIR`; tests always use a temp dir).

## Invariants that must never regress (SPEC §2)

- Raw `ingest_raw` input is never written to the DB, logs, error reports or exports. Never
  log tool arguments, request bodies or LLM prompts/responses, at any level.
- No contact identities (names, phones, emails, messenger IDs, or hashes) anywhere.
- Third-party visibility is filtered in SQL before any LLM call, never by the prompt.
- `predict` audit rows carry no text.

## Conventions

- Python ≥ 3.12, FastAPI, official MCP SDK `mcp>=2.3,<3` (`from mcp.server import
  MCPServer`; FastMCP no longer exists under that name in v2).
- SQLite in WAL mode, plain SQL migrations in `src/wwyd/db/migrations/NNNN_name.sql`,
  applied automatically on start. No ORM.
- snake_case field names everywhere, kind-prefixed random IDs (`clm_`, `aud_`, …).
- MCP tools: one module per tool group under `src/wwyd/mcp/tools/`, thin wrappers over
  `src/wwyd/store/`; business logic lives in `store/` and is tested directly too.
- Tests use a fake LLM fixture; nothing in the default test run needs the network.

## Delegation

The main session is the orchestrator: it plans, reviews, and answers questions.
Delegate implementation to the `worker` agent using these rules:

- **Confirm scope before implementing anything.** If it's ambiguous whether
  the user wants analysis/a plan or actual code changes — or they explicitly
  asked to "look into," "analyze," "think about," or "plan" something —
  default to analysis-only: present findings/a plan and stop. Never let "this
  looks easy" justify skipping that check; easy-looking tasks are exactly the
  ones that slip through unnoticed and burn tokens on unrequested work.
- **Do it yourself (no delegation) only if BOTH hold:** the edit touches 1–2
  files in a precisely known location, AND the current session is not
  pricier to run than the worker. "Pricier" is model tier *and* effort: the
  worker's are pinned in `.claude/agents/worker.md` (full model ID + an
  `effort:` field), the session's model is stated in the system prompt but
  its effort is not. Typical setup: session on Opus 5.5 at `high`, worker
  on Opus 5.5 at `low` — same model, but the session spends more thinking
  per turn over a much larger context, so it is the pricier side and
  anything beyond a precisely-located 1–2 file edit goes to the worker.
  When the comparison is uncertain (pricing and model lineups change),
  delegate rather than guess. The reverse also holds: if the user has
  deliberately switched the main session to a cheaper model than the
  worker's for cost control, don't hand bulk work to the pricier worker
  without saying so — that silently defeats their choice.
- **Send a follow-up task to a live worker (SendMessage):** the next task
  touches the same code the worker just worked on, and no more than a couple
  of minutes have passed.
- **Spawn a new worker:** the topic/subsystem changed, the previous agent
  already completed a large task (its context is bloated), or the tasks are
  independent — in that case spawn several new workers in parallel.
- **Dispatch independent workers in one message, not one at a time.** When a
  batch's Agent calls have no data dependency between them, send them
  together (multiple tool uses in a single message) even if you plan to
  review each one's diff before deciding the next step — reviewing
  sequentially doesn't require launching sequentially. Conflating "I'll
  check this before moving on" with "so I'll launch them one at a time"
  silently serializes work that could run concurrently.

After delegating, always review the resulting diff yourself.
