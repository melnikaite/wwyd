-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0007: partial ingest jobs and per-source watermarks (SPEC §4.6, §7.1). Indices,
-- timestamps and counts only; still no column that could hold raw records (P1).

-- The status CHECK gains 'partial': SQLite cannot alter a CHECK, so the table is rebuilt.
CREATE TABLE jobs_new (
    id                TEXT PRIMARY KEY,
    persona_id        TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    status            TEXT NOT NULL CHECK (status IN
                          ('queued', 'running', 'done', 'partial', 'failed', 'interrupted')),
    progress          REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    counts            TEXT NOT NULL DEFAULT '{}',
    error_code        TEXT,
    remaining_records TEXT,  -- JSON array of batch indices, never record content
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
INSERT INTO jobs_new(id, persona_id, status, progress, counts, error_code, created_at,
                     started_at, finished_at)
    SELECT id, persona_id, status, progress, counts, error_code, created_at, started_at,
           finished_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX jobs_persona_created ON jobs(persona_id, created_at);
CREATE INDEX jobs_status ON jobs(status) WHERE status IN ('queued', 'running');

-- Per-source watermark: the largest processed record timestamp (UTC ISO 8601).
CREATE TABLE ingest_sources (
    persona_id    TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    last_ts       TEXT,
    last_job_at   TEXT NOT NULL,
    records_total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (persona_id, source)
);
