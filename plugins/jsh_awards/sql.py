"""
jsh_awards' queries. Tested against the bot's own DDL on a real PostgreSQL by
tests/cq_awards_sql_check.py. Every timestamp is naive UTC, as the bot stores
them; %(start)s and %(end)s bound one calendar month, end exclusive.
"""

#: Credits EARNED in the month: the gains in credits_log, not the balance --
#: InactivityDecay's deductions, spending and voting costs are losses and do
#: not subtract from what a pilot earned. Transfers between pilots (the
#: 'donation' event, by default) are excluded, or a donation would win it.
CREDITS = """
    SELECT l.player_ucid AS ucid, p.name, p.discord_id,
           SUM(l.new_points - l.old_points) AS value
      FROM credits_log l
      JOIN players p ON p.ucid = l.player_ucid
     WHERE l.time >= %(start)s AND l.time < %(end)s
       AND l.new_points > l.old_points
       AND NOT (lower(l.event) = ANY(%(exclude)s::text[]))
       AND (%(campaign_id)s::int IS NULL OR l.campaign_id = %(campaign_id)s::int)
     GROUP BY l.player_ucid, p.name, p.discord_id
     ORDER BY value DESC, p.name
     LIMIT %(top)s
"""

#: jsh_csar's own event log, every server and every CSAR script.
CSAR = """
    SELECT r.player_ucid AS ucid, p.name, p.discord_id, SUM(r.rescues) AS value
      FROM jsh_csar_rescues r
      JOIN players p ON p.ucid = r.player_ucid
     WHERE r.rescued_at >= %(start)s AND r.rescued_at < %(end)s
     GROUP BY r.player_ucid, p.name, p.discord_id
    HAVING SUM(r.rescues) > 0
     ORDER BY value DESC, p.name
     LIMIT %(top)s
"""

#: Slot time inside the month, each session CLIPPED to it, so a sortie over
#: midnight on the 1st is split between the two months. Open sessions are
#: skipped (no end yet), and so are reversed ones the bot leaves behind
#: (hop_off < hop_on -- see jsh_statfix), which would otherwise subtract.
#: This is slot occupancy, as the bot's own hours are; see CLAUDE.md.
HOURS = """
    SELECT s.player_ucid AS ucid, p.name, p.discord_id,
           ROUND((SUM(EXTRACT(EPOCH FROM (LEAST(s.hop_off, %(end)s) - GREATEST(s.hop_on, %(start)s))))
                  / 3600.0)::numeric, 1) AS value
      FROM statistics s
      JOIN players p ON p.ucid = s.player_ucid
     WHERE s.hop_off IS NOT NULL
       AND s.hop_off >= s.hop_on
       AND s.hop_on < %(end)s AND s.hop_off > %(start)s
     GROUP BY s.player_ucid, p.name, p.discord_id
    HAVING SUM(EXTRACT(EPOCH FROM (LEAST(s.hop_off, %(end)s) - GREATEST(s.hop_on, %(start)s)))) > 0
     ORDER BY value DESC, p.name
     LIMIT %(top)s
"""

#: Kills and deaths of one kind, for sessions that STARTED in the month.
#: Air-to-air: aircraft killed, against deaths to aircraft. Air-to-ground:
#: ground units, SAMs and ships killed, against deaths to those. Matching the
#: two sides keeps a strike pilot's K/D from being sunk by one Su-27.
_KD = """
    SELECT s.player_ucid AS ucid, p.name, p.discord_id,
           SUM({kills})::int AS kills, SUM({deaths})::int AS deaths
      FROM statistics s
      JOIN players p ON p.ucid = s.player_ucid
     WHERE s.hop_on >= %(start)s AND s.hop_on < %(end)s
     GROUP BY s.player_ucid, p.name, p.discord_id
    HAVING SUM({kills}) > 0
"""
KD_AA = _KD.format(kills="s.kills_planes + s.kills_helicopters",
                   deaths="s.deaths_planes + s.deaths_helicopters")
KD_AG = _KD.format(kills="s.kills_ground + s.kills_sams + s.kills_ships",
                   deaths="s.deaths_ground + s.deaths_sams + s.deaths_ships")

TABLES = """
    SELECT to_regclass('credits_log') IS NOT NULL AS credits,
           to_regclass('jsh_csar_rescues') IS NOT NULL AS csar,
           to_regclass('logbook_pilot_awards') IS NOT NULL AS logbook
"""

#: One row per month posted, so a restart on the 1st does not post twice and
#: a bot that was down on the 1st still posts when it comes back.
POSTED = "SELECT 1 FROM jsh_awards_boards WHERE month = %(month)s"
ANY_POSTED = "SELECT EXISTS (SELECT 1 FROM jsh_awards_boards)"
MARK_POSTED = """
    INSERT INTO jsh_awards_boards (month, posted_at, message_id, note)
    VALUES (%(month)s, (now() AT TIME ZONE 'utc'), %(message_id)s, %(note)s)
    ON CONFLICT (month) DO UPDATE
       SET posted_at = EXCLUDED.posted_at, message_id = EXCLUDED.message_id,
           note = EXCLUDED.note
"""

AWARD_ID = "SELECT id FROM logbook_awards WHERE name = %(name)s"
GIVE_AWARD = """
    INSERT INTO logbook_pilot_awards (player_ucid, award_id, granted_by, granted_at, citation)
    SELECT %(ucid)s, %(aid)s, NULL, (now() AT TIME ZONE 'utc'), %(citation)s
     WHERE NOT EXISTS (SELECT 1 FROM logbook_pilot_awards
                        WHERE player_ucid = %(ucid)s AND award_id = %(aid)s
                          AND citation = %(citation)s)
"""
