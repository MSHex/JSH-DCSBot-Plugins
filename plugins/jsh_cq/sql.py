"""
jsh_cq's queries. Tested against the bot's own DDL on a real PostgreSQL by
tests/cq_awards_sql_check.py.

`traps` is greenieboard's; `logbook_*` are the logbook plugin's. jsh_cq owns
no tables -- the point is that a CQ lives where every other qualification
already does, and shows in /logbook pilot and /qualification list.
"""

#: Qualifying traps inside the window, for one pilot or (ucid NULL) for all.
#: `traps.time` is UTC without a time zone, hence `now() AT TIME ZONE 'utc'`.
#: A trap with no wire never counts, whatever its points: greenieboard's
#: points come from config, and a bolter must not qualify anyone.
TRAPS = """
    SELECT player_ucid, night, time
      FROM traps
     WHERE points >= %(min_points)s
       AND wire IS NOT NULL
       AND time >= (now() AT TIME ZONE 'utc') - make_interval(days => %(valid_days)s)
       AND (cardinality(%(places)s::text[]) = 0 OR place = ANY(%(places)s::text[]))
       AND (%(ucid)s::text IS NULL OR player_ucid = %(ucid)s::text)
"""

#: Make sure the two qualifications exist. The logbook seed file already
#: creates CQ_CV_DAY and CQ_CV_NIGHT; this only matters on an install that
#: skipped it, or for names changed in config. An existing row is left as it is.
ENSURE_QUALIFICATION = """
    INSERT INTO logbook_qualifications (name, description, valid_days)
    VALUES (%(name)s, %(description)s, %(valid_days)s)
    ON CONFLICT (name) DO NOTHING
"""

QUALIFICATION = """
    SELECT id, requirements FROM logbook_qualifications WHERE name = %(name)s
"""

#: The pilot's current row, locked, so a sweep and a trap evaluating the same
#: pilot at once cannot both decide it is a new grant.
HELD = """
    SELECT expires_at FROM logbook_pilot_qualifications
     WHERE player_ucid = %(ucid)s AND qualification_id = %(qid)s
     FOR UPDATE
"""

GRANT = """
    INSERT INTO logbook_pilot_qualifications
           (player_ucid, qualification_id, granted_by, granted_at, expires_at)
    VALUES (%(ucid)s, %(qid)s, NULL, (now() AT TIME ZONE 'utc'), %(expires)s)
    ON CONFLICT (player_ucid, qualification_id) DO UPDATE
       SET expires_at = EXCLUDED.expires_at,
           granted_at = CASE WHEN logbook_pilot_qualifications.expires_at
                                  <= (now() AT TIME ZONE 'utc')
                             THEN EXCLUDED.granted_at
                             ELSE logbook_pilot_qualifications.granted_at END
"""

AWARD_ID = """
    SELECT id FROM logbook_awards WHERE name = %(name)s
"""

HAS_AWARD = """
    SELECT 1 FROM logbook_pilot_awards
     WHERE player_ucid = %(ucid)s AND award_id = %(aid)s LIMIT 1
"""

GIVE_AWARD = """
    INSERT INTO logbook_pilot_awards (player_ucid, award_id, granted_by, granted_at, citation)
    VALUES (%(ucid)s, %(aid)s, NULL, (now() AT TIME ZONE 'utc'), %(citation)s)
"""

TABLES = """
    SELECT to_regclass('traps') IS NOT NULL AS traps,
           to_regclass('logbook_pilot_qualifications') IS NOT NULL AS logbook
"""
