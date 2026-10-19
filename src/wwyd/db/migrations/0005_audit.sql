-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0005: predict audit log (SPEC §4.6, P6). No text columns for content: never the situation,
-- facts or answer. client_name is the registered client's name; outcome is a fixed code.

CREATE TABLE audit (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    id                TEXT NOT NULL UNIQUE,
    persona_id        TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    ts                TEXT NOT NULL,
    client_name       TEXT,
    grant_id          TEXT,
    api_key_id        TEXT,
    scope             TEXT NOT NULL CHECK (scope IN ('owner', 'read', 'respond')),
    audience_id       TEXT,
    audience_fallback INTEGER NOT NULL DEFAULT 0 CHECK (audience_fallback IN (0, 1)),
    outcome           TEXT NOT NULL CHECK (outcome IN ('answer', 'needs') OR outcome LIKE 'escalate:%'),
    confidence        REAL,
    llm_tokens        INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX audit_persona_seq ON audit(persona_id, seq);
CREATE INDEX audit_ts ON audit(ts);
