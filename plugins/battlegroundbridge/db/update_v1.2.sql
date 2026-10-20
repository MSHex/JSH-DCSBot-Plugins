-- 1.2 -> 1.3: generated threat rings.
--
-- A ring attached to marker intel has to follow its report: move with the
-- marker, and go when the report goes. That needs a parent link and a new
-- kind. Rings generated from mission drawings are stored as 'drawing' and need
-- neither, since the import replaces all of its own rows on every run.

ALTER TABLE bgb_items DROP CONSTRAINT IF EXISTS bgb_items_kind_check;
ALTER TABLE bgb_items ADD CONSTRAINT bgb_items_kind_check
    CHECK (kind IN ('intel', 'drawing', 'ring'));

ALTER TABLE bgb_items ADD COLUMN IF NOT EXISTS parent_id INTEGER
    REFERENCES bgb_items (id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS bgb_items_parent_idx
    ON bgb_items (parent_id) WHERE parent_id IS NOT NULL AND deleted_at IS NULL;
