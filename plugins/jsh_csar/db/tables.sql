-- Event log: one row per rescue event, per pilot status, from every CSAR script.
CREATE TABLE IF NOT EXISTS jsh_csar_rescues (
    id SERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    player_ucid TEXT,
    player_name TEXT NOT NULL,
    pilot_status TEXT NOT NULL,
    rescues INTEGER NOT NULL DEFAULT 1,
    points INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    source TEXT,
    dynamic BOOLEAN NOT NULL DEFAULT FALSE,
    rescued_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS idx_jsh_csar_rescues_ucid ON jsh_csar_rescues (player_ucid, rescued_at);

-- Dynamic campaign only: totals per player and pilot status.
-- This is the per-status breakdown another plugin builds awards from.
CREATE TABLE IF NOT EXISTS jsh_csar_dynamic (
    player_ucid TEXT NOT NULL,
    pilot_status TEXT NOT NULL,
    rescues INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT,
    last_rescue TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    PRIMARY KEY (player_ucid, pilot_status),
    FOREIGN KEY (player_ucid) REFERENCES players (ucid) ON UPDATE CASCADE ON DELETE CASCADE
);

-- Every server and every CSAR script, dynamic campaign included: one row per
-- player. This is the "pilots rescued" figure for any plugin that wants it.
CREATE TABLE IF NOT EXISTS jsh_csar_totals (
    player_ucid TEXT NOT NULL PRIMARY KEY,
    rescues INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT,
    last_rescue TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    FOREIGN KEY (player_ucid) REFERENCES players (ucid) ON UPDATE CASCADE ON DELETE CASCADE
);
