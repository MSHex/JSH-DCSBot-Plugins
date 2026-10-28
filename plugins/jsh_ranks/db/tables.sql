-- Manually granted ranks.
--
-- The Discord ladder in creditsystem.yaml is earned: credits and playtime
-- decide it, and a rung set beyond either -- JSH's sixteenth is deliberately
-- out of reach -- can never be reached by flying. This table is how such a
-- rank is given out, and how any rank is pinned regardless of what a pilot
-- has earned.
--
-- A grant names its subject by UCID, by Discord ID, or by both:
--
--   ucid        pins the rank to one DCS account. Survives a Discord account
--               being unlinked, relinked or left behind.
--   discord_id  pins it to the person. Follows them onto a second DCS
--               install, and can be written before they have ever connected,
--               which a UCID cannot.
--
-- One row per identifier, so re-granting is an upsert rather than a pile of
-- history. There is deliberately NO foreign key to players(ucid): a grant for
-- somebody who has not connected yet is the normal case for the Discord form,
-- and an FK would reject it.
CREATE TABLE IF NOT EXISTS jsh_rank_grants (
    id SERIAL PRIMARY KEY,
    ucid TEXT,
    discord_id BIGINT,
    rank TEXT NOT NULL,
    reason TEXT,
    granted_by TEXT,
    granted_at TIMESTAMP NOT NULL DEFAULT (timezone('utc'::text, now())),
    CONSTRAINT jsh_rank_grants_subject CHECK (ucid IS NOT NULL OR discord_id IS NOT NULL),
    CONSTRAINT jsh_rank_grants_discord_real CHECK (discord_id IS NULL OR discord_id > 0)
);

-- Partial, because several rows may legitimately have a NULL in either column.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jsh_rank_grants_ucid
    ON jsh_rank_grants (ucid) WHERE ucid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jsh_rank_grants_discord
    ON jsh_rank_grants (discord_id) WHERE discord_id IS NOT NULL;
