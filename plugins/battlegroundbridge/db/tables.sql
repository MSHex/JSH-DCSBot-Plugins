-- Bridge-owned metadata. Created by normal plugin initialisation, under the
-- bot's own database role, so the objects end up owned by the role that uses
-- them. Creating them by hand as `postgres` produces a working-looking setup
-- that fails on the first write.
--
-- Nothing here duplicates Battleground's data. bg_geometry2 stays the single
-- source of truth for what is on the map; these tables only record which of
-- those rows the bridge created, who owns them, and what has already been
-- processed.

-- Bridge geometry IDs start high so they cannot collide with bg_geometry2's own
-- SERIAL (which starts at 1) or with bg_geometry_id_seq (which starts at
-- 20000), and so they clear the `geo.Id > 10000` guard in DCSBattleground's
-- delete path -- below that, the website refuses to delete them.
CREATE SEQUENCE IF NOT EXISTS bgb_geometry_id_seq
    INCREMENT 1 START 1000000 MINVALUE 1000000 MAXVALUE 2147483647 CACHE 1;

-- One row per mission session. The token is generated inside the mission Lua
-- environment, so it changes when the mission is reloaded and survives a bot
-- restart while DCS keeps running -- which is exactly the distinction needed to
-- decide between "update the existing report" and "this is a new mission".
CREATE TABLE IF NOT EXISTS bgb_sessions (
    id SERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    mission_token TEXT NOT NULL,
    mission_name TEXT,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
    last_seen TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
    UNIQUE (server_name, mission_token),
    FOREIGN KEY (server_name) REFERENCES servers (server_name) ON UPDATE CASCADE ON DELETE CASCADE
);

-- What the bridge created, and for whom.
--
-- geometry_id deliberately has NO foreign key to bg_geometry2. The website can
-- delete a row from under us at any time, and the mapping has to outlive that:
-- a missing row means "already gone", not a constraint violation. deleted_at is
-- a tombstone rather than a DELETE for the same reason -- it is what stops a
-- replayed or queued event recreating a report somebody deliberately removed.
CREATE TABLE IF NOT EXISTS bgb_items (
    id SERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    mission_token TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('intel', 'drawing')),
    marker_id INTEGER,
    geometry_id INTEGER NOT NULL,
    owner_ucid TEXT,
    owner_name TEXT,
    title TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
    content_changed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
    deleted_at TIMESTAMP WITHOUT TIME ZONE,
    UNIQUE (server_name, geometry_id),
    FOREIGN KEY (server_name) REFERENCES servers (server_name) ON UPDATE CASCADE ON DELETE CASCADE
);

-- A marker ID is only unique within one mission, because DCS reuses them in the
-- next one. The tombstoned rows are intentionally inside this index: once a
-- report has been deleted, the same marker must not be able to recreate it --
-- new intel needs a new marker.
CREATE UNIQUE INDEX IF NOT EXISTS bgb_items_marker_uq
    ON bgb_items (server_name, mission_token, marker_id)
    WHERE kind = 'intel' AND marker_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS bgb_items_live_idx
    ON bgb_items (server_name, kind) WHERE deleted_at IS NULL;

-- Processed event sequences, so a retry after a commit is a no-op and a retry
-- before one still runs. The sequence number comes from the mission, which
-- increments it per marker event.
CREATE TABLE IF NOT EXISTS bgb_receipts (
    server_name TEXT NOT NULL,
    mission_token TEXT NOT NULL,
    marker_id INTEGER NOT NULL,
    sequence BIGINT NOT NULL,
    processed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
    PRIMARY KEY (server_name, mission_token, marker_id, sequence),
    FOREIGN KEY (server_name) REFERENCES servers (server_name) ON UPDATE CASCADE ON DELETE CASCADE
);

-- Append-only history. There is no automatic pruning: this table and
-- bgb_receipts grow with use, and are the only record of who deleted what.
-- Prune them yourself if they ever become a problem.
CREATE TABLE IF NOT EXISTS bgb_audit (
    id BIGSERIAL PRIMARY KEY,
    server_name TEXT NOT NULL,
    at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
    action TEXT NOT NULL,
    geometry_id INTEGER,
    marker_id INTEGER,
    actor_ucid TEXT,
    actor_name TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS bgb_audit_server_idx ON bgb_audit (server_name, at DESC);
