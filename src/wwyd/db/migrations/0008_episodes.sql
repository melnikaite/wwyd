-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0008: episodes and organic categories (SPEC §4.4, §4.6, D17, D21). Paths, IDs and label
-- names only; still no column that could hold raw episodes (P1).

-- Where a category came from. Only 'seed' and 'owner' categories are guard vocabulary
-- (§7.7); 'episode' categories were created from ingest_raw paths. Rows inserted without
-- an origin are the per-persona seeds (store.personas); everything else names its origin.
ALTER TABLE categories ADD COLUMN origin TEXT NOT NULL DEFAULT 'seed'
    CHECK (origin IN ('seed', 'owner', 'episode'));
UPDATE categories SET origin = 'owner' WHERE path NOT IN (
    'communication', 'communication/style', 'communication/boundaries', 'work',
    'work/meetings', 'work/decisions', 'schedule', 'schedule/routine', 'food', 'travel',
    'money', 'health', 'family', 'hobbies', 'tech', 'other');

-- An old path keeps routing incoming episodes to the category it was merged or renamed into.
CREATE TABLE category_aliases (
    persona_id  TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (persona_id, path)
);
CREATE INDEX category_aliases_category ON category_aliases(category_id);

-- Guarded label and tag names per category (§7.3); never value -> label mappings.
CREATE TABLE category_labels (
    persona_id  TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    field       TEXT NOT NULL CHECK (field IN ('decision', 'situation', 'tag')),
    label       TEXT NOT NULL,
    PRIMARY KEY (category_id, field, label)
);
CREATE INDEX category_labels_persona ON category_labels(persona_id);

-- The op CHECK gains 'category_create' and 'category_split': the table is rebuilt.
CREATE TABLE changes_new (
    id         TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    op         TEXT NOT NULL CHECK (op IN ('merge', 'absorb', 'supersede', 'decay',
                 'archive_budget', 'purge', 'restore', 'category_create', 'category_merge',
                 'category_rename', 'category_split')),
    state      TEXT NOT NULL CHECK (state IN ('applied', 'proposed', 'dismissed', 'undone', 'expired')),
    reason     TEXT,
    job_id     TEXT,
    payload    TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO changes_new(rowid, id, persona_id, ts, op, state, reason, job_id, payload)
    SELECT rowid, id, persona_id, ts, op, state, reason, job_id, payload FROM changes;
DROP TABLE changes;
ALTER TABLE changes_new RENAME TO changes;
CREATE INDEX changes_persona_ts ON changes(persona_id, ts);
CREATE INDEX changes_persona_state ON changes(persona_id, state);
