# JSH CQ 1.0 — carrier qualification

Grants **carrier qualifications** from greenieboard's traps and writes them into
the **logbook** plugin's own tables. So a CQ shows in `/logbook pilot` and
`/qualification list` like any other qualification, carries its ribbon in the
ribbon rack, and expires through logbook's own machinery.

Needs **greenieboard** and **logbook**.

## Why not logbook's own auto-grant

logbook can grant a qualification from a `carrier_landings` requirement, but
that is a lifetime count of every `traps` row:

- bolters, wave-offs and cut passes all count as landings
- day and night are not told apart
- a 90-day qualification renews forever, because the lifetime count never drops

jsh_cq applies real rules instead.

## The rules (defaults)

| | Needs |
| --- | --- |
| **Day CQ** (`CQ_CV_DAY`) | 4 day traps in the last 90 days |
| **Night CQ** (`CQ_CV_NIGHT`) | 2 night traps in the last 90 days, **and** a current day CQ |

A trap counts when it caught a wire and scored `min_points` or more (3, i.e.
`(OK)` Fair or better, with greenieboard's default grades). Day or night is
greenieboard's own `night` flag.

A CQ **expires when the oldest trap that still counts drops out of the
window**. Every new trap pushes the expiry forward, so a pilot who keeps
trapping stays qualified and one who stops lapses. A night CQ never outlasts
the day CQ.

- A **manual grant** with no expiry (`/qualification grant`) is never touched.
- A qualification is never shortened.
- Only a **new** qualification is announced and given its ribbon. A renewal is
  silent, and the ribbon is given only once per pilot.

## When it checks

- About 15 seconds after each Super Carrier grade, for that pilot. The wait
  lets greenieboard's own row land first.
- Every `sweep_minutes` on the master node, for everyone. This catches anything
  missed while the bot was down.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/cq status [member]` | DCS | Day and night CQ, expiry dates, and progress such as `Day 3/4 · Night 0/2` |
| `/cq check` | DCS Admin | Re-applies the rules to everyone now |

## Setup

1. Copy `plugins/jsh_cq/` and `config/jsh_cq.yaml`, then restart the bot.
2. **Clear logbook's own requirements** on the two qualifications, or logbook
   grants them as well from its lifetime count. jsh_cq warns about this in the
   log once, and `/jsh doctor` reports it:

   ```sql
   UPDATE logbook_qualifications SET requirements = NULL
    WHERE name IN ('CQ_CV_DAY', 'CQ_CV_NIGHT');
   ```

3. For ribbons, create the awards with `/award create`, then set `day_award` and
   `night_award` in `jsh_cq.yaml`.
4. Run `/cq check` once to qualify everyone who already meets the rules.
