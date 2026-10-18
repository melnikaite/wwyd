# wwyd

**wwyd — what would you do: an MCP server that holds a person's character and answers how they would reply or act.**

[![CI](https://github.com/melnikaite/wwyd/actions/workflows/ci.yml/badge.svg)](https://github.com/melnikaite/wwyd/actions/workflows/ci.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)

## Status

**Pre-alpha.** Milestone 1 of 6 (store and CRUD over stdio) is done. HTTP, auth,
`predict`, ingest and compaction are still to come; see the
[roadmap in SPEC.md §16](SPEC.md#16-milestones). Interfaces and the database schema may
change without notice.

## What it is

wwyd stores one person's character and ways of acting: how they think and decide, their
habits, and how they talk to different kinds of people. Any AI harness can use it over MCP
for two things:

1. **Personalise** the harness to its owner (`get_persona`).
2. **Predict** how the owner would reply or act in a situation (`predict`), e.g. for an
   out-of-office auto-responder or for reply drafts.

## What it is not

The project ends at the answer. It does not decide whether to reply or to whom, classify
contacts, transcribe calls, send messages or read calendars: that is the harness's job. It
is not a general-purpose memory or RAG store, and it has no messenger plugins or importers.

## Privacy by design

The full rules are in [SPEC.md §2](SPEC.md#2-privacy-invariants). In short:

- **No raw conversations.** Raw input lives only in process memory while it is processed;
  it never reaches the database, logs, error reports or exports.
- **No contact identities.** No names, phone numbers, emails, messenger IDs or hashes of
  them. Derived claims pass a verbatim guard before they are stored.
- **No live data.** Calendar contents, mail and documents are never stored.
- **Audiences, not individuals.** The owner defines audiences ("family", "colleagues");
  the harness maps contacts to audiences.
- **Visibility filtered in SQL.** What a third party may see is decided by the database
  query before any LLM call, never by the prompt.
- **Disclosure.** Answers given to third parties always carry a server-built notice that
  they are automated.

Text sent to `predict` or ingest goes to the LLM backend the persona is configured with;
with a local backend nothing leaves the machine.

## Quick start (from source)

Requires [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/melnikaite/wwyd.git
cd wwyd
uv sync
uv run wwyd stdio --user you@example.com
```

The stdio transport serves one user's persona with owner scope. Usually you let your MCP
client start it, as below.

Planned, not available yet: `uvx wwyd` from PyPI, a Docker image, and the HTTP server with
OAuth and API keys (milestone 2).

### Claude Code

```sh
claude mcp add wwyd -- uv run --project /path/to/wwyd wwyd stdio --user you@example.com
```

Add `--scope project` to write it to the project's `.mcp.json` instead.

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wwyd": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/wwyd", "wwyd", "stdio", "--user", "you@example.com"]
    }
  }
}
```

## Tools

Available now (milestone 1):

| Tool | Purpose |
|---|---|
| `add_claims` | Add claims (statements about the owner) |
| `list_claims` | List claims with filters |
| `search_claims` | Full-text search over claims |
| `update_claim` | Edit a claim |
| `delete_claims` | Delete claims |
| `review_claims` | Accept or reject pending claims |
| `list_audiences` | List audiences |
| `upsert_audience` | Create or update an audience |
| `delete_audience` | Delete an audience |
| `list_categories` | List categories |
| `upsert_category` | Create or update a category |
| `merge_categories` | Merge one category into another |
| `export_persona` | Export the persona as `persona.v1.json` |
| `import_persona` | Import a persona (replace or merge) |
| `delete_persona` | Delete the persona |
| `get_settings` | Read persona settings |
| `update_settings` | Change persona settings |

Planned:

| Tool | Milestone |
|---|---|
| `predict`, `get_persona` | 3 |
| `ingest_raw` and job status | 4 |
| Compaction, change log and undo | 5 |

See [SPEC.md §6](SPEC.md#6-mcp-tools) for the full tool list.

## Data location

Data lives in `~/.wwyd/` (a single SQLite file, `wwyd.db`). Set `WWYD_DATA_DIR` to use
another directory.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

`uv run pytest -m llm` runs the few tests that need a live LLM; they are skipped by default.
[SPEC.md](SPEC.md) is the source of truth for the design.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), the [Code of Conduct](CODE_OF_CONDUCT.md) and the
[security policy](SECURITY.md).

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified wwyd as a network service, you must
offer its source to its users; harnesses and clients that merely talk to wwyd over MCP are
separate programs and are not affected.
