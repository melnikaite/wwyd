-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0003: per-user limit overrides and daily usage counters (SPEC §11). Numbers only, no text.

CREATE TABLE limit_overrides (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    set_by     TEXT NOT NULL CHECK (set_by IN ('admin', 'user')),
    limits     TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, set_by)
) WITHOUT ROWID;

-- day is 'YYYY-MM-DD' in UTC; metric e.g. 'calls:predict', 'predict:aud:<id>', 'cost_usd'.
CREATE TABLE usage_daily (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     TEXT NOT NULL,
    metric  TEXT NOT NULL,
    value   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, metric)
) WITHOUT ROWID;
