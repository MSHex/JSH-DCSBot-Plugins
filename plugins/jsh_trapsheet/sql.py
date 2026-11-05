"""
The SQL jsh_trapsheet runs against greenieboard's `traps` table.

Pure constants, no imports, so tests/trapsheet_sql_check.py can run them
against a real PostgreSQL built from greenieboard's own tables.sql.

Why ATTACH and not INSERT
-------------------------
greenieboard handles S_EVENT_LANDING_QUALITY_MARK itself (process_sc_event ->
process_lso_event) and INSERTs a row for every Super Carrier grade: it derives
day or night from mission time, sets the case, scores from the configured
`grades`, and credits the points. A second INSERT from here would put every
graded pass on the board twice, at two different scores. So the trapsheet is
attached to greenieboard's row, and a row is only inserted when greenieboard
writes none -- which happens when FunkMan is enabled in greenieboard.yaml,
because it then ignores Super Carrier events entirely.

Finding the right row
---------------------
Both plugins react to the same event and nothing orders them, so
greenieboard's row may not exist yet when this runs: the caller retries for
a few seconds before falling back to INSERT. And an older, picture-less row
for the same pilot -- a pass whose samples were lost -- must never take the
picture meant for this one. So the match is narrow: same pilot, same
mission, same grade, no trapsheet yet, written in the last 30 seconds, most
recent first. Two passes by one pilot with one grade inside 30 seconds is
not something a carrier pattern allows.

`time` is stored as UTC WITHOUT a time zone (`DEFAULT (now() AT TIME ZONE
'utc')`), so the window is computed the same way. Comparing it against a
bare now() would be off by the database session's UTC offset, which would
quietly widen or close the window by hours.
"""

#: Attach a trapsheet to greenieboard's row for this pass. RETURNING id, so
#: the caller can tell whether it found one.
ATTACH = """
    UPDATE traps
       SET trapsheet = %(image)s,
           trapdata  = %(trapdata)s,
           tgroove   = COALESCE(tgroove, %(tgroove)s)
     WHERE id = (
           SELECT id
             FROM traps
            WHERE player_ucid = %(ucid)s
              AND mission_id  = %(mission_id)s
              AND grade       = %(grade)s
              AND trapsheet IS NULL
              AND time > (now() AT TIME ZONE 'utc') - %(window)s::interval
            ORDER BY time DESC, id DESC
            LIMIT 1
            FOR UPDATE)
    RETURNING id
"""

#: Only when greenieboard wrote no row of its own. Mirrors what
#: process_lso_event writes, so a fallback row looks like any other.
INSERT = """
    INSERT INTO traps (mission_id, player_ucid, unit_type, grade, comment,
                       place, trapcase, wire, night, points, trapsheet,
                       trapdata, tgroove)
    VALUES (%(mission_id)s, %(ucid)s, %(unit_type)s, %(grade)s, %(comment)s,
            %(place)s, %(case)s, %(wire)s, %(night)s, %(points)s, %(image)s,
            %(trapdata)s, %(tgroove)s)
    RETURNING id
"""

#: How far back a greenieboard row may be and still be this pass's.
WINDOW = '30 seconds'
