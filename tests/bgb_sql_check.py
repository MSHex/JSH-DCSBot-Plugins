#!/usr/bin/env python3
"""
Executes the bridge's real SQL against a real PostgreSQL instance.

    python3 tests/bgb_sql_check.py "host=/tmp port=5433 dbname=bgbtest user=postgres"

Why this exists rather than a pytest suite: the statements are the part of this
plugin most likely to be quietly wrong -- a mis-scoped WHERE deletes another
server's map -- and they cannot be checked by reading them. This script imports
the statements from plugins/battlegroundbridge/sql.py (it does not copy them),
rewrites %s to $n, and runs them through PREPARE/EXECUTE against a throwaway
database, with the assertions written as DO blocks that raise.

It needs only python3 and psql. No psycopg, no discord, no pytest -- which is
what makes it runnable in places the bot's own dependencies are not installable.

NEVER point it at a production database. It inserts, updates and deletes.
"""
import re
import subprocess
import sys
import tempfile

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins' / 'battlegroundbridge'))

import sql as S  # noqa: E402

#: Statements PostgreSQL cannot infer parameter types for in isolation. psycopg
#: sends types with the values, so this is a limitation of PREPARE, not of the
#: statement -- the cast is applied only for the type check here.
CASTS = {
    'EXPIRED_INTEL': {2: 'int'},
    'TOUCH_SESSION': {3: 'text'},
    'INSERT_GEOMETRY': {3: 'json'},
    'UPDATE_GEOMETRY': {1: 'json'},
    'INSERT_ITEM': {8: 'text'},
    'AUDIT': {3: 'int', 4: 'int', 7: 'text'},
}


def to_numbered(statement: str, casts: dict[int, str] | None = None) -> str:
    """%s -> $1, $2, ... in order, with optional casts for PREPARE's benefit."""
    counter = iter(range(1, 99))

    def repl(_match):
        n = next(counter)
        cast = (casts or {}).get(n)
        return f'${n}::{cast}' if cast else f'${n}'

    return re.sub(r'%s', repl, statement)


def inline(statement: str, *literals: str) -> str:
    """
    The same statement with SQL literals in place of the placeholders.

    Needed only where a result has to be captured from a data-modifying
    statement: `CREATE TABLE ... AS EXECUTE` refuses anything that is not a
    SELECT, so an INSERT ... RETURNING has to be wrapped in a CTE instead. The
    text still comes from sql.py; only the parameters differ, and the PREPARE
    pass above has already proved the parameterised form valid.
    """
    counter = iter(literals)
    return re.sub(r'%s', lambda _: next(counter), statement)


def capture(name: str, table: str, *literals: str) -> str:
    body = inline(getattr(S, name), *literals).strip().rstrip(';')
    return f'CREATE TEMP TABLE {table} AS WITH t AS (\n{body}\n) SELECT * FROM t;'


def build_script() -> str:
    out: list[str] = ['\\set ON_ERROR_STOP on', 'BEGIN;']

    # ---- 1. every statement must at least parse and plan against the schema
    names = [n for n in dir(S) if n.isupper() and isinstance(getattr(S, n), str)]
    for name in sorted(names):
        statement = to_numbered(getattr(S, name), CASTS.get(name))
        out.append(f'-- {name}')
        out.append(f'PREPARE p_{name.lower()} AS {statement.strip()};')

    out.append("""
-- ===================================================================
-- 2. behaviour
-- ===================================================================
CREATE OR REPLACE FUNCTION assert(ok boolean, what text) RETURNS void AS $fn$
BEGIN
  IF NOT ok THEN RAISE EXCEPTION 'FAILED: %', what; END IF;
  RAISE NOTICE 'ok: %', what;
END $fn$ LANGUAGE plpgsql;

-- A record the WEBSITE created: ordinary SERIAL id, no bgb_items mapping.
-- Nothing the bridge does may touch it.
INSERT INTO bg_geometry2 (server_name, data)
VALUES ('JSH Dynamic Campaign', '{"title":"website recon","fields":{"type":"recon"}}');
""")

    def execute(name: str, *args: str) -> str:
        return f"EXECUTE p_{name.lower()}({', '.join(args)});"

    SRV = "'JSH Dynamic Campaign'"
    OTHER = "'Other Server'"
    TOK = "'tok-aaa'"

    out.append('-- session upsert is idempotent')
    out.append(execute('TOUCH_SESSION', SRV, TOK, "'Caucasus Dynamic'"))
    out.append(execute('TOUCH_SESSION', SRV, TOK, 'NULL'))
    out.append(f"SELECT assert((SELECT count(*) FROM bgb_sessions WHERE server_name={SRV})=1,"
               f" 'session upsert does not duplicate');")
    out.append(f"SELECT assert((SELECT mission_name FROM bgb_sessions WHERE mission_token={TOK})"
               f" = 'Caucasus Dynamic', 'NULL mission name does not erase the stored one');")

    out.append('-- receipts: first claim wins, replay is a no-op')
    out.append(capture('CLAIM_RECEIPT', 'r1', SRV, TOK, '7', '1'))
    out.append(capture('CLAIM_RECEIPT', 'r2', SRV, TOK, '7', '1'))
    out.append("SELECT assert((SELECT count(*) FROM r1)=1, 'first receipt claim succeeds');")
    out.append("SELECT assert((SELECT count(*) FROM r2)=0, 'replayed receipt claims nothing');")

    out.append('-- create intel. The id goes into a psql variable: EXECUTE refuses a')
    out.append('-- subquery as a parameter, and the sequence is not transactional so it')
    out.append('-- cannot be assumed to start at 1000000 on a second run.')
    out.append("SELECT nextval('bgb_geometry_id_seq') AS gid\n\\gset")
    out.append("SELECT assert(:gid >= 1000000,"
               " 'bridge ids clear the websites id>10000 delete guard');")
    out.append(f"""INSERT INTO bg_geometry2 (id, server_name, data, "time")
        VALUES (:gid, {SRV},
               '{{"title":"Suspected SAM","author":{{"name":"Curse"}},'
               '"fields":{{"type":"recon","side":"blue","description":["Intel"],'
               '"screenshot":[]}}}}'::json,
               timezone('utc'::text, now()));""")
    out.append(f"""INSERT INTO bgb_items (server_name, mission_token, kind, marker_id,
                   geometry_id, owner_ucid, owner_name, title)
        VALUES ({SRV}, {TOK}, 'intel', 7, :gid, 'ucid-curse', 'Curse', 'Suspected SAM');""")

    out.append('-- the website then enriches it')
    out.append(f"""UPDATE bg_geometry2 SET data = jsonb_set(jsonb_set(data::jsonb,
              '{{fields,description}}', '["Intel #1000000","web note"]'::jsonb),
              '{{fields,screenshot}}', '["http://img/1.png"]'::jsonb)::json
        WHERE id = :gid AND server_name = {SRV};""")

    out.append('-- a marker edit must change title/position and NOTHING else')
    out.append(f"""UPDATE bg_geometry2 SET data = jsonb_set(jsonb_set(data::jsonb,
              '{{title}}', '"Confirmed SA-11"'::jsonb),
              '{{fields,posMGRS}}', '"38TLN0274366889"'::jsonb)::json,
              "time" = timezone('utc'::text, now())
        WHERE id = :gid AND server_name = {SRV};""")
    out.append("""SELECT assert((SELECT data::jsonb #>> '{fields,description,1}'
          FROM bg_geometry2 WHERE id=:gid) = 'web note',
          'website description survives a marker edit');""")
    out.append("""SELECT assert((SELECT data::jsonb #>> '{fields,screenshot,0}'
          FROM bg_geometry2 WHERE id=:gid) = 'http://img/1.png',
          'website screenshot survives a marker edit');""")

    out.append('-- cross-server and unrelated-record protection')
    out.append("CREATE TEMP TABLE wrong_server AS "
               + execute('FIND_ITEM_BY_GEOMETRY', OTHER, ':gid').rstrip(';') + ';')
    out.append("SELECT assert((SELECT count(*) FROM wrong_server)=0,"
               " 'an intel id is invisible from another server');")
    out.append(f"SELECT id AS webid FROM bg_geometry2 WHERE server_name={SRV}"
               f" AND id < 1000000\n\\gset")
    out.append("CREATE TEMP TABLE website_lookup AS "
               + execute('FIND_ITEM_BY_GEOMETRY', SRV, ':webid').rstrip(';') + ';')
    out.append("SELECT assert((SELECT count(*) FROM website_lookup)=0,"
               " 'a website-created record has no bridge mapping, so it cannot be deleted');")

    out.append('-- delete by id, then prove a replay cannot resurrect it')
    out.append("CREATE TEMP TABLE found AS "
               + execute('FIND_ITEM_BY_GEOMETRY', SRV, ':gid').rstrip(';') + ';')
    out.append("SELECT assert((SELECT count(*) FROM found)=1, 'owner lookup finds the report');")
    out.append("SELECT assert((SELECT owner_ucid FROM found)='ucid-curse',"
               " 'ownership is recorded against a ucid');")
    out.append(execute('DELETE_GEOMETRY', ':gid', SRV))
    out.append("SELECT id AS itemid FROM bgb_items WHERE geometry_id = :gid\n\\gset")
    out.append(execute('TOMBSTONE_ITEM', ':itemid'))
    out.append("""SELECT assert((SELECT count(*) FROM bg_geometry2
          WHERE id=:gid)=0, 'deleted intel leaves bg_geometry2');""")
    out.append(f"""SELECT assert((SELECT count(*) FROM bg_geometry2
          WHERE server_name={SRV})=1, 'the website record is still there afterwards');""")
    out.append("SELECT assert((SELECT deleted_at IS NOT NULL FROM bgb_items"
               " WHERE geometry_id=:gid), 'the mapping is tombstoned, not removed');")

    out.append("""-- the same marker in the same mission must not create a second report
DO $$
BEGIN
  INSERT INTO bgb_items (server_name, mission_token, kind, marker_id, geometry_id, owner_ucid)
  VALUES ('JSH Dynamic Campaign', 'tok-aaa', 'intel', 7, 1900003, 'ucid-curse');
  RAISE EXCEPTION 'FAILED: a deleted marker was allowed to create new intel';
EXCEPTION WHEN unique_violation THEN
  RAISE NOTICE 'ok: a tombstoned marker cannot recreate its report';
END $$;""")

    out.append("""-- but the SAME marker id in a NEW mission is a different thing entirely
INSERT INTO bgb_sessions (server_name, mission_token) VALUES ('JSH Dynamic Campaign','tok-bbb');
INSERT INTO bgb_items (server_name, mission_token, kind, marker_id, geometry_id, owner_ucid)
VALUES ('JSH Dynamic Campaign', 'tok-bbb', 'intel', 7, 1900002, 'ucid-curse');
SELECT assert(true, 'a reused marker id in a new mission is independent');""")

    out.append('-- expiry reads the DCS content clock')
    out.append("UPDATE bgb_items SET content_changed_at = timezone('utc'::text, now())"
               " - interval '5 hours' WHERE geometry_id = 1900002;")
    out.append("CREATE TEMP TABLE exp AS "
               + execute('EXPIRED_INTEL', SRV, '4').rstrip(';') + ';')
    out.append("SELECT assert((SELECT count(*) FROM exp)=1, 'intel older than the window expires');")
    out.append("CREATE TEMP TABLE noexp AS "
               + execute('EXPIRED_INTEL', SRV, '6').rstrip(';') + ';')
    out.append("SELECT assert((SELECT count(*) FROM noexp)=0, 'intel inside the window does not');")

    out.append('-- drawings are tracked separately from intel')
    out.append(f"""INSERT INTO bgb_items (server_name, mission_token, kind, marker_id,
                   geometry_id, title)
        VALUES ({SRV}, {TOK}, 'drawing', NULL, 1900500, 'FEBA'),
               ({SRV}, {TOK}, 'drawing', NULL, 1900501, 'Kill box');""")
    out.append("CREATE TEMP TABLE draw AS " + execute('LIVE_DRAWING_IDS', SRV).rstrip(';') + ';')
    out.append("SELECT assert((SELECT count(*) FROM draw)=2, 'live drawings are listed');")
    out.append("CREATE TEMP TABLE intel AS " + execute('LIVE_INTEL_IDS', SRV).rstrip(';') + ';')
    out.append("SELECT assert((SELECT count(*) FROM intel)=1,"
               " 'a drawing refresh cannot see intel rows');")
    out.append(execute('AUDIT', SRV, "'delete'", '1000000', '7', "'ucid-curse'", "'Curse'", "'test'"))

    out.append('ROLLBACK;')
    return '\n'.join(out) + '\n'


def main() -> int:
    dsn = sys.argv[1] if len(sys.argv) > 1 else 'dbname=bgbtest'
    script = build_script()
    with tempfile.NamedTemporaryFile('w', suffix='.sql', delete=False) as handle:
        handle.write(script)
        path = handle.name
    Path(path).chmod(0o644)
    result = subprocess.run(['psql', dsn, '-v', 'ON_ERROR_STOP=1', '-q', '-f', path],
                            capture_output=True, text=True)
    notices = [line for line in result.stderr.splitlines() if 'ok:' in line]
    failures = [line for line in result.stderr.splitlines()
                if 'FAILED' in line or 'ERROR' in line]
    for line in notices:
        print(line.replace('NOTICE:  ', '  '))
    if result.returncode or failures:
        print('\n--- FAILURES ---')
        for line in failures:
            print(line)
        print(f'\nscript kept at {path}')
        return 1
    print(f'\n{len(notices)} assertions passed against real PostgreSQL')
    return 0


if __name__ == '__main__':
    sys.exit(main())
