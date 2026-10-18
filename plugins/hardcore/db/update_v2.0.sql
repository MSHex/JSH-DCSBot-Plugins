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
