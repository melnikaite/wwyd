-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0006: ingest jobs (SPEC §4.6, §7.1). Counts and status only; there is deliberately no
-- column that could hold raw records (P1).

CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    status      TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'interrupted')),
    progress    REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    counts      TEXT NOT NULL DEFAULT '{}',
    error_code  TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX jobs_persona_created ON jobs(persona_id, created_at);
CREATE INDEX jobs_status ON jobs(status) WHERE status IN ('queued', 'running');
