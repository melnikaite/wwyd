-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0009: analyzers and their mergeable state (SPEC §4.6, §7.2). Rows hold names, counts and
-- (LLM analyzers only, guarded) source and statement; state holds numbers under filtered
-- keys. Nothing here can hold raw episodes (P1).

CREATE TABLE analyzers (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    id                 TEXT NOT NULL UNIQUE,
    persona_id         TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    -- Polymorphic scope: a category id or an audience id (not an FK; queries resolve it).
    scope_kind         TEXT NOT NULL CHECK (scope_kind IN ('category', 'audience')),
    scope_id           TEXT NOT NULL,
    origin             TEXT NOT NULL CHECK (origin IN ('builtin', 'llm')),
    name               TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    statement_template TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL CHECK (kind IN
                         ('trait', 'preference', 'habit', 'decision_rule', 'style', 'boundary')),
    source             TEXT NOT NULL DEFAULT '' CHECK (length(CAST(source AS BLOB)) <= 20480),
    status             TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'disabled', 'retired')),
    runs               INTEGER NOT NULL DEFAULT 0,
    failures           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    last_run_at        TEXT,
    claim_id           TEXT
);
CREATE INDEX analyzers_persona_scope ON analyzers(persona_id, scope_kind, scope_id);
-- One builtin row per (name, scope).
CREATE UNIQUE INDEX analyzers_builtin_scope ON analyzers(persona_id, name, scope_kind, scope_id)
    WHERE origin = 'builtin';

CREATE TABLE analyzer_state (
    analyzer_id TEXT NOT NULL REFERENCES analyzers(id) ON DELETE CASCADE,
    month       TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    metric      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       REAL NOT NULL,
    PRIMARY KEY (analyzer_id, month, metric, key)
) WITHOUT ROWID;
