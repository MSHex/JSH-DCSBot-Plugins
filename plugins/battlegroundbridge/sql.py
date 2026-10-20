"""
Every SQL statement the bridge runs, as plain strings.

Kept in a module of its own with no imports at all, so the statements can be
executed against a real PostgreSQL instance by a test that has no psycopg,
no discord and no bot. That is not a stylistic choice: it is the only way the
SQL in this plugin gets verified in an environment where the driver cannot be
installed.

All parameterised. No string interpolation of values anywhere.
"""

# --------------------------------------------------------------- sessions

TOUCH_SESSION = """
    INSERT INTO bgb_sessions (server_name, mission_token, mission_name)
    VALUES (%s, %s, %s)
    ON CONFLICT (server_name, mission_token) DO UPDATE
       SET last_seen = timezone('utc'::text, now()),
           mission_name = COALESCE(EXCLUDED.mission_name, bgb_sessions.mission_name)
    RETURNING id, started_at
"""

LATEST_SESSION = """
    SELECT mission_token, mission_name, started_at
      FROM bgb_sessions
     WHERE server_name = %s
     ORDER BY last_seen DESC
     LIMIT 1
"""

# --------------------------------------------------------------- receipts
#
# Claimed inside the same transaction as the work it guards. If the transaction
# rolls back the claim rolls back with it, so a retry still runs; if it commits,
# the ON CONFLICT makes every later retry a no-op. Returning no row means
# "already done".
CLAIM_RECEIPT = """
    INSERT INTO bgb_receipts (server_name, mission_token, marker_id, sequence)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (server_name, mission_token, marker_id, sequence) DO NOTHING
    RETURNING processed_at
"""

# --------------------------------------------------------------- items

NEXT_GEOMETRY_ID = "SELECT nextval('bgb_geometry_id_seq')"

#: Locked for update: a concurrent delete-by-ID for the same marker must wait
#: rather than interleave with an update.
FIND_ITEM_BY_MARKER = """
    SELECT id, geometry_id, owner_ucid, owner_name, title, deleted_at
      FROM bgb_items
     WHERE server_name = %s AND mission_token = %s AND marker_id = %s
       AND kind = 'intel'
     FOR UPDATE
"""

#: Scoped to the server on purpose. An ID from another server must come back
#: empty, not be found and deleted.
FIND_ITEM_BY_GEOMETRY = """
    SELECT id, geometry_id, marker_id, owner_ucid, owner_name, title, deleted_at
      FROM bgb_items
     WHERE server_name = %s AND geometry_id = %s
     FOR UPDATE
"""

INSERT_ITEM = """
    INSERT INTO bgb_items (server_name, mission_token, kind, marker_id, geometry_id,
                           owner_ucid, owner_name, title, parent_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING id
"""

#: The generated ring belonging to one intel report, if it still has one.
FIND_RING_BY_PARENT = """
    SELECT id, geometry_id, title
      FROM bgb_items
     WHERE parent_id = %s AND kind = 'ring' AND deleted_at IS NULL
     FOR UPDATE
"""

TOUCH_ITEM_CONTENT = """
    UPDATE bgb_items
       SET content_changed_at = timezone('utc'::text, now()), title = %s
     WHERE id = %s
"""

TOMBSTONE_ITEM = """
    UPDATE bgb_items
       SET deleted_at = timezone('utc'::text, now())
     WHERE id = %s AND deleted_at IS NULL
    RETURNING geometry_id
"""

LIVE_DRAWING_IDS = """
    SELECT id, geometry_id
      FROM bgb_items
     WHERE server_name = %s AND kind = 'drawing' AND deleted_at IS NULL
"""

LIVE_INTEL_IDS = """
    SELECT id, geometry_id
      FROM bgb_items
     WHERE server_name = %s AND kind = 'intel' AND deleted_at IS NULL
"""

#: Expiry runs on the last DCS-driven content change, so website-only edits
#: never extend an intel report's life.
EXPIRED_INTEL = """
    SELECT id, geometry_id, title
      FROM bgb_items
     WHERE server_name = %s AND kind = 'intel' AND deleted_at IS NULL
       AND content_changed_at < timezone('utc'::text, now()) - make_interval(hours => %s)
"""

# ------------------------------------------------------------- bg_geometry2
#
# The website's table. Writes here are always qualified by server_name as well
# as id, so a wrong or hostile ID cannot reach another server's map.

#: `time` is not cosmetic. DCSBattleground publishes a row to open maps only
#: when it falls inside a window measured from this column
#: (session.go: `extract(EPOCH FROM now() - time) < RadarRefreshRate * 3`), so a
#: change that does not bump it is written to the database and never broadcast.
INSERT_GEOMETRY = """
    INSERT INTO bg_geometry2 (id, server_name, data, "time")
    VALUES (%s, %s, %s, timezone('utc'::text, now()))
"""

SELECT_GEOMETRY_FOR_UPDATE = """
    SELECT data
      FROM bg_geometry2
     WHERE id = %s AND server_name = %s
     FOR UPDATE
"""

UPDATE_GEOMETRY = """
    UPDATE bg_geometry2
       SET data = %s, "time" = timezone('utc'::text, now())
     WHERE id = %s AND server_name = %s
"""

#: Only ever called with an ID the bridge created, having already confirmed it
#: in bgb_items for this server. That is what keeps website-created geometry,
#: imported drawings and unrelated recon records out of reach.
DELETE_GEOMETRY = """
    DELETE FROM bg_geometry2
     WHERE id = %s AND server_name = %s
"""

# --------------------------------------------------------------- audit

AUDIT = """
    INSERT INTO bgb_audit (server_name, action, geometry_id, marker_id,
                           actor_ucid, actor_name, detail)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
