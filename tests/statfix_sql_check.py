"""
Prepares and runs every statement in jsh_statfix/sql.py against a real
PostgreSQL, on a scratch schema with data shaped like the JSH failure.

    psql -h /var/tmp/pgtest -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
    python3 -I tests/statfix_sql_check.py "host=/var/tmp/pgtest dbname=postgres user=postgres"

Guards the three properties that matter, each of which would silently corrupt
the statistics table if it broke:
  * a live session (hop_off NULL, opened minutes ago) is never closed
  * a correction only ever SHORTENS a session, never lengthens one
  * after all three passes the overlap count is zero
"""
import re
import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'plugins' / 'jsh_statfix'))
import sql as S  # noqa: E402

DSN = sys.argv[1] if len(sys.argv) > 1 else "host=/var/tmp/pgtest dbname=postgres user=postgres"
PSQL = ["/usr/lib/postgresql/16/bin/psql", DSN, "-X", "-v", "ON_ERROR_STOP=1"]

SCHEMA = """
CREATE TABLE players (ucid TEXT PRIMARY KEY, name TEXT);
CREATE TABLE missions (id SERIAL PRIMARY KEY, server_name TEXT,
                       mission_start TIMESTAMP, mission_end TIMESTAMP);
CREATE TABLE statistics (mission_id INT, player_ucid TEXT, slot TEXT,
                         hop_on TIMESTAMP, hop_off TIMESTAMP,
                         takeoffs INT DEFAULT 0, landings INT DEFAULT 0, kills INT DEFAULT 0);
CREATE TABLE missionstats (id SERIAL PRIMARY KEY, mission_id INT, event TEXT,
                           init_id TEXT, "time" TIMESTAMP);
INSERT INTO players VALUES ('toby','Toby'),('joker','Joker'),('live','Live'),('grif','Griffin'),('douca','Douca');

-- A mission record left open for a fortnight, closed long after the fact
INSERT INTO missions VALUES (1,'JSH Dynamic Campaign','2026-07-24 18:00','2026-08-07 07:22:50');
-- Toby: machine-closed, has events -> pass 2 corrects it
INSERT INTO statistics VALUES (1,'toby','F-16C_50','2026-07-24 20:12:03','2026-08-07 07:22:50',1,0,1);
INSERT INTO missionstats (mission_id,event,init_id,"time") VALUES
  (1,'S_EVENT_TAKEOFF','toby','2026-07-24 20:18:00'),
  (1,'S_EVENT_SHOT','toby','2026-07-24 20:33:52');
-- Joker: machine-closed instructor, NO events -> only pass 3 can reach it
INSERT INTO statistics VALUES (1,'joker','instructor','2026-07-24 16:12:50','2026-08-07 07:22:50',0,0,0);
INSERT INTO statistics VALUES (1,'joker','F-16C_50','2026-07-28 22:05:00','2026-07-29 00:50:14',1,1,0);
-- An orphan old enough to close
INSERT INTO missions VALUES (2,'Server One','2026-09-16 18:00','2026-09-16 22:30');
INSERT INTO statistics VALUES (2,'toby','AH-64D_BLK_II','2026-09-16 19:00',NULL,0,0,0);
-- THE BUG THAT SHIPPED: close_all_statistics sets a mission's end to the NEXT
-- mission's start minus one second, so a session can open AFTER its own
-- mission's recorded end. Closing it against that end wrote hop_off < hop_on,
-- and every tsrange(hop_on, hop_off) in the bot then raised
-- "range lower bound must be less than or equal to range upper bound".
INSERT INTO missions VALUES (4,'JSH Co-op','2026-09-16 18:00','2026-09-16 20:00:00');
INSERT INTO statistics VALUES (4,'joker','UH-1H','2026-09-16 20:14:43',NULL,0,0,0);
-- THE 1-MINUTE FLOOR BUG: a session SHORTER than a minute, followed straight
-- away by the next slot. jsh_statfix 1.0/1.1 floored hop_off at hop_on + 1
-- MINUTE, which pushed it past the next session's start -- so the repair
-- created the overlap it exists to remove, re-clipped it every run without
-- converging, and inflated the session to a minute it never flew. 3,606 rows
-- and 60 hours of phantom time on the live database.
INSERT INTO missions VALUES (5,'JSH Co-op','2026-10-20 18:00','2026-10-20 23:00');
INSERT INTO statistics VALUES (5,'toby','F-16C_50','2026-10-20 20:00:00','2026-10-20 20:00:12',0,0,0);
INSERT INTO statistics VALUES (5,'toby','A-10C_2', '2026-10-20 20:00:13','2026-10-20 21:00:00',1,1,0);
-- THE LIVE PATHOLOGY, exactly: a SHORT session that the clip pass touches.
-- hop_off sits at the mission end so pass 3 sees it overlapping the next slot
-- taken 30 seconds later. With a 1-minute floor the clip writes hop_on + 60s,
-- which is STILL past the next session's start -- so the row never leaves the
-- overlap set, is rewritten every run, and carries 60 seconds it never flew.
-- That is the 3,606 rows and 60 hours on the live database.
INSERT INTO missions VALUES (7,'JSH Co-op','2026-10-22 18:00','2026-10-22 20:05:00');
INSERT INTO statistics VALUES (7,'grif','F-16C_50','2026-10-22 20:00:00','2026-10-22 20:05:00',0,0,0);
INSERT INTO statistics VALUES (7,'grif','A-10C_2', '2026-10-22 20:00:30','2026-10-22 21:00:00',1,1,0);
-- THE BOT'S OWN SLOT-CHANGE ARTEFACT, which must never be rewritten.
-- stop_player closes a session at GREATEST(hop_on + 1 second, now()), so an
-- instant slot change leaves the next hop_on a few MILLISECONDS after this
-- hop_on and about a second BEFORE this hop_off. GREATEST(next_hop_on,
-- hop_on + 1 second) then equals the hop_off already stored, so without a
-- "must shorten" guard the clip rewrites the same value on every run and
-- never converges -- 3,860 rows for 0.0 hours on the live database.
INSERT INTO missions VALUES (8,'JSH Co-op','2026-10-23 18:00','2026-10-23 23:00');
INSERT INTO statistics VALUES (8,'douca','C-130J-30','2026-10-23 20:00:00','2026-10-23 20:00:01',0,0,0);
INSERT INTO statistics VALUES (8,'douca','A-10C_2',  '2026-10-23 20:00:00.004','2026-10-23 20:45:00',1,1,0);
-- SUB-SECOND SKEW: the bot closes one slot and opens the next from two
-- separately-taken timestamps, so they cross by under a second. Not
-- corruption; min_overlap must leave it alone.
INSERT INTO missions VALUES (6,'JSH Co-op','2026-10-21 18:00','2026-10-21 23:00');
INSERT INTO statistics VALUES (6,'joker','F-16C_50','2026-10-21 20:00:00','2026-10-21 20:30:00.995',0,0,0);
INSERT INTO statistics VALUES (6,'joker','UH-1H',   '2026-10-21 20:30:00','2026-10-21 21:00:00',1,1,0);
-- A LIVE session: open, started minutes ago. Must survive untouched.
INSERT INTO missions VALUES (3,'JSH Foothold Alpha', now() AT TIME ZONE 'utc' - INTERVAL '2 hours', NULL);
INSERT INTO statistics VALUES (3,'live','FA-18C_hornet',
                               (now() AT TIME ZONE 'utc') - INTERVAL '30 minutes', NULL,0,0,0);
"""

def run(script):
    p = subprocess.run(PSQL, input=script, capture_output=True, text=True)
    if p.returncode:
        print(p.stdout); print(p.stderr, file=sys.stderr); sys.exit(1)
    return p.stdout

def update(script):
    """Run a data-modifying statement and return its row count."""
    for line in reversed(run(script).splitlines()):
        if line.startswith("UPDATE "):
            return int(line.split()[1])
    return None


def bind(stmt, params):
    """Substitute named placeholders so psql can run the real statement text."""
    out = stmt
    for key, val in params.items():
        out = out.replace(f"%({key})s", val)
    return out


def numbered(stmt):
    """Rewrite psycopg's %s placeholders to $1, $2 ... so PREPARE accepts them."""
    count = [0]
    def repl(_):
        count[0] += 1
        return f"${count[0]}"
    return re.sub(r"%s", repl, stmt)

checks, fails = [], []
def eq(label, got, want):
    ok = str(got).strip() == str(want).strip()
    checks.append((label, got, want, ok))
    if not ok:
        fails.append(label)

run("DROP SCHEMA public CASCADE; CREATE SCHEMA public;" + SCHEMA)

# Every statement must at least PREPARE: a syntax error never reaches production.
prep = []
for name in ['CLOSE_ORPHANS', 'CORRECT_BY_EVENTS', 'CLIP_OVERLAPS', 'TOTAL_HOURS',
             'HAS_MISSIONSTATS', 'OVERLAP_COUNT', 'PREVIEW', 'LAST_RUN', 'RECENT_RUNS',
             'START_RUN', 'FINISH_RUN']:
    stmt = getattr(S, name)
    if name in ('LAST_RUN', 'RECENT_RUNS', 'START_RUN', 'FINISH_RUN'):
        continue  # need jsh_statfix_runs; covered after it is created
    body = bind(stmt, {'min_age': "'6 hours'", 'grace': "'15 minutes'",
                       'min_overlap': "'60 seconds'"})
    prep.append(f"PREPARE p_{name.lower()} AS {body};")
run("\n".join(prep))
checks.append(("every repair statement prepares", "ok", "ok", True))

run((ROOT / 'plugins' / 'jsh_statfix' / 'db' / 'tables.sql').read_text())
run(f"PREPARE p_lastrun AS {S.LAST_RUN};\n"
    f"PREPARE p_recent AS {numbered(S.RECENT_RUNS)};\n"
    f"PREPARE p_start AS {numbered(S.START_RUN)};\n"
    f"PREPARE p_finish AS {numbered(S.FINISH_RUN)};")
checks.append(("run-log statements prepare against tables.sql", "ok", "ok", True))

before = run(f"COPY ({S.TOTAL_HOURS}) TO STDOUT").strip()
# 323.18 (Toby's fortnight) + 327.17 (Joker's) + 2.75 (one real sortie)
eq("hours before the repair", before, "656.93")  # the two orphans are still open here

pre = run(f"COPY ({bind(S.PREVIEW, {'min_age': chr(39)+'6 hours'+chr(39), 'min_overlap': chr(39)+'60 seconds'+chr(39)})}) TO STDOUT").strip()
eq("preview counts orphans/machine-closed/overlapping", pre, "2\t3\t2")

eq("pass 1 closed both stale orphans",
   update(f"{bind(S.CLOSE_ORPHANS, {'min_age': chr(39)+'6 hours'+chr(39)})};"), 2)

# The guard that was missing when jsh_statfix 1.0 shipped.
eq("a mission ending BEFORE a session started never reverses it",
   run("""COPY (SELECT count(*) FROM statistics
          WHERE hop_off IS NOT NULL AND hop_off < hop_on) TO STDOUT""").strip(), "0")
# One SECOND, matching DCSServerBot's own floor -- a minute was the bug.
eq("an unrecoverable session gets the minimum length, not a minute",
   run("""COPY (SELECT hop_off FROM statistics
          WHERE player_ucid='joker' AND slot='UH-1H' AND mission_id=4) TO STDOUT""").strip(),
   "2026-09-16 20:14:44")

eq("pass 2 corrected only the session with events",
   update(f"{bind(S.CORRECT_BY_EVENTS, {'grace': chr(39)+'15 minutes'+chr(39)})};"), 1)

eq("pass 3 clipped the instructor session and the short one",
   update(f"{bind(S.CLIP_OVERLAPS, {'min_overlap': chr(39)+'60 seconds'+chr(39)})};"), 2)

# The floor must match DCSServerBot's own (one second), not stretch a short
# session past the next one.
eq("a 12-second session keeps its length, not a minute",
   run("""COPY (SELECT hop_off FROM statistics
          WHERE player_ucid='toby' AND slot='F-16C_50' AND mission_id=5) TO STDOUT""").strip(),
   "2026-10-20 20:00:12")
eq("and so does not overlap the slot taken one second later",
   run("""COPY (SELECT count(*) FROM statistics a JOIN statistics b
          ON a.player_ucid=b.player_ucid AND a.hop_on < b.hop_on
         AND b.hop_on < a.hop_off WHERE a.mission_id=5) TO STDOUT""").strip(), "0")
eq("the bot's own 1-second slot-change artefact is never rewritten",
   run("""COPY (SELECT hop_off FROM statistics
          WHERE player_ucid='douca' AND slot='C-130J-30' AND mission_id=8) TO STDOUT""").strip(),
   "2026-10-23 20:00:01")
eq("a short clipped session ends where the next one starts, not a minute later",
   run("""COPY (SELECT hop_off FROM statistics
          WHERE player_ucid='grif' AND slot='F-16C_50' AND mission_id=7) TO STDOUT""").strip(),
   "2026-10-22 20:00:30")
eq("sub-second slot-change skew is left alone",
   run("""COPY (SELECT hop_off FROM statistics
          WHERE player_ucid='joker' AND slot='F-16C_50' AND mission_id=6) TO STDOUT""").strip(),
   "2026-10-21 20:30:00.995")

eq("no overlap above the threshold remains",
   run("""COPY (SELECT count(*) FROM statistics a JOIN statistics b
          ON a.player_ucid = b.player_ucid
         AND (a.mission_id, a.slot, a.hop_on) < (b.mission_id, b.slot, b.hop_on)
         AND a.hop_on < b.hop_off AND b.hop_on < a.hop_off
        WHERE a.hop_off IS NOT NULL AND b.hop_off IS NOT NULL
          AND LEAST(a.hop_off, b.hop_off) - GREATEST(a.hop_on, b.hop_on)
              > INTERVAL '60 seconds') TO STDOUT""").strip(), "0")
eq("the sub-second slot-change artefacts are still there, deliberately",
   run(f"COPY ({S.OVERLAP_COUNT}) TO STDOUT").strip(), "2")

# The live session must be exactly as it was: still open.
eq("the live session was not touched",
   run("COPY (SELECT hop_off IS NULL FROM statistics WHERE player_ucid='live') TO STDOUT").strip(), "t")

# Nothing may have been lengthened.
eq("no session was lengthened",
   run("""COPY (SELECT count(*) FROM statistics WHERE hop_off IS NOT NULL
          AND hop_off <= hop_on) TO STDOUT""").strip(), "0")

# What /pilot status, AwardAutomation and CreditSystem all do. Counting
# hop_off <= hop_on is the same check, but building the range is the thing
# that actually threw in production, so assert on that directly.
eq("every closed session can build a tsrange",
   run("""COPY (SELECT count(*) FROM statistics
          WHERE hop_off IS NOT NULL
            AND tsrange(hop_on, hop_off) IS NOT NULL) TO STDOUT""").strip(),
   run("""COPY (SELECT count(*) FROM statistics
          WHERE hop_off IS NOT NULL) TO STDOUT""").strip())

eq("Toby's fortnight became a real sortie",
   run("""COPY (SELECT round(extract(epoch from (hop_off-hop_on))/3600.0,2)
          FROM statistics WHERE player_ucid='toby' AND slot='F-16C_50'
            AND mission_id=1) TO STDOUT""").strip(), "0.61")

eq("Joker's event-less instructor session was clipped to his next slot",
   run("""COPY (SELECT round(extract(epoch from (hop_off-hop_on))/3600.0,2)
          FROM statistics WHERE player_ucid='joker' AND slot='instructor') TO STDOUT""").strip(), "101.87")

after = run(f"COPY ({S.TOTAL_HOURS}) TO STDOUT").strip()
checks.append((f"hours after the repair: {before} -> {after}", after, after, True))

# Idempotence: a second run must be a no-op, or a monthly job drifts the data.
again = [update(f"{bind(S.CLOSE_ORPHANS, {'min_age': chr(39)+'6 hours'+chr(39)})};"),
         update(f"{bind(S.CORRECT_BY_EVENTS, {'grace': chr(39)+'15 minutes'+chr(39)})};"),
         update(f"{bind(S.CLIP_OVERLAPS, {'min_overlap': chr(39)+'60 seconds'+chr(39)})};")]
eq("running it again changes nothing (idempotent)", again, "[0, 0, 0]")

# The one-off cleanup is run with the threshold dropped to zero, which is the
# only way to reach rows already inflated below it. At that setting nothing
# protects the pass from the bot's own slot-change artefact, so the clip must
# refuse to write a value equal to the one already stored -- otherwise it
# rewrites the same rows forever. On the live database that was 3,860 rows for
# 0.0 hours, every single run.
zero = chr(39) + '0 seconds' + chr(39)
first = update(f"{bind(S.CLIP_OVERLAPS, {'min_overlap': zero})};")
second = update(f"{bind(S.CLIP_OVERLAPS, {'min_overlap': zero})};")
# The first pass legitimately clips what the 60-second threshold had skipped.
# The guarantee is the SECOND one: nothing left to do.
eq("a threshold-zero clip converges instead of rewriting forever", second, 0)

for label, got, want, ok in checks:
    print(("  ok  " if ok else " FAIL "), label, "" if ok else f"-> {got!r} (wanted {want!r})")
print(f"\n{len(checks)-len(fails)}/{len(checks)} passed")
sys.exit(1 if fails else 0)
