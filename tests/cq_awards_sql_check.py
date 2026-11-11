"""
jsh_cq's and jsh_awards' SQL, run against a real PostgreSQL.

The tables come from the bot's OWN DDL (and jsh_csar's and jsh_awards'),
read from a DCSServerBot checkout and loaded into a throwaway database that
is dropped afterwards:

    python3 -I tests/cq_awards_sql_check.py <dsn> <DCSServerBot checkout>

Parameters are substituted as SQL literals, so every %(name)s placeholder and
cast in the queries is exercised as the bot will run it.
"""
import pathlib
import re
import subprocess
import sys
import importlib.util
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(plugin, module):
    spec = importlib.util.spec_from_file_location(f"{plugin}_{module}",
                                                  ROOT / 'plugins' / plugin / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cq = load('jsh_cq', 'sql')
aw = load('jsh_awards', 'sql')

DSN = sys.argv[1] if len(sys.argv) > 1 else "host=/var/tmp/pgtest dbname=postgres user=postgres"
BOT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
DB = 'jsh_cq_awards_check'
PSQL = "/usr/lib/postgresql/16/bin/psql"
DDL = ['sql/tables.sql', 'plugins/mission/db/tables.sql', 'plugins/userstats/db/tables.sql',
       'plugins/gamemaster/db/tables.sql', 'plugins/creditsystem/db/tables.sql',
       'plugins/missionstats/db/tables.sql', 'plugins/greenieboard/db/tables.sql',
       'plugins/logbook/db/tables.sql']
OURS = [ROOT / 'plugins/jsh_csar/db/tables.sql', ROOT / 'plugins/jsh_awards/db/tables.sql']

results: list[tuple[str, bool, str]] = []


def eq(label, got, want):
    results.append((label, got == want, f"got {got!r}, want {want!r}"))


def run(script: str, dsn: str) -> list[list[str]]:
    r = subprocess.run([PSQL, dsn, "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
                       input=script, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return [line.split('|') for line in r.stdout.strip().splitlines() if line]


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat(sep=' ')}'::timestamp"
    if isinstance(v, list):
        return "ARRAY[" + ",".join(lit(x) for x in v) + "]::text[]"
    return "'" + str(v).replace("'", "''") + "'"


def q(dsn, query, **params):
    text = re.sub(r'%\((\w+)\)s', lambda m: lit(params[m.group(1)]), query)
    return run(text + ";", dsn)


if BOT is None:
    sys.exit("usage: cq_awards_sql_check.py <dsn> <DCSServerBot checkout>")

run(f"DROP DATABASE IF EXISTS {DB}; CREATE DATABASE {DB};", DSN)
db = re.sub(r'dbname=\S+', f'dbname={DB}', DSN)
try:
    for f in DDL:
        run((BOT / f).read_text(), db)
    for f in OURS:
        run(f.read_text(), db)

    NOW = "(now() AT TIME ZONE 'utc')"
    run(f"""
    INSERT INTO servers (server_name) VALUES ('S1');
    INSERT INTO campaigns (id, name, start) VALUES (1, 'c', {NOW} - interval '400 days');
    INSERT INTO players (ucid, discord_id, name) VALUES
        ('A', 10, 'Able'), ('B', -1, 'Baker'), ('C', 30, 'Charlie');
    INSERT INTO missions (id, server_name, mission_name, mission_theatre, mission_start)
        VALUES (1, 'S1', 'm', 'Caucasus', '2026-09-01');
    -- traps: A has 4 good day traps, 1 bolter (no wire), 1 old one, 1 on another deck
    INSERT INTO traps (mission_id, player_ucid, unit_type, grade, comment, place, trapcase, wire, night, points, time) VALUES
        (1,'A','FA-18C_hornet','OK','', 'CVN-72',1,3,FALSE,4, {NOW} - interval '1 day'),
        (1,'A','FA-18C_hornet','(OK)','','CVN-72',1,2,FALSE,3, {NOW} - interval '2 days'),
        (1,'A','FA-18C_hornet','OK','', 'CVN-72',1,3,FALSE,4, {NOW} - interval '3 days'),
        (1,'A','FA-18C_hornet','_OK_','','CVN-72',1,3,TRUE,5, {NOW} - interval '4 days'),
        (1,'A','FA-18C_hornet','---','','CVN-72',1,1,FALSE,2, {NOW} - interval '5 days'),
        (1,'A','FA-18C_hornet','B','',  'CVN-72',1,NULL,FALSE,3, {NOW} - interval '6 days'),
        (1,'A','FA-18C_hornet','OK','', 'CVN-72',1,3,FALSE,4, {NOW} - interval '100 days'),
        (1,'A','FA-18C_hornet','OK','', 'CVN-73',1,3,FALSE,4, {NOW} - interval '7 days');
    """, db)

    def traps(**kw):
        p = dict(min_points=3.0, valid_days=90, places=[], ucid=None)
        p.update(kw)
        return sorted((r[0], r[1]) for r in q(db, cq.TRAPS, **p))

    got = traps()
    eq("CQ traps: 3+ points, wire caught, inside 90 days, any deck",
       [g for g in got], [('A', 'f'), ('A', 'f'), ('A', 'f'), ('A', 'f'), ('A', 't')])
    eq("...a bolter with points but no wire is excluded", len(got), 5)
    eq("places narrows to listed decks", len(traps(places=['CVN-72'])), 4)
    eq("one pilot only", traps(ucid='B'), [])

    # ---- qualification grant flow
    q(db, cq.ENSURE_QUALIFICATION, name='CQ_CV_DAY', description='d', valid_days=90)
    q(db, cq.ENSURE_QUALIFICATION, name='CQ_CV_DAY', description='again', valid_days=90)
    rows = q(db, cq.QUALIFICATION, name='CQ_CV_DAY')
    eq("ENSURE creates once, leaves an existing row alone",
       run("SELECT count(*), max(description) FROM logbook_qualifications;", db), [['1', 'd']])
    qid = int(rows[0][0])
    eq("no row held yet", q(db, cq.HELD, ucid='A', qid=qid), [])
    q(db, cq.GRANT, ucid='A', qid=qid, expires=datetime(2027, 1, 1))
    q(db, cq.GRANT, ucid='A', qid=qid, expires=datetime(2027, 2, 1))
    eq("GRANT then re-GRANT: one row, expiry moved",
       run(f"SELECT count(*), max(expires_at) FROM logbook_pilot_qualifications;", db),
       [['1', '2027-02-01 00:00:00']])
    run("UPDATE logbook_pilot_qualifications SET expires_at = '2020-01-01', granted_at = '2019-01-01';", db)
    q(db, cq.GRANT, ucid='A', qid=qid, expires=datetime(2027, 3, 1))
    eq("re-granting a LAPSED row resets granted_at",
       run("SELECT granted_at > '2020-01-01' FROM logbook_pilot_qualifications;", db), [['t']])
    eq("HELD sees the row", q(db, cq.HELD, ucid='A', qid=qid), [['2027-03-01 00:00:00']])

    run("INSERT INTO logbook_awards (name) VALUES ('CQ Ribbon'), ('Iron Butt');", db)
    aid = int(q(db, cq.AWARD_ID, name='CQ Ribbon')[0][0])
    eq("no ribbon yet", q(db, cq.HAS_AWARD, ucid='A', aid=aid), [])
    q(db, cq.GIVE_AWARD, ucid='A', aid=aid, citation='x')
    eq("ribbon given", q(db, cq.HAS_AWARD, ucid='A', aid=aid), [['1']])
    eq("TABLES sees greenieboard and logbook", run(cq.TABLES + ";", db), [['t', 't']])

    # ---- awards: October 2026
    run(f"""
    INSERT INTO credits_log (campaign_id, event, player_ucid, old_points, new_points, time) VALUES
        (1, 'points',   'A', 0,   500, '2026-10-05'),
        (1, 'landing',  'A', 500, 600, '2026-10-06'),
        (1, 'decay',    'A', 600, 100, '2026-10-07'),   -- a LOSS: must not subtract
        (1, 'Donation', 'C', 0,  9000, '2026-10-08'),   -- a transfer: excluded, any case
        (1, 'points',   'C', 0,   300, '2026-10-09'),
        (1, 'points',   'C', 300, 999, '2026-11-01');   -- next month
    INSERT INTO jsh_csar_rescues (server_name, player_ucid, player_name, pilot_status, rescues, rescued_at) VALUES
        ('S1', 'C', 'Charlie', 'alive', 2, '2026-10-10'),
        ('S1', 'A', 'Able', 'alive', 1, '2026-10-11'),
        ('S1', 'A', 'Able', 'alive', 5, '2026-09-30');  -- previous month
    INSERT INTO statistics (mission_id, player_ucid, slot, side, hop_on, hop_off,
                            kills_planes, kills_helicopters, deaths_planes, deaths_helicopters,
                            kills_ground, kills_sams, kills_ships, deaths_ground, deaths_sams, deaths_ships) VALUES
        -- A: 2 h inside October, plus a sortie across midnight on Nov 1 (1 h in Oct)
        (1,'A','F-16C',2,'2026-10-10 10:00','2026-10-10 12:00', 6,0,1,0, 0,0,0, 0,0,0),
        (1,'A','F-16C',2,'2026-10-31 23:00','2026-11-01 01:00', 0,0,0,0, 0,0,0, 0,0,0),
        -- C: a REVERSED session and an OPEN one -- neither may count
        (1,'C','A-10C',2,'2026-10-12 10:00','2026-10-12 09:00', 0,0,0,0, 9,1,0, 1,0,0),
        (1,'C','A-10C',2,'2026-10-13 10:00', NULL,              0,0,0,0, 0,0,0, 0,0,0),
        (1,'C','A-10C',2,'2026-10-14 10:00','2026-10-14 12:00', 0,0,0,0, 0,0,0, 0,0,0),
        -- B: a sortie wholly in September
        (1,'B','F-16C',2,'2026-09-10 10:00','2026-09-10 15:00', 9,0,0,0, 0,0,0, 0,0,0);
    """, db)
    M = dict(start=datetime(2026, 10, 1), end=datetime(2026, 11, 1), top=3,
             exclude=['donation'], campaign_id=None)
    eq("credits EARNED: gains only, transfers out, next month out",
       q(db, aw.CREDITS, **M), [['A', 'Able', '10', '600'], ['C', 'Charlie', '30', '300']])
    eq("credits: campaign filter", q(db, aw.CREDITS, **dict(M, campaign_id=2)), [])
    eq("CSAR rescues in the month", q(db, aw.CSAR, **M),
       [['C', 'Charlie', '30', '2'], ['A', 'Able', '10', '1']])
    eq("hours clipped to the month; reversed, open and other months out",
       q(db, aw.HOURS, **M), [['A', 'Able', '10', '3.0'], ['C', 'Charlie', '30', '2.0']])
    eq("A/A kills and deaths", q(db, aw.KD_AA, **M), [['A', 'Able', '10', '6', '1']])
    eq("A/G: the reversed session's kills still count -- kills are real, only its time is not",
       q(db, aw.KD_AG, **M), [['C', 'Charlie', '30', '10', '1']])

    run("INSERT INTO jsh_awards_boards (month) VALUES ('2026-09-01');", db)
    eq("POSTED finds a posted month", q(db, aw.POSTED, month='2026-09-01'), [['1']])
    eq("...and not another", q(db, aw.POSTED, month='2026-10-01'), [])
    q(db, aw.MARK_POSTED, month='2026-10-01', message_id=123, note=None)
    q(db, aw.MARK_POSTED, month='2026-10-01', message_id=456, note=None)
    eq("MARK_POSTED twice: one row, latest message",
       run("SELECT count(*), max(message_id) FROM jsh_awards_boards WHERE month = '2026-10-01';", db),
       [['1', '456']])
    aid = int(q(db, aw.AWARD_ID, name='Iron Butt')[0][0])
    for _ in range(2):
        q(db, aw.GIVE_AWARD, ucid='A', aid=aid, citation='Most flight hours, October 2026: 3.0 h.')
    q(db, aw.GIVE_AWARD, ucid='A', aid=aid, citation='Most flight hours, November 2026: 9.0 h.')
    eq("the same month's award is given once; another month's again",
       run(f"SELECT count(*) FROM logbook_pilot_awards WHERE award_id = {aid};", db), [['2']])
finally:
    run(f"DROP DATABASE IF EXISTS {DB};", DSN)

failed = [r for r in results if not r[1]]
for label, ok, detail in results:
    print(' ok   ' if ok else ' FAIL ', label, '' if ok else f'-- {detail}')
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
