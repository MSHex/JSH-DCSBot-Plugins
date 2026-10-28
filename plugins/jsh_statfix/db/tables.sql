-- One row per repair run, so a run is never guessed at afterwards and the
-- schedule survives a bot restart: the next run is due relative to the last
-- SUCCESSFUL run recorded here, not to when this process happened to start.
CREATE TABLE IF NOT EXISTS jsh_statfix_runs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP NOT NULL DEFAULT (timezone('utc'::text, now())),
    finished_at TIMESTAMP,
    dry_run BOOLEAN NOT NULL DEFAULT FALSE,
    triggered_by TEXT NOT NULL DEFAULT 'schedule',
    orphans_closed INTEGER NOT NULL DEFAULT 0,
    corrected_by_events INTEGER NOT NULL DEFAULT 0,
    overlaps_clipped INTEGER NOT NULL DEFAULT 0,
    hours_before NUMERIC,
    hours_after NUMERIC,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jsh_statfix_runs_finished
    ON jsh_statfix_runs (finished_at DESC NULLS LAST);
