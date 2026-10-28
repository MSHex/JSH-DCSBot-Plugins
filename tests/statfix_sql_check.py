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
INSERT INTO players VALUES ('toby','Toby'),('joker','Joker'),('live','Live');

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
    body = bind(stmt, {'min_age': "'6 hours'", 'grace': "'15 minutes'"})
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
eq("hours before the repair", before, "653.10")

pre = run(f"COPY ({bind(S.PREVIEW, {'min_age': chr(39)+'6 hours'+chr(39)})}) TO STDOUT").strip()
eq("preview counts orphans/machine-closed/overlapping", pre, "1\t2\t1")

eq("pass 1 closed exactly the one stale orphan",
   update(f"{bind(S.CLOSE_ORPHANS, {'min_age': chr(39)+'6 hours'+chr(39)})};"), 1)

eq("pass 2 corrected only the session with events",
   update(f"{bind(S.CORRECT_BY_EVENTS, {'grace': chr(39)+'15 minutes'+chr(39)})};"), 1)

eq("pass 3 clipped the event-less instructor session",
   update(f"{S.CLIP_OVERLAPS};"), 1)

eq("no overlaps remain", run(f"COPY ({S.OVERLAP_COUNT}) TO STDOUT").strip(), "0")

# The live session must be exactly as it was: still open.
eq("the live session was not touched",
   run("COPY (SELECT hop_off IS NULL FROM statistics WHERE player_ucid='live') TO STDOUT").strip(), "t")

# Nothing may have been lengthened.
eq("no session was lengthened",
   run("""COPY (SELECT count(*) FROM statistics WHERE hop_off IS NOT NULL
          AND hop_off <= hop_on) TO STDOUT""").strip(), "0")

eq("Toby's fortnight became a real sortie",
   run("""COPY (SELECT round(extract(epoch from (hop_off-hop_on))/3600.0,2)
          FROM statistics WHERE player_ucid='toby' AND slot='F-16C_50') TO STDOUT""").strip(), "0.61")

eq("Joker's event-less instructor session was clipped to his next slot",
   run("""COPY (SELECT round(extract(epoch from (hop_off-hop_on))/3600.0,2)
          FROM statistics WHERE player_ucid='joker' AND slot='instructor') TO STDOUT""").strip(), "101.87")

after = run(f"COPY ({S.TOTAL_HOURS}) TO STDOUT").strip()
checks.append((f"hours after the repair: {before} -> {after}", after, after, True))

# Idempotence: a second run must be a no-op, or a monthly job drifts the data.
again = [update(f"{bind(S.CLOSE_ORPHANS, {'min_age': chr(39)+'6 hours'+chr(39)})};"),
         update(f"{bind(S.CORRECT_BY_EVENTS, {'grace': chr(39)+'15 minutes'+chr(39)})};"),
         update(f"{S.CLIP_OVERLAPS};")]
eq("running it again changes nothing (idempotent)", again, "[0, 0, 0]")

for label, got, want, ok in checks:
    print(("  ok  " if ok else " FAIL "), label, "" if ok else f"-> {got!r} (wanted {want!r})")
print(f"\n{len(checks)-len(fails)}/{len(checks)} passed")
sys.exit(1 if fails else 0)
