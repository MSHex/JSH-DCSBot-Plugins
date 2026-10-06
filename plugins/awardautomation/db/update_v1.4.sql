CREATE INDEX IF NOT EXISTS idx_awardautomation_statistics_player_mission
    ON statistics (player_ucid, mission_id);
