## What and why

<!-- What does this PR change, and why? Link the issue if there is one. -->

## Spec

- [ ] No spec change needed, or SPEC.md is updated in this PR (sections: )

## Privacy checklist (SPEC §2)

- [ ] Tool arguments, request bodies and LLM prompts/responses are never logged, at any level.
- [ ] Raw `ingest_raw` input is never persisted (DB, logs, error reports, exports, change log).
- [ ] No contact identities (names, phones, emails, messenger IDs, or hashes of them) are stored.
- [ ] Third-party visibility is filtered in SQL, never by the prompt.
- [ ] Test data is synthetic; no real conversations or personal data.

## Checks

- [ ] `uv run pytest -q`
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] CHANGELOG.md updated under `[Unreleased]` if user-visible
