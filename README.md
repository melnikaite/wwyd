# wwyd

**wwyd — what would you do: an MCP server that holds a person's character and answers how they would reply or act.**

[![CI](https://github.com/melnikaite/wwyd/actions/workflows/ci.yml/badge.svg)](https://github.com/melnikaite/wwyd/actions/workflows/ci.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)

## Status

**Pre-alpha.** All seven milestones of the [roadmap in SPEC.md §16](SPEC.md#16-milestones)
are implemented: store and CRUD, HTTP with OAuth 2.1 and API keys, `predict`, ingest,
compaction, limits and packaging, and episodes with organic categories and analyzers. No release has been published yet;
interfaces and the database schema may still change without notice.

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

## Quick start

Requires [uv](https://docs.astral.sh/uv/). Step-by-step instructions for every setup
(own computer, Docker, cloud server with a domain, connecting Claude and ChatGPT) are in
[docs/install.md](docs/install.md).

### HTTP server

```sh
uvx wwyd               # http://127.0.0.1:8740, MCP endpoint /mcp
```

From source: `git clone https://github.com/melnikaite/wwyd.git && cd wwyd && uv sync && uv run wwyd`.

On first start wwyd prints a one-time link (`/setup?t=...`) to create the admin account.
`wwyd invite` prints an invite link for another user, `wwyd reset-password <login>` a
password reset link. `GET /healthz` is a liveness check.

**OAuth.** MCP clients that support OAuth (claude.ai custom connectors, ChatGPT, Claude
Code) only need the URL `https://your.domain/mcp`: they register themselves (dynamic client
registration or a client ID metadata document), the user signs in and picks a scope on the
consent page. Anything but localhost needs `public_url` with https.

**API keys.** On the account page (`/account`) create a key with a scope: `owner`, `read`,
or `respond:<audience>[,<audience>...]`. Send it as `Authorization: Bearer <key>`. Keys
and OAuth grants can be revoked there; revocation is effective on the next request.

### stdio

```sh
uvx wwyd stdio --user you@example.com
```

Serves one user's persona with owner scope, no OAuth. Usually the MCP client starts it:

```sh
claude mcp add wwyd -- uvx wwyd stdio --user you@example.com
```

or, in Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wwyd": {
      "command": "uvx",
      "args": ["wwyd", "stdio", "--user", "you@example.com"]
    }
  }
}
```

### Docker

```sh
docker run -d --name wwyd -v wwyd-data:/data -p 127.0.0.1:8740:8740 ghcr.io/melnikaite/wwyd:latest
docker logs wwyd   # shows the setup link
```

`deploy/` has a `docker-compose.yml` with Caddy for TLS and a cloud-init file for a VM.

### Evaluation

`wwyd eval <pairs.jsonl> --user <login>` blind-compares `predict` answers with the owner's
real replies; see [docs/eval.md](docs/eval.md).

## Further reading

- [docs/install.md](docs/install.md): installation and connecting AI apps.
- [docs/ecosystem.md](docs/ecosystem.md): what a harness sends to `ingest_raw` and how it
  uses `predict`.
- [docs/habits.md](docs/habits.md): the analyzers (builtin and LLM-written) and the claims
  they produce.
- [docs/eval.md](docs/eval.md): evaluation method and results.
- [docs/clients.md](docs/clients.md): the contract for MCP client authors (auth, scopes,
  revocation, tool shapes, `depends_on` and `context`, error codes, versioning).

## Tools

30 tools. Scopes per [SPEC.md §5](SPEC.md#5-scopes-and-who-sees-what): a client only sees
the tools its scope allows.

| Tool | Scope | Purpose |
|---|---|---|
| `predict` | owner, respond | How would the owner reply or act in this situation |
| `get_persona` | owner, read | Persona summary for personalising a harness |
| `ingest_raw` | owner | Submit episodes; derives claims in a background job |
| `job_status` | owner | Progress and counts of an ingest job |
| `list_sources` | owner | Per-source ingest watermarks |
| `reset_source` | owner | Forget one source's watermark |
| `add_claims` | owner | Add claims (statements about the owner) |
| `list_claims` | owner | List claims with filters |
| `search_claims` | owner | Full-text search over claims |
| `update_claim` | owner | Edit a claim |
| `delete_claims` | owner | Delete claims |
| `review_claims` | owner | Approve or reject pending claims and proposed changes |
| `list_audiences` | owner | List audiences |
| `upsert_audience` | owner | Create or update an audience |
| `delete_audience` | owner | Delete an audience |
| `list_categories` | owner | List categories |
| `upsert_category` | owner | Create or update a category |
| `merge_categories` | owner | Merge one category into another |
| `compact` | owner | Dedupe, contradictions, decay, size budget, category merge/split (dry run by default) |
| `list_changes` | owner | Change log |
| `undo_change` | owner | Undo a change |
| `list_analyzers` | owner | List analyzers |
| `get_analyzer` | owner | One analyzer with its source and state keys |
| `update_analyzer` | owner | Enable, disable or retire an analyzer |
| `list_audit` | owner | Audit log of `predict` calls (no text) |
| `export_persona` | owner | Export the persona as `persona.v1.json` |
| `import_persona` | owner | Import a persona (replace or merge) |
| `delete_persona` | owner | Delete the persona's content, settings, audit and jobs |
| `get_settings` | owner | Read persona settings, limits and usage |
| `update_settings` | owner | Change persona settings, including the LLM backend |

See [SPEC.md §6](SPEC.md#6-mcp-tools) for arguments and results.

## Configuration

Built-in defaults, then `wwyd.toml` in the data directory, then `WWYD_*` environment
variables.

| Key | Env | Meaning |
|---|---|---|
| `public_url` | `WWYD_PUBLIC_URL` | External URL and OAuth issuer; https unless localhost |
| `host`, `port` | `WWYD_HOST`, `WWYD_PORT` | Bind address, default `127.0.0.1:8740` |
| `trusted_proxies` | `WWYD_TRUSTED_PROXIES` (comma-separated) | Peers whose `X-Forwarded-For` is trusted |
| `smtp_*` | `WWYD_SMTP_*` | Optional mail for password reset links |
| `[limits]` | `WWYD_LIMIT_<NAME>` | Instance default limits (SPEC §11), e.g. `spend_cap_usd` |
| `[llm]` | `WWYD_LLM_<NAME>` | Instance default LLM backend |
| `[auth] allow_insecure_redirects` | `WWYD_AUTH_ALLOW_INSECURE_REDIRECTS` | Accept http OAuth redirect URIs on non-loopback hosts (default `true`; set `false` on public hosted instances) |

The `[llm]` table has `base_url`, `model`, `api_key`, `price_in_per_m` and
`price_out_per_m` (USD per million tokens). Unset values keep the built-in LocalAI default
(`http://127.0.0.1:1240/v1`, `qwen3-vl-8b-instruct`). Each persona can override the backend
in its settings. The API key is never logged.
`allow_private_base_urls` (`WWYD_LLM_ALLOW_PRIVATE_BASE_URLS`, default `true`): when
`false`, a persona's own `base_url` must resolve to public addresses only (SSRF guard; the
instance backend is exempt). Set it to `false` on multi-user hosted instances.

```toml
public_url = "https://wwyd.example.com"
trusted_proxies = ["127.0.0.1"]

[limits]
spend_cap_usd = 5

[llm]
base_url = "https://openrouter.ai/api/v1"
model = "some/model"
api_key = "..."
price_in_per_m = 0.2
price_out_per_m = 0.8
```

## Data location

Data lives in `~/.wwyd/` (`wwyd.db`, `secret.key`, optional `wwyd.toml`). Set
`WWYD_DATA_DIR` to use another directory.

The secret key encrypts stored LLM API keys. Supply it with `WWYD_SECRET_KEY` (the Fernet
key itself) or `WWYD_SECRET_KEY_FILE` (a path); otherwise `<data dir>/secret.key` is
generated on first start, and a backup of the data directory then includes the key.

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
