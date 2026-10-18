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

[Unreleased]: https://github.com/melnikaite/wwyd/commits/main
