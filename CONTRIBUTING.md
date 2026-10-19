# Contributing to wwyd

Thanks for your interest. wwyd is pre-alpha, so expect things to move.

## Spec first

[SPEC.md](SPEC.md) is the source of truth for the design. Read the sections relevant to
your change first. If a change does not fit the spec's data model or rules, propose the
spec change in the same pull request (or open a feature request first) rather than working
around it.

## Setup

wwyd uses [uv](https://docs.astral.sh/uv/) only (no pip, no system packages).

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Tests must pass and lint must be clean before review. The default test run needs no
network; tests that need a live LLM are marked `llm` and skipped by default.

### Sandbox assets

LLM-written analyzers run in CPython-for-WASI under wasmtime ([SPEC.md §7.5](SPEC.md)).
The interpreter and stdlib zip are build artifacts in `src/wwyd/sandbox/assets/`
(gitignored, shipped in the wheel). Without them the server still starts and LLM analyzers
are skipped, and `tests/test_sandbox.py` is skipped. To build them:

```sh
uv run python -I scripts/build_sandbox.py              # downloads the sha256-pinned archive
uv run python -I scripts/build_sandbox.py --zip PATH   # or use a local copy of it
```

The helper module `src/wwyd/sandbox/helpers/wwyd_helpers.py` is injected into the guest at
run time, so editing it needs no rebuild.

## Privacy invariants

Every pull request must respect these ([SPEC.md §2](SPEC.md#2-privacy-invariants)):

- [ ] Never log tool arguments, request bodies or LLM prompts/responses, at any level.
- [ ] Never persist raw input (DB, logs, error reports, exports, change log).
- [ ] No contact identities: no names, phones, emails, messenger IDs, or hashes of them.
- [ ] Third-party visibility is filtered in SQL, never by the prompt.

## Test data

Test data must be synthetic. Never commit real conversations, real contact data, tokens
or exported personas, not even "anonymised" ones.

## Style

- English only: code, comments, docs, commit messages.
- Small, focused pull requests; one topic each.
- Conventional Commits style (`feat:`, `fix:`, `docs:` …) is optional but preferred.
- Add user-visible changes to [CHANGELOG.md](CHANGELOG.md) under `[Unreleased]`.

## License of contributions

wwyd is licensed under AGPL-3.0-or-later. Contributions are accepted under the same
license (inbound = outbound); there is no CLA.
