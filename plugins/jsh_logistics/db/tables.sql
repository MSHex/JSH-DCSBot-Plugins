-- One row per delivery type per banked landing, or per airtime payout.
CREATE TABLE IF NOT EXISTS jsh_logistics_events (
    id SERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    player_ucid TEXT,
    player_name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'delivery' or 'airtime'
    what TEXT NOT NULL,              -- Foothold stat name, or aircraft type
    amount INTEGER NOT NULL,         -- deliveries, or airborne seconds
    points INTEGER NOT NULL DEFAULT 0,
    time TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS idx_jsh_logistics_events_ucid ON jsh_logistics_events (player_ucid, time);
