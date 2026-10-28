"""
Every statement the repair runs, as plain strings.

Kept apart from commands.py so the SQL can be prepared against a real
PostgreSQL without importing discord or DCSServerBot -- see
tests/statfix_sql_check.sql. The repair rewrites the statistics table, which
is the only record of who flew what; SQL that has never been executed outside
the bot is not something to point at that table.
"""

#: Sessions the bot never saw end. The next registerDCSServer will repair these
#: itself by stamping them with the mission's end time, so closing them here
#: first is what stops that repair inventing the hours in the first place.
#:
#: The age guard is the safety rail: a session opened minutes ago is somebody
#: flying right now, and its hop_off is supposed to be NULL.
#:
#: The outer GREATEST is the OTHER guard, and shipping without it was a bug.
#: close_all_statistics sets a mission's end to the NEXT mission's start minus
#: one second, so a session that opened after that boundary has a hop_on LATER
#: than its own mission's recorded end. Closing it against that end wrote
#: hop_off < hop_on, and every tsrange(hop_on, hop_off) in the bot -- rankstatus,
#: CreditSystem, AwardAutomation -- then raised
#: "range lower bound must be less than or equal to range upper bound".
CLOSE_ORPHANS = """
    UPDATE statistics s
       SET hop_off = GREATEST(
               COALESCE(
                   m.mission_end,
                   (SELECT MAX(s2.hop_off) FROM statistics s2
                     WHERE s2.mission_id = s.mission_id),
                   s.hop_on + INTERVAL '1 minute'),
               s.hop_on + INTERVAL '1 minute')
      FROM missions m
     WHERE m.id = s.mission_id
       AND s.hop_off IS NULL
       AND s.hop_on < (now() AT TIME ZONE 'utc') - %(min_age)s::interval
"""

#: Sessions DCSServerBot closed at the mission's end rather than when the pilot
#: left. missionstats timestamps every takeoff, shot, hit and landing, so the
#: pilot's last event in that session is a firm lower bound for when they were
#: still there -- far closer than the mission end.
#:
#: Only ever shortens: the last condition drops any row where the corrected
#: time would be later than what is recorded.
CORRECT_BY_EVENTS = """
    WITH fix AS (
        SELECT s.mission_id, s.player_ucid, s.slot, s.hop_on,
               GREATEST(ev.last_event + %(grace)s::interval,
                        s.hop_on + INTERVAL '1 minute') AS new_hop_off
          FROM statistics s
          JOIN missions m ON m.id = s.mission_id
          CROSS JOIN LATERAL (
              SELECT MAX(ms."time") AS last_event
                FROM missionstats ms
               WHERE ms.mission_id = s.mission_id
                 AND ms.init_id = s.player_ucid
                 AND ms."time" BETWEEN s.hop_on AND s.hop_off
          ) ev
         WHERE s.hop_off IS NOT NULL
           AND s.hop_off = m.mission_end
           AND ev.last_event IS NOT NULL
           AND ev.last_event + %(grace)s::interval < s.hop_off
    )
    UPDATE statistics s
       SET hop_off = f.new_hop_off
      FROM fix f
     WHERE f.mission_id  = s.mission_id
       AND f.player_ucid = s.player_ucid
       AND f.slot        = s.slot
       AND f.hop_on      = s.hop_on
"""

#: Whatever the first two passes could not reach. One person has one DCS
#: client and cannot occupy two slots at once, so a session running past the
#: start of that pilot's next session is wrong by definition and the next
#: hop_on is a firm upper bound.
#:
#: Needs no missionstats, is a strictly tighter bound than CORRECT_BY_EVENTS,
#: and catches the instructor and artillery-commander sessions that pass two is
#: blind to -- those slots shoot at nothing, so they generate no events at all.
#: Driving the overlap count to zero is the same operation seen from the other
#: side: an overlap is exactly what this removes.
CLIP_OVERLAPS = """
    WITH clip AS (
        SELECT s.mission_id, s.player_ucid, s.slot, s.hop_on,
               GREATEST(nxt.next_hop_on,
                        s.hop_on + INTERVAL '1 minute') AS new_hop_off
          FROM statistics s
          CROSS JOIN LATERAL (
              SELECT MIN(n.hop_on) AS next_hop_on
                FROM statistics n
               WHERE n.player_ucid = s.player_ucid
                 AND n.hop_on > s.hop_on
          ) nxt
         WHERE s.hop_off IS NOT NULL
           AND nxt.next_hop_on IS NOT NULL
           AND nxt.next_hop_on < s.hop_off
    )
    UPDATE statistics s
       SET hop_off = c.new_hop_off
      FROM clip c
     WHERE c.mission_id  = s.mission_id
       AND c.player_ucid = s.player_ucid
       AND c.slot        = s.slot
       AND c.hop_on      = s.hop_on
"""

#: Aliased, because preview() reads it through dict_row and an unaliased
#: ROUND() would arrive as the column "round".
TOTAL_HOURS = """
    SELECT COALESCE(ROUND(SUM(EXTRACT(EPOCH FROM (hop_off - hop_on)))/3600.0, 2), 0) AS hours
      FROM statistics WHERE hop_off IS NOT NULL
"""

#: Pass two is skipped rather than failed when missionstats is not installed.
HAS_MISSIONSTATS = "SELECT to_regclass('public.missionstats') IS NOT NULL"

OVERLAP_COUNT = """
    SELECT COUNT(*)
      FROM statistics a
      JOIN statistics b
        ON a.player_ucid = b.player_ucid
       AND (a.mission_id, a.slot, a.hop_on) < (b.mission_id, b.slot, b.hop_on)
       AND a.hop_on < b.hop_off AND b.hop_on < a.hop_off
     WHERE a.hop_off IS NOT NULL AND b.hop_off IS NOT NULL
"""

#: What the next run would do, without doing it.
PREVIEW = """
    SELECT
      (SELECT COUNT(*) FROM statistics s
        WHERE s.hop_off IS NULL
          AND s.hop_on < (now() AT TIME ZONE 'utc') - %(min_age)s::interval) AS orphans,
      (SELECT COUNT(*) FROM statistics s JOIN missions m ON m.id = s.mission_id
        WHERE s.hop_off IS NOT NULL AND s.hop_off = m.mission_end)            AS machine_closed,
      (SELECT COUNT(*) FROM statistics s
        CROSS JOIN LATERAL (
            SELECT MIN(n.hop_on) AS next_hop_on FROM statistics n
             WHERE n.player_ucid = s.player_ucid AND n.hop_on > s.hop_on) nxt
        WHERE s.hop_off IS NOT NULL AND nxt.next_hop_on IS NOT NULL
          AND nxt.next_hop_on < s.hop_off)                                    AS overlapping
"""

#: Due-ness is measured from the last successful run in the table, never from
#: process uptime: a bot that restarts weekly would otherwise never reach a
#: monthly interval.
LAST_RUN = """
    SELECT MAX(finished_at) FROM jsh_statfix_runs
     WHERE dry_run = FALSE AND error IS NULL AND finished_at IS NOT NULL
"""

START_RUN = """
    INSERT INTO jsh_statfix_runs (dry_run, triggered_by, hours_before)
    VALUES (%s, %s, %s) RETURNING id
"""

FINISH_RUN = """
    UPDATE jsh_statfix_runs
       SET finished_at = timezone('utc'::text, now()),
           orphans_closed = %s, corrected_by_events = %s, overlaps_clipped = %s,
           hours_after = %s, error = %s
     WHERE id = %s
"""

RECENT_RUNS = """
    SELECT id, started_at, finished_at, dry_run, triggered_by,
           orphans_closed, corrected_by_events, overlaps_clipped,
           hours_before, hours_after, error
      FROM jsh_statfix_runs
     ORDER BY started_at DESC
     LIMIT %s
"""
