-- Checks the jsh_rank_grants DDL and every query jsh_ranks runs against it,
-- against a real PostgreSQL rather than in the head. Run it on a scratch
-- database -- it creates its own `players` stub and never touches a live one:
--
--   initdb -D /tmp/pg -A trust && pg_ctl -D /tmp/pg -o '-k /tmp' start
--   psql -h /tmp -q -f tests/ranks_grants_sql_check.sql
--
-- What it is actually guarding:
--
--   players.discord_id is NOT NULL DEFAULT -1 in DCSServerBot, so every
--   unlinked pilot carries -1. Without the `g.discord_id > 0` guard, one
--   stray -1 in a grant row would match every unlinked pilot on the server.
--   CHECK 1 fails loudly if that guard is ever dropped: 'ccc' is unlinked and
--   must not appear in the result.
--
--   A pilot can hold both a UCID grant and a Discord grant. CHECK 1 also
--   pins the precedence: 'ddd' must come back as the UCID one.

\set ON_ERROR_STOP on
CREATE TABLE players (ucid TEXT PRIMARY KEY, discord_id BIGINT NOT NULL DEFAULT -1, name TEXT);
\i plugins/jsh_ranks/db/tables.sql
-- idempotency: the migration file is the same DDL, and must be safe to re-run
\i plugins/jsh_ranks/db/update_v1.5.sql

INSERT INTO players (ucid, discord_id, name) VALUES
  ('aaa', 111, 'Hexe'),      -- linked, grant by ucid
  ('bbb', 222, 'Joker'),     -- linked, grant by discord only
  ('ccc',  -1, 'Ajax'),      -- UNLINKED: must not match anything
  ('ddd', 333, 'Both'),      -- has both a ucid grant and a discord grant
  ('eee', 444, 'Clean');     -- no grant

INSERT INTO jsh_rank_grants (ucid, discord_id, rank, reason, granted_by) VALUES
  ('aaa', 111, 'Colonel (O-6)', 'founder', 'me'),
  (NULL,  222, 'Major (O-4)',   'by discord', 'me'),
  ('ddd', NULL, 'Captain (O-3)', 'ucid form', 'me');
INSERT INTO jsh_rank_grants (ucid, discord_id, rank, granted_by) VALUES
  (NULL, 333, 'Airman (E-2)', 'me');   -- competing discord grant for ddd

\echo '--- CHECK 1: resolution (ccc must be absent, ddd must be Captain) ---'
SELECT DISTINCT ON (p.ucid) p.ucid, g.rank
FROM (SELECT unnest(ARRAY['aaa','bbb','ccc','ddd','eee']::TEXT[]) AS ucid) p
LEFT JOIN players pl ON pl.ucid = p.ucid
JOIN jsh_rank_grants g
  ON g.ucid = p.ucid
  OR (g.discord_id IS NOT NULL AND g.discord_id > 0 AND g.discord_id = pl.discord_id)
ORDER BY p.ucid, (g.ucid = p.ucid) DESC NULLS LAST;

\echo '--- CHECK 2: a grant row with neither identifier is rejected ---'
DO $$ BEGIN
  INSERT INTO jsh_rank_grants (rank, granted_by) VALUES ('X', 'me');
  RAISE EXCEPTION 'FAIL: subject check did not fire';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: subject check fired';
END $$;

\echo '--- CHECK 3: discord_id = -1 is rejected ---'
DO $$ BEGIN
  INSERT INTO jsh_rank_grants (discord_id, rank, granted_by) VALUES (-1, 'X', 'me');
  RAISE EXCEPTION 'FAIL: -1 accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: -1 rejected';
END $$;

\echo '--- CHECK 4: a second grant for the same ucid is rejected (upsert path) ---'
DO $$ BEGIN
  INSERT INTO jsh_rank_grants (ucid, rank, granted_by) VALUES ('aaa', 'X', 'me');
  RAISE EXCEPTION 'FAIL: duplicate ucid accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'ok: duplicate ucid rejected';
END $$;

\echo '--- CHECK 5: the grant delete+insert replaces BOTH forms ---'
BEGIN;
DELETE FROM jsh_rank_grants
 WHERE (ucid IS NOT NULL AND ucid = 'ddd') OR (discord_id IS NOT NULL AND discord_id = 333);
INSERT INTO jsh_rank_grants (ucid, discord_id, rank, granted_by) VALUES ('ddd', 333, 'Colonel (O-6)', 'me');
SELECT count(*) AS rows_for_ddd FROM jsh_rank_grants WHERE ucid='ddd' OR discord_id=333;
COMMIT;

\echo '--- CHECK 6: the /ranks grants listing query ---'
SELECT g.rank, g.reason, g.granted_by, g.ucid, g.discord_id, p.name
  FROM jsh_rank_grants g LEFT JOIN players p ON p.ucid = g.ucid
 ORDER BY g.granted_at DESC;

\echo '--- CHECK 7: revoke by discord id only ---'
DELETE FROM jsh_rank_grants
 WHERE (ucid IS NOT NULL AND ucid = NULL) OR (discord_id IS NOT NULL AND discord_id = 222);
SELECT count(*) AS remaining FROM jsh_rank_grants;
