-- JSH Credits: per-event ledger.
--
-- One row per credit-earning event, written the moment it happens. 'paid' flips
-- to true when the player lands and collects; rows left false are credits that
-- were earned and then lost to a crash, eject, slot change or disconnect.
--
-- This is a debugging and analysis table, not a payment record. The balance and
-- the money trail live in CreditSystem's credits / credits_log.

CREATE TABLE IF NOT EXISTS jsh_credits_events (
    id              SERIAL PRIMARY KEY,
    instance        TEXT NOT NULL,       -- DCSServerBot instance name
    player_ucid     TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'kill',   -- 'kill' or 'mission'
    unit_type       TEXT,                           -- victim unit type, kills only
    category        TEXT,                           -- victim category, kills only
    reason          TEXT,                           -- award reason, mission only
    points          INTEGER NOT NULL DEFAULT 0,
    paid            BOOLEAN NOT NULL DEFAULT FALSE,
    time            TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    paid_time       TIMESTAMP,
    paid_place      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jsh_credits_events_ucid ON jsh_credits_events (player_ucid);
CREATE INDEX IF NOT EXISTS idx_jsh_credits_events_time ON jsh_credits_events (time);
CREATE INDEX IF NOT EXISTS idx_jsh_credits_events_paid ON jsh_credits_events (paid);
