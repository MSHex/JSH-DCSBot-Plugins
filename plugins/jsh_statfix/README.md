# Plugin "JSH StatFix"

Repairs the playtime DCSServerBot credits for sessions nobody flew, on a
schedule, so the ranks are gated on hours people actually spent in a cockpit.

## The problem

When a pilot leaves a slot cleanly, the bot records the moment they left. That
is correct, and it is what happens to the overwhelming majority of sessions.

When they *don't* — a DCS crash, a dropped connection, a server kill — the row
is left open. The next time that server registers, the bot repairs it by
stamping `hop_off` with **the mission's end time**
(`plugins/userstats/listener.py`, `close_all_statistics`):

```sql
UPDATE statistics SET hop_off = (SELECT mission_end FROM missions WHERE id = %s)
WHERE mission_id = %s AND player_ucid = %s AND slot = %s AND hop_off IS NULL
```

A pilot who dropped out at 19:30 of a mission that ran until 06:00 is credited
with ten and a half hours. Afterwards the session looks entirely normal — a
join time, a leave time, a takeoff — so nothing flags it.

On JSH this reached the point where **232 sessions, 1.7% of the total, carried
2,743 of 6,052 recorded hours**: a mission record left open from 24 July to
7 August credited five pilots with roughly 323 hours each, from sessions whose
last recorded action was 21 to 46 minutes after they joined.

This is in the bot's own source, so it keeps happening. This plugin repairs the
damage on a schedule rather than patching the bot, which an update would undo.

## The three passes

Run in this order, in one transaction. Order matters.

1. **Close the orphans.** Sessions still open and still growing. Doing this
   first is what stops the bot's own repair inventing the hours. Only touches
   sessions older than `min_age_hours`, so a pilot in the air right now is
   never closed underneath them.
2. **Correct from recorded events.** `missionstats` timestamps every takeoff,
   shot, hit and landing, so a pilot's last event in a session is a firm lower
   bound for when they were still there — far closer than the mission's end.
   Plus `grace_minutes` for the flying after the last trackable event. Only
   ever *shortens* a session.
3. **Clip the overlaps.** One person has one DCS client and cannot occupy two
   slots at once, so a session running past the start of that pilot's next
   session is wrong by definition, and that next `hop_on` is a firm upper
   bound. This needs no `missionstats`, is a strictly tighter bound than pass
   2, and catches what pass 2 is blind to: instructor and artillery-commander
   sessions, which generate no events because those slots shoot at nothing.
   Driving the overlap count to zero is the same operation seen from the other
   side — an overlap is exactly what it removes.

Pass 2 is skipped, not failed, when `missionstats` is not installed.

## Commands (DCS Admin / Admin)

| Command | Description |
| --- | --- |
| `/statfix preview` | How many sessions each pass would touch. Reads only |
| `/statfix run [dry_run]` | Run now. `dry_run` defaults to **True** |
| `/statfix status` | The last eight runs, with what each changed |

`dry_run` does the work, counts it, then unwinds the transaction — so the
numbers it reports are what a real run would do, not an estimate.

## Installation

1. Copy `plugins/jsh_statfix` into the bot's `plugins` directory on **both**
   nodes, and `config/jsh_statfix.yaml` into `config/plugins/` on the master.
2. Add `jsh_statfix` to `opt_plugins` in `config/main.yaml`.
3. **Take a backup before the first real run:**
   `CREATE TABLE statistics_backup_YYYYMMDD AS SELECT * FROM statistics;`
4. Restart the bot. `jsh_statfix_runs` is created on load.

Leaving `dry_run: true` for the first month is worth it: `/statfix status` then
shows what it *would* have done, with nothing written.

## What it does not do

**It does not make hours mean flight time.** `hop_on`/`hop_off` measures slot
occupancy, so an instructor or artillery-commander slot accrues hours like a
cockpit, and a pilot parked on a ramp accrues them like one flying. Those are
real recorded occupancy, not corruption, and this plugin leaves them alone.
Changing what *counts* as flight time is a decision about the rank ladder, not
a repair.

**It does not touch credits.** Only `statistics.hop_off` is ever written.

**Hours go down, so promotion progress moves backwards.** Our ladder needs both
credits and hours, so a pilot mid-progress toward a rank will see the hours half
of that progress fall. Nobody loses a rank they already hold. The corrected
number is simply the real one.

## Node safety

The scheduler starts only on the master node (`self.node.master`). Every node
loads every plugin, and two nodes repairing the same table at once is not
something to leave to chance. The slash commands work from any node, because
they are explicit.

## Testing

`tests/statfix_sql_check.py` prepares and runs every statement in `sql.py`
against a real PostgreSQL on a scratch schema shaped like the JSH failure. It
guards the three properties that would silently corrupt `statistics` if they
broke:

- a live session (open, started minutes ago) is **never** closed;
- a correction only ever **shortens** a session, never lengthens one;
- running the repair twice changes nothing the second time.

```bash
initdb -D /tmp/pg -A trust && pg_ctl -D /tmp/pg -o '-k /tmp' start
python3 -I tests/statfix_sql_check.py "host=/tmp dbname=postgres user=postgres"
```
