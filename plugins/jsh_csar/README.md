# JSH CSAR plugin for DCSServerBot

Pays CreditSystem credits for CSAR rescues and records how many pilots each
player has rescued, by pilot status.

## Install (both nodes)
1. Copy this folder to `<bot>/plugins/jsh_csar/`.
2. Copy `config/jsh_csar.yaml` to `<bot>/config/plugins/jsh_csar.yaml`.
3. Add `jsh_csar` to `opt_plugins` in `config/main.yaml`.
4. Set `enabled: false` under the instance name of any server that shouldn't use it.
5. Restart the bot, then restart the DCS servers.

## Check it works
`dcs.log` should show `[jsh_csar] loaded`, then `attached joker`
(or `attached foothold` / `attached ciribob` / `attached moose:...`).

## Supported CSAR scripts
| Script | Pilot status reported |
|---|---|
| Joker ADV_CSAR | Healthy, Wounded, Critical, RedPilot (captured enemy) |
| Foothold | Unknown (friendly rescues), RedPilot (enemy pilots delivered) |
| Ciribob CSAR | Unknown |
| MOOSE Ops.CSAR | Unknown |

Joker's auto-rescue (a pilot landing inside a friendly base) pays nothing,
because there is no rescuer. It is not recorded either.

Any callback already set on `ADV_CSAR.OnRescueCallback` is kept and called
first, so this doesn't replace an existing token economy hook.

## Commands
- `/csar stats [user]` — rescues by pilot status for a player.
- `/csar top [pilot_status] [limit]` — leaderboard.
- `/csar history server [limit]` — recent rescues on a server (DCS Admin / GameMaster).

## Credits log
Credit entries from rescues are labelled `csar` in the event column (change it
with `credits_log_event`) with a remark like `for rescuing 2 critical pilots`, next to the
bot's own `kill`, `payback` and `donation` entries. The reason is passed to
`addUserPoints()`, and a second later the plugin labels that entry itself.
Column names are detected at runtime; an unexpected schema disables the
labelling with a warning and nothing else changes.

Remarks and the in-game message are built from `labels:`, so the database keys
stay `Critical`, `RedPilot` and so on while players read "critical pilot" and
"captured enemy pilot". Set `announce_in_game: false` to drop the plugin's own
"+4 server credits" line if the credit system already announces the points.

## Database
- `jsh_csar_rescues` — event log, one row per rescue event and pilot status:
  server, ucid, name, status, count, credits, reason, source, dynamic flag, timestamp.
- `jsh_csar_dynamic` — dynamic campaign only, totals per `(player_ucid, pilot_status)`
  with credits, last reason and last rescue.
  A server feeds it only when `dynamic_campaign: true` is set for that instance.
- `jsh_csar_totals` — every server and every CSAR script, one row per player:
  total pilots rescued, credits, last reason, last rescue.

Both totals tables key on `player_ucid` (FK to `players.ucid`), the key the logbook
and userstats plugins use, so they join directly. This plugin grants no awards of
its own -- these tables are the source another plugin builds them from:

```sql
-- pilots rescued, all servers
SELECT p.name, t.rescues
FROM jsh_csar_totals t JOIN players p ON p.ucid = t.player_ucid;

-- 10+ critical pilots rescued in the dynamic campaign
SELECT p.name, d.rescues
FROM jsh_csar_dynamic d JOIN players p ON p.ucid = d.player_ucid
WHERE d.pilot_status = 'Critical' AND d.rescues >= 10;
```

Rescues are recorded even when the reward is 0, so setting a status to 0 in
the YAML keeps the counts without paying credits.

Upgrading from 1.0: `db/update_v1.0.sql` adds the new columns, creates
`jsh_csar_dynamic`, and rebuilds `jsh_csar_totals` per player.
