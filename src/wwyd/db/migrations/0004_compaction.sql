-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0004: compaction (SPEC §10).
-- `decayed_at` is the reference point of the last applied decay step, so nightly decay
-- compounds exactly (c × 0.5^(Δ/6 months) is memoryless) without a stored base value.

ALTER TABLE claims ADD COLUMN decayed_at TEXT;

CREATE INDEX changes_persona_state ON changes(persona_id, state);

-- Last nightly compaction run per persona (UTC timestamp).
CREATE TABLE compaction_runs (
    persona_id      TEXT PRIMARY KEY REFERENCES personas(id) ON DELETE CASCADE,
    last_nightly_at TEXT NOT NULL
);
