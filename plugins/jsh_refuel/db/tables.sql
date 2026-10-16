-- One row per completed air-to-air refuelling, whether or not it paid.
-- Rows with points = 0 are the ones below minimum_lbs or past the sortie cap;
-- keeping them makes the totals a true record of fuel taken.
CREATE TABLE IF NOT EXISTS jsh_refuel_log (
    id SERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    player_ucid TEXT,
    player_name TEXT NOT NULL,
    aircraft TEXT,
    lbs INTEGER NOT NULL DEFAULT 0,
    paid_lbs INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    refuelled_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS idx_jsh_refuel_log_ucid ON jsh_refuel_log (player_ucid, refuelled_at);

-- Lifetime totals per player, across every server.
CREATE TABLE IF NOT EXISTS jsh_refuel_totals (
    player_ucid TEXT NOT NULL PRIMARY KEY,
    refuellings INTEGER NOT NULL DEFAULT 0,
    lbs BIGINT NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    last_aircraft TEXT,
    last_refuel TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    FOREIGN KEY (player_ucid) REFERENCES players (ucid) ON UPDATE CASCADE ON DELETE CASCADE
);
