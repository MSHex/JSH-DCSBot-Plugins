-- 1.0 -> 1.1: split the totals into an all-servers total and a dynamic campaign
-- breakdown by pilot status, and add reason to the event log.
ALTER TABLE jsh_csar_rescues ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE jsh_csar_rescues ADD COLUMN IF NOT EXISTS dynamic BOOLEAN NOT NULL DEFAULT FALSE;

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

-- Old jsh_csar_totals was keyed (ucid, pilot_status); rebuild it per player.
ALTER TABLE jsh_csar_totals RENAME TO jsh_csar_totals_v1;
CREATE TABLE jsh_csar_totals (
    player_ucid TEXT NOT NULL PRIMARY KEY,
    rescues INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT,
    last_rescue TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    FOREIGN KEY (player_ucid) REFERENCES players (ucid) ON UPDATE CASCADE ON DELETE CASCADE
);
INSERT INTO jsh_csar_totals (player_ucid, rescues, points, last_rescue)
SELECT player_ucid, SUM(rescues), SUM(points), MAX(last_rescue)
FROM jsh_csar_totals_v1 GROUP BY player_ucid;
DROP TABLE jsh_csar_totals_v1;
