CREATE TABLE IF NOT EXISTS hardcore_status (
    player_ucid TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    changed_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    changed_by TEXT NOT NULL DEFAULT 'USER',
    FOREIGN KEY (player_ucid) REFERENCES players(ucid)
        ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hardcore_wallet_carry (
    campaign_id INTEGER NOT NULL,
    player_ucid TEXT NOT NULL,
    half_credit_units INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (campaign_id, player_ucid),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (player_ucid) REFERENCES players(ucid)
        ON UPDATE CASCADE ON DELETE CASCADE,
    CHECK (half_credit_units >= 0 AND half_credit_units < 2)
);

CREATE TABLE IF NOT EXISTS hardcore_sessions (
    id BIGSERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    player_ucid TEXT NOT NULL,
    campaign_id INTEGER NOT NULL,
    player_name TEXT,

    connected_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    last_seen_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),

    segment_started_at TIMESTAMP,
    eligible_seconds BIGINT NOT NULL DEFAULT 0,

    hardcore_at_start BOOLEAN NOT NULL DEFAULT FALSE,
    hardcore_revoked BOOLEAN NOT NULL DEFAULT FALSE,
    deaths INTEGER NOT NULL DEFAULT 0,

    airborne BOOLEAN NOT NULL DEFAULT FALSE,
    last_loss_at TIMESTAMP,
    last_loss_reason TEXT,

    settled BOOLEAN NOT NULL DEFAULT FALSE,
    payout INTEGER,
    gross_half_units BIGINT,
    penalty_percent INTEGER,
    settled_at TIMESTAMP,

    FOREIGN KEY (server_name) REFERENCES servers(server_name)
        ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (player_ucid) REFERENCES players(ucid)
        ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS hardcore_one_open_session
    ON hardcore_sessions (server_name, player_ucid)
    WHERE settled = FALSE;

CREATE INDEX IF NOT EXISTS hardcore_sessions_ucid_idx
    ON hardcore_sessions (player_ucid, settled);

CREATE TABLE IF NOT EXISTS hardcore_events (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL,
    event_time TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    event_type TEXT NOT NULL,
    details TEXT,
    FOREIGN KEY (session_id) REFERENCES hardcore_sessions(id) ON DELETE CASCADE
);
ALTER TABLE hardcore_status ADD COLUMN IF NOT EXISTS ejections_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS aircraft_seconds BIGINT NOT NULL DEFAULT 0;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS support_seconds BIGINT NOT NULL DEFAULT 0;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS segment_role TEXT NOT NULL DEFAULT 'aircraft';
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS aircraft_gross_half_units BIGINT;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS support_gross_half_units BIGINT;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS ejections_used_at_end INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS protected BOOLEAN;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS protection_place TEXT;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS telemetry_at TIMESTAMP;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS telemetry_seq BIGINT NOT NULL DEFAULT -1;
ALTER TABLE hardcore_sessions ADD COLUMN IF NOT EXISTS telemetry_epoch TEXT;
ALTER TABLE hardcore_events ADD COLUMN IF NOT EXISTS event_key TEXT;
ALTER TABLE hardcore_events ADD COLUMN IF NOT EXISTS spawn_key TEXT;
ALTER TABLE hardcore_events ADD COLUMN IF NOT EXISTS protected BOOLEAN;
ALTER TABLE hardcore_events ADD COLUMN IF NOT EXISTS counted BOOLEAN NOT NULL DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS hardcore_event_once ON hardcore_events (event_key) WHERE event_key IS NOT NULL;
-- Historic time was entirely aircraft time. Do not reinterpret historic losses.
UPDATE hardcore_sessions SET aircraft_seconds = eligible_seconds,
    aircraft_gross_half_units = gross_half_units, support_gross_half_units = 0
    WHERE aircraft_gross_half_units IS NULL AND support_gross_half_units IS NULL
      AND aircraft_seconds = 0 AND support_seconds = 0;
