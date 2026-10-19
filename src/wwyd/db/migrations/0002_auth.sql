-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Milestone 2: accounts, web sessions, one-time links, OAuth clients/codes/grants/tokens,
-- API keys. Every secret (session cookie, link token, code, token, key) is stored only as
-- a SHA-256 hex digest.

ALTER TABLE users ADD COLUMN password_reset_by_admin_at TEXT;

CREATE TABLE web_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX web_sessions_user ON web_sessions(user_id);

CREATE TABLE one_time_links (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('setup', 'invite', 'reset')),
    user_id     TEXT REFERENCES users(id) ON DELETE CASCADE,   -- reset only
    created_by  TEXT,                                          -- admin user id, or NULL (CLI/first run)
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE oauth_clients (
    client_id   TEXT PRIMARY KEY,
    source      TEXT NOT NULL CHECK (source IN ('dcr', 'cimd')),
    client_name TEXT,
    metadata    TEXT NOT NULL,          -- OAuthClientInformationFull as JSON
    created_at  TEXT NOT NULL,
    fetched_at  TEXT                    -- cimd: when the metadata document was fetched
);

-- An /authorize request waiting for the user to sign in and consent.
CREATE TABLE pending_authorizations (
    id          TEXT PRIMARY KEY,       -- random, carried in the consent URL
    client_id   TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    params      TEXT NOT NULL,          -- AuthorizationParams as JSON
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE grants (
    id           TEXT PRIMARY KEY,      -- grt_
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id    TEXT NOT NULL,
    client_name  TEXT,
    scope        TEXT NOT NULL,
    resource     TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX grants_user ON grants(user_id);
CREATE INDEX grants_client ON grants(client_id);

-- The grant is created when the code is exchanged, so abandoned flows leave no grant.
CREATE TABLE oauth_codes (
    code_hash      TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id      TEXT NOT NULL,
    scope          TEXT NOT NULL,       -- the scope chosen on the consent page
    grant_id       TEXT,                -- set when the code is exchanged
    code_challenge TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL,
    resource       TEXT,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    used_at        TEXT
);

CREATE TABLE tokens (
    token_hash  TEXT PRIMARY KEY,
    grant_id    TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT,
    rotated_at  TEXT,                   -- refresh: when it was exchanged
    replaced_by TEXT                    -- refresh: hash of its successor
);
CREATE INDEX tokens_grant ON tokens(grant_id);

CREATE TABLE api_keys (
    id           TEXT PRIMARY KEY,      -- key_
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL UNIQUE,
    key_hash     TEXT NOT NULL,
    scope        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX api_keys_user ON api_keys(user_id);
