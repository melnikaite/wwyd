-- SPDX-License-Identifier: AGPL-3.0-or-later
-- 0010: the claims a prediction used, as a JSON array of claim IDs (never text; SPEC §4.6,
-- D23), so a correction episode can be judged against exactly the claims behind the answer.
ALTER TABLE audit ADD COLUMN used_claims TEXT NOT NULL DEFAULT '[]';
