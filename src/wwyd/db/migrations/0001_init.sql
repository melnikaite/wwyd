-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0001: users, personas, audiences, categories, claims (+ FTS), change log.
-- Auth, limits, audit and jobs tables arrive in later migrations.

CREATE TABLE users (
    id                  TEXT PRIMARY KEY,
    login               TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT,
    is_admin            INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    disabled_at         TEXT,
    created_at          TEXT NOT NULL,
    password_changed_at TEXT
);

CREATE TABLE personas (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    display_name    TEXT NOT NULL,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    settings        TEXT NOT NULL DEFAULT '{}',
    spend_capped_at TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE audiences (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (persona_id, name)
);
-- At most one default audience per persona; the store guarantees at least one.
CREATE UNIQUE INDEX audiences_one_default ON audiences(persona_id) WHERE is_default = 1;

CREATE TABLE categories (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    parent_id   TEXT REFERENCES categories(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (persona_id, path)
);
CREATE INDEX categories_parent ON categories(parent_id);

-- `seq` is an explicit rowid alias so the FTS external-content mapping survives VACUUM.
CREATE TABLE claims (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    id                TEXT NOT NULL UNIQUE,
    persona_id        TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL CHECK (kind IN
                        ('trait', 'preference', 'habit', 'decision_rule', 'style', 'boundary')),
    category_id       TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    text              TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 500),
    norm_text         TEXT NOT NULL,
    audiences_all     INTEGER NOT NULL DEFAULT 1 CHECK (audiences_all IN (0, 1)),
    visibility        TEXT NOT NULL DEFAULT 'shared' CHECK (visibility IN ('owner_only', 'shared')),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending', 'approved', 'rejected', 'archived')),
    confidence        REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    evidence          TEXT NOT NULL DEFAULT '{}',
    key               TEXT,
    origin            TEXT NOT NULL CHECK (origin IN ('derived', 'added', 'imported', 'owner')),
    proposed_category TEXT,
    superseded_by     TEXT,
    archive_reason    TEXT CHECK (archive_reason IS NULL OR archive_reason IN
                        ('merged', 'superseded', 'decayed', 'budget', 'owner')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    reviewed_at       TEXT,
    archived_at       TEXT,
    last_evidence_at  TEXT
);
CREATE INDEX claims_persona_status ON claims(persona_id, status);
CREATE INDEX claims_category ON claims(category_id);
CREATE INDEX claims_absorb ON claims(persona_id, kind, category_id, norm_text);
CREATE UNIQUE INDEX claims_key_live ON claims(persona_id, key)
    WHERE key IS NOT NULL AND status <> 'archived';

CREATE TABLE claim_audiences (
    claim_id    TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    audience_id TEXT NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
    PRIMARY KEY (claim_id, audience_id)
) WITHOUT ROWID;
CREATE INDEX claim_audiences_audience ON claim_audiences(audience_id);

CREATE VIRTUAL TABLE claims_fts USING fts5(
    text,
    content = 'claims',
    content_rowid = 'seq',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER claims_fts_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, text) VALUES (new.seq, new.text);
END;
CREATE TRIGGER claims_fts_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES ('delete', old.seq, old.text);
END;
CREATE TRIGGER claims_fts_au AFTER UPDATE OF text ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES ('delete', old.seq, old.text);
    INSERT INTO claims_fts(rowid, text) VALUES (new.seq, new.text);
END;

CREATE TABLE changes (
    id         TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    op         TEXT NOT NULL CHECK (op IN ('merge', 'absorb', 'supersede', 'decay',
                 'archive_budget', 'purge', 'restore', 'category_merge', 'category_rename')),
    state      TEXT NOT NULL CHECK (state IN ('applied', 'proposed', 'dismissed', 'undone', 'expired')),
    reason     TEXT,
    job_id     TEXT,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX changes_persona_ts ON changes(persona_id, ts);
