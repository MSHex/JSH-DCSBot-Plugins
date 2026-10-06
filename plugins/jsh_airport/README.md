# Airport plugin for DCSServerBot

Gives GMs Discord commands to set an airbase warehouse to a level (0-3) from
Excel sheets and tracks the level per airbase.

## Install (both nodes)
1. Copy this folder to `<bot>/plugins/jsh_airport/`.
2. Copy `config/jsh_airport.yaml` to `<bot>/config/plugins/jsh_airport.yaml`.
3. Copy the level sheets to `<bot>/config/plugins/jsh_airport/`
   (`warehouse-lvl0.xlsx` ... `warehouse-lvl3.xlsx`). Keep these names; they are
   not uploaded to Discord, so the bot's `warehouse-<ICAO>` convention doesn't apply.
4. Add `jsh_airport` to `opt_plugins` in `config/main.yaml`.
5. Set `enabled: false` under the instance name of any server that shouldn't use it.
6. Restart the bot, then restart the DCS servers.

## Check it works
`dcs.log` should show `[jsh_airport] loaded` on enabled servers.

## Commands (DCS Admin / GameMaster)
- `/airport level server airbase level` — pick the level from the dropdown
  (0 stripped, 1 resupplied, 2 forward base, 3 logistics hub). Applies that
  level's Excel sheet and records the level. Any level can be set at any time,
  so this covers both upgrades and a base being lost.
- `/airport damage server airbase percent` — battle damage. Deducts 25%, 50%,
  75% or 100% from **what is in the warehouse right now**, not from the level
  sheet, so whatever players have already used is accounted for: a base down to
  half a sheet loses half of that half. Aircraft, weapons and liquids are all
  scaled. The reply shows the totals before and after.
- `/airport status server` — every airbase with a level, who set it and when.

`/airport damage` leaves the stored level alone, because a level is what the
base was stocked to, not what is left in it. The exception is 100%: the base can
no longer spawn or arm anything, so it is recorded as level 0 (destroyed).
Restocking it is a normal `/airport level`.

The airbase dropdown lists the airfields, FARPs and ships in the running
mission, read from DCS itself, each tagged with its coalition and its stored
level if it has one. The list is refreshed on every mission load. If the mission
can't be reached, it falls back to airbases already set on that server; if there
are none of those either, the dropdown shows why it is empty rather than nothing
at all. A typed name always works.

### If the dropdown is empty
It means the mission never answered. In order of likelihood:
1. `mission.lua` isn't loaded. `dcs.log` should have `[jsh_airport] loaded`.
   The bot copies it to
   `Saved Games/<instance>/Scripts/net/DCSServerBot/jsh_airport/mission.lua`
   at **bot startup**, so installing the plugin needs a bot restart and then a
   DCS server restart.
2. The mission is running an older `mission.lua` without the function being
   called — again, restart the mission.
3. The plugin is disabled for that instance in `jsh_airport.yaml`.

## Warehouse sheets
Use exports from `/airbase info` unchanged: sheets Aircraft, Weapons, Liquids
with columns Name and Count. Liquids rows 0-3 are jet fuel, avgas, MW50, diesel
(kg). Every row is applied as an absolute count. Items DCS rejects are listed in
the reply; everything else still applies.

## Database
- `jsh_airport_levels` — current level per (server, airbase), with who set it and when.
  `/airport damage` only writes here at 100%, which records level 0.

Upgrading from 1.0: `db/update_v1.0.sql` drops the payouts table. Player rewards
are no longer part of this plugin.
