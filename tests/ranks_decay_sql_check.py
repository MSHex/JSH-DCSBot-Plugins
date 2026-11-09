"""
jsh_ranks' decay query (decay.INPUTS), run against a real PostgreSQL.

The tables come from the bot's OWN DDL, read from a DCSServerBot checkout,
loaded into a throwaway database that is dropped afterwards:

    python3 -I tests/ranks_decay_sql_check.py <dsn> <DCSServerBot checkout>

The DSN must reach a server where CREATE DATABASE is allowed.
"""
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / 'plugins' / 'jsh_ranks'))
import decay  # noqa: E402

DSN = sys.argv[1] if len(sys.argv) > 1 else "host=/var/tmp/pgtest dbname=postgres user=postgres"
BOT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
DB = 'jsh_decay_check'
PSQL = "/usr/lib/postgresql/16/bin/psql"
DDL = ['sql/tables.sql', 'plugins/mission/db/tables.sql', 'plugins/userstats/db/tables.sql',
       'plugins/gamemaster/db/tables.sql', 'plugins/creditsystem/db/tables.sql']

checks: list[tuple[str, bool, str]] = []


def eq(label, got, want):
    checks.append((label, got == want, f"got {got!r}, want {want!r}"))


def run(script: str, dsn: str) -> str:
    r = subprocess.run([PSQL, dsn, "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
                       input=script, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def literal(v) -> str:
    if isinstance(v, list):
        return "ARRAY[" + ",".join(str(int(x)) for x in v) + "]::BIGINT[]"
    return repr(int(v))


def query(dsn, campaign_id, discord_ids) -> dict:
    """{ucid: (discord_id, credits, playtime)}"""
    q = re.sub(r'%\((\w+)\)s',
               lambda m: literal({'campaign_id': campaign_id, 'discord_ids': discord_ids}[m.group(1)]),
               decay.INPUTS)
    out = run(q + ";", dsn)
    rows = {}
    for line in filter(None, out.splitlines()):
        did, ucid, cr, pt = line.split('|')
        rows[ucid] = (int(did), int(cr), int(float(pt)))
    return rows


if BOT is None:
    sys.exit("usage: ranks_decay_sql_check.py <dsn> <DCSServerBot checkout>")

run(f"DROP DATABASE IF EXISTS {DB}; CREATE DATABASE {DB};", DSN)
db = re.sub(r'dbname=\S+', f'dbname={DB}', DSN)
try:
    for f in DDL:
        run((BOT / f).read_text(), db)

    run("""
    INSERT INTO servers (server_name) VALUES ('S1'), ('S2');
    INSERT INTO campaigns (id, name, start, stop) VALUES
        (1, 'running', (now() AT TIME ZONE 'utc') - interval '30 days', NULL),
        (2, 'over',    (now() AT TIME ZONE 'utc') - interval '90 days',
                       (now() AT TIME ZONE 'utc') - interval '60 days');
    INSERT INTO campaigns_servers VALUES (1, 'S1'), (2, 'S1');
    INSERT INTO players (ucid, discord_id, name) VALUES
        ('A', 10, 'Able'), ('A2', 10, 'Able alt'),
        ('B', -1, 'Unlinked'), ('C', 30, 'Charlie'), ('D', 40, 'Dog');
    INSERT INTO missions (id, server_name, mission_name, mission_theatre, mission_start)
        VALUES (1, 'S1', 'm', 'Caucasus', (now() AT TIME ZONE 'utc') - interval '40 days'),
               (2, 'S2', 'm', 'Caucasus', (now() AT TIME ZONE 'utc') - interval '10 days');
    INSERT INTO credits VALUES (1, 'A', 500), (2, 'A', 9000), (1, 'B', 999), (1, 'C', 50);
    INSERT INTO statistics (mission_id, player_ucid, slot, side, hop_on, hop_off) VALUES
        -- A: 2 h inside the campaign on S1
        (1, 'A', 'F/A-18C', 2, (now() AT TIME ZONE 'utc') - interval '5 days',
                               (now() AT TIME ZONE 'utc') - interval '5 days' + interval '2 hours'),
        -- A: 3 h BEFORE the campaign started -- not counted
        (1, 'A', 'F/A-18C', 2, (now() AT TIME ZONE 'utc') - interval '35 days',
                               (now() AT TIME ZONE 'utc') - interval '35 days' + interval '3 hours'),
        -- C: 4 h on S2, which is not in the campaign -- not counted
        (2, 'C', 'F-16C', 2, (now() AT TIME ZONE 'utc') - interval '5 days',
                             (now() AT TIME ZONE 'utc') - interval '5 days' + interval '4 hours'),
        -- C: 1 h on S1, and an OPEN session -- the open one adds nothing
        (1, 'C', 'F-16C', 2, (now() AT TIME ZONE 'utc') - interval '4 days',
                             (now() AT TIME ZONE 'utc') - interval '4 days' + interval '1 hour'),
        (1, 'C', 'F-16C', 2, (now() AT TIME ZONE 'utc') - interval '1 hour', NULL),
        -- D: a REVERSED session, as close_all_statistics leaves -- must not
        -- raise, and must not count against him; plus 1 h that is fine
        (1, 'D', 'A-10C', 2, (now() AT TIME ZONE 'utc') - interval '2 days',
                             (now() AT TIME ZONE 'utc') - interval '3 days'),
        (1, 'D', 'A-10C', 2, (now() AT TIME ZONE 'utc') - interval '1 day',
                             (now() AT TIME ZONE 'utc') - interval '1 day' + interval '1 hour');
    """, db)

    rows = query(db, 1, [10, 30, 40, -1])
    eq("every UCID linked to a member comes back, alts included",
       sorted(rows), ['A', 'A2', 'C', 'D'])
    eq("credits from THIS campaign only, hours inside its dates on its servers",
       rows['A'], (10, 500, 7200))
    eq("an alt with nothing gets zeros, not a missing row", rows['A2'], (10, 0, 0))
    eq("another server's hours and an open session add nothing", rows['C'], (30, 50, 3600))
    eq("a reversed session neither raises nor counts", rows['D'], (40, 0, 3600))
    eq("an unlinked pilot (discord_id -1) never matches, even if asked for",
       'B' in rows, False)
    eq("a campaign that has ENDED returns nothing -- no demotions outside a campaign",
       query(db, 2, [10, 30]), {})
    eq("no members asked about, no rows", query(db, 1, []), {})
    # The bot refuses overlapping campaigns, so move campaign 1 into the future.
    run("UPDATE campaigns SET start = (now() AT TIME ZONE 'utc') + interval '1 day' WHERE id = 1;", db)
    eq("a campaign not yet STARTED returns nothing", query(db, 1, [10]), {})
finally:
    run(f"DROP DATABASE IF EXISTS {DB};", DSN)

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    print(' ok   ' if ok else ' FAIL ', label, '' if ok else f'-- {detail}')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
