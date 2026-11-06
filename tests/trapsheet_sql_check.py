"""
jsh_trapsheet's SQL, run against a real PostgreSQL.

The table is built from greenieboard's OWN tables.sql (fetched from a
DCSServerBot checkout), so the test is against the schema the bot actually
creates, not one written to suit the query.

The case that matters most is the first one. greenieboard writes its own
row for every Super Carrier grade. An earlier version of this plugin
INSERTed a second, so every graded pass counted twice on the board at two
different scores. FIND then SET must leave exactly one row.

    python3 -I tests/trapsheet_sql_check.py <dsn> <path to greenieboard tables.sql>
"""
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / 'plugins' / 'jsh_trapsheet'))
import sql  # noqa: E402

DSN = sys.argv[1] if len(sys.argv) > 1 else "host=/var/tmp/pgtest dbname=postgres user=postgres"
TABLES = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
PSQL = ["/usr/lib/postgresql/16/bin/psql", DSN, "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"]

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    checks.append((label, ok, detail))


def run(script: str) -> str:
    r = subprocess.run(PSQL, input=script, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def literal(v) -> str:
    if v is None:
        return 'NULL'
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def bind(query: str, params: dict) -> str:
    """psycopg %(name)s placeholders -> SQL literals, for psql."""
    return re.sub(r'%\((\w+)\)s', lambda m: literal(params[m.group(1)]), query)


def find(ucid='U1', mission=1, grade='OK', prefix='') -> str:
    """FIND, as store() runs it: 'id|points|trapcase|night', or ''."""
    return run(prefix + bind(sql.FIND, {'ucid': ucid, 'mission_id': mission,
                                        'grade': grade, 'window': sql.WINDOW}))


def attach(ucid='U1', mission=1, grade='OK', image='\\x89504e47') -> str:
    """FIND then SET, exactly the order store() uses. The id SET wrote, or ''."""
    found = find(ucid, mission, grade)
    if not found:
        return ''
    return run(bind(sql.SET, {'id': int(found.split('|')[0]), 'image': image,
                              'trapdata': '{"points": []}', 'tgroove': 17.9}))


def greenieboard_row(ucid='U1', mission=1, grade='OK', ago='0 seconds',
                     with_picture=False) -> str:
    """A row as greenieboard's process_lso_event writes it."""
    pic = "'\\x00'::bytea" if with_picture else 'NULL'
    return run(f"""
        INSERT INTO traps (mission_id, player_ucid, unit_type, grade, comment,
                           place, trapcase, wire, night, points, trapsheet, time)
        VALUES ({mission}, '{ucid}', 'FA-18C_hornet', '{grade}', 'gb', 'CVN-72',
                1, 3, false, 4, {pic},
                (now() AT TIME ZONE 'utc') - interval '{ago}')
        RETURNING id""")


def count(where: str = 'true') -> int:
    return int(run(f"SELECT count(*) FROM traps WHERE {where}"))


def reset() -> None:
    run("TRUNCATE traps RESTART IDENTITY")


# ------------------------------------------------------------------- schema
schema = TABLES.read_text() if TABLES else ''
# The bot's tables.sql also back-fills traps from missionstats; only the
# CREATE statements are wanted here.
creates = ';\n'.join(s for s in schema.split(';')
                     if re.search(r'CREATE (TABLE|INDEX)', s, re.I)) + ';'
run("""
    DROP TABLE IF EXISTS traps CASCADE;
    DROP TABLE IF EXISTS players CASCADE;
    CREATE TABLE players (ucid TEXT PRIMARY KEY);
    INSERT INTO players VALUES ('U1'), ('U2');
""")
run(creates)
check("built traps from greenieboard's own tables.sql",
      run("SELECT to_regclass('public.traps') IS NOT NULL") == 't')

# ------------------------------------------------------------- the double row
reset()
gb = greenieboard_row()
got = attach()
check("FIND then SET lands on greenieboard's row", got == gb, f"got {got!r}, want {gb!r}")
check("and leaves exactly ONE row for the pass", count() == 1,
      f"{count()} rows -- the pass would count twice on the greenieboard")
check("the picture is on greenieboard's row",
      run(f"SELECT trapsheet IS NOT NULL FROM traps WHERE id = {gb}") == 't')
check("greenieboard's own points are left alone",
      run(f"SELECT points FROM traps WHERE id = {gb}") == '4')

# FIND hands back greenieboard's own verdict for the picture and embed.
reset()
gb = greenieboard_row()
verdict = find()
check("FIND returns greenieboard's points, case and night for this pass",
      verdict == f"{gb}|4|1|f", f"got {verdict!r}")
run(f"UPDATE traps SET trapsheet = '\\x00' WHERE id = {gb}")
check("SET never gives a row a second picture",
      run(bind(sql.SET, {'id': int(gb), 'image': '\\x01', 'trapdata': '{}',
                         'tgroove': None})) == '')

# ------------------------------------------------------------------ no row
reset()
check("no greenieboard row -> FIND finds nothing, so the caller inserts",
      attach() == '')

# --------------------------------------------------- the wrong row is safe
# An older picture-less row for the same pilot -- a pass whose samples were
# lost -- must not take the picture meant for this pass.
reset()
stale = greenieboard_row(grade='OK', ago='5 minutes')
check("a stale untagged row outside the window is not taken", attach() == '',
      "the picture would land on a pass flown minutes ago")
check("and is still untagged",
      run(f"SELECT trapsheet IS NULL FROM traps WHERE id = {stale}") == 't')

reset()
other = greenieboard_row(grade='B', ago='5 seconds')
check("a recent row with a DIFFERENT grade is not taken", attach(grade='OK') == '')

reset()
greenieboard_row(ucid='U2')
check("another pilot's row is not taken", attach(ucid='U1') == '')

reset()
greenieboard_row(mission=2)
check("a row from another mission is not taken", attach(mission=1) == '')

reset()
greenieboard_row(with_picture=True)
check("a row that already has a picture is not overwritten", attach() == '')

reset()
older = greenieboard_row(grade='OK', ago='20 seconds')
newer = greenieboard_row(grade='OK', ago='1 second')
got = attach()
check("with two candidates, the most recent wins", got == newer,
      f"got {got!r}, want {newer!r}")

# --------------------------------------------------- the time zone trap
# `time` is UTC with no zone. A session in UTC+3 comparing against a bare
# now() would push the window three hours out. Prove the query does not.
reset()
greenieboard_row(ago='5 seconds')
got = find(prefix="SET TIME ZONE 'Asia/Riyadh';\n")
check("the window holds when the session is not in UTC", got != '',
      "a non-UTC session would never find greenieboard's row")

# ---------------------------------------------------------------- INSERT
reset()
got = run(bind(sql.INSERT, {
    'mission_id': 1, 'ucid': 'U1', 'unit_type': 'FA-18C_hornet', 'grade': 'OK',
    'comment': 'LSO: GRADE:OK: WIRE# 3', 'place': 'CVN_72', 'case': 1, 'wire': 3,
    'night': False, 'points': 4.0, 'image': '\\x00', 'trapdata': '{}',
    'tgroove': 17.9}))
check("the fallback INSERT satisfies greenieboard's constraints, and returns its verdict",
      got == '1|4.0|1|f', f"got {got!r}")

run("DROP TABLE traps; DROP TABLE players;")

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    print(('  ok  ' if ok else ' FAIL '), label, f'-- {detail}' if detail and not ok else '')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
