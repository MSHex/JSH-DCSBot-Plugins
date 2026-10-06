CREATE TABLE IF NOT EXISTS awardautomation_state (
    player_ucid TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('collecting', 'awarded_locked')),
    cycle_number INTEGER NOT NULL DEFAULT 1 CHECK (cycle_number > 0),
    last_awarded_at TIMESTAMP,
    last_reset_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    PRIMARY KEY (player_ucid, rule_key),
    FOREIGN KEY (player_ucid) REFERENCES players (ucid) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS awardautomation_ledger (
    id BIGSERIAL PRIMARY KEY,
    player_ucid TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    cycle_number INTEGER NOT NULL CHECK (cycle_number > 0),
    award_id INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    configured_credit_reward INTEGER NOT NULL CHECK (configured_credit_reward >= 0),
    credited_amount INTEGER NOT NULL CHECK (credited_amount >= 0),
    old_credits INTEGER NOT NULL,
    new_credits INTEGER NOT NULL,
    granted_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    UNIQUE (player_ucid, rule_key, cycle_number),
    FOREIGN KEY (player_ucid) REFERENCES players (ucid) ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (award_id) REFERENCES logbook_awards (id) ON DELETE CASCADE,
    FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_awardautomation_ledger_player
    ON awardautomation_ledger (player_ucid, granted_at DESC);

CREATE INDEX IF NOT EXISTS idx_awardautomation_statistics_player_mission
    ON statistics (player_ucid, mission_id);
