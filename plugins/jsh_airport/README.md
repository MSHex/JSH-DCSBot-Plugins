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
- `/airport list server [side] [kind] [stocked_only]` — every airbase on the
  current map with its ICAO, its coalition and its level, which the dropdown
  can't show in full (Discord stops at 25 choices and most maps have far more
  airfields than that). Filter by side, by airfields/FARPs/ships, or to only
  those with a level set.
- `/airport status server` — every airbase with a level, who set it and when.

`/airport damage` leaves the stored level alone, because a level is what the
base was stocked to, not what is left in it. The exception is 100%: the base can
no longer spawn or arm anything, so it is recorded as level 0 (destroyed).
Restocking it is a normal `/airport level`.

## Picking an airbase
`airbase` takes an ICAO code or a name, typed or picked from the dropdown. These
are all the same airfield:

```
/airport level server:JSH-1 airbase:OMAM       level:2
/airport level server:JSH-1 airbase:Al Dhafra  level:2
/airport level server:JSH-1 airbase:dhafra     level:2
```

A partial name or ICAO works as long as it matches one airbase; if it matches
several, the reply lists them instead of guessing. ICAO is only ever an input —
the levels table and the warehouse calls always key on the airbase name, so
nothing in storage changes.

The dropdown covers every airbase on the map, each shown as
`OMAM - Al Dhafra AB [blue] - level 2`, and searches names and ICAO codes
together. Discord only ever shows 25 choices, so the ordering is what makes it
usable: an exact ICAO first, then codes and names starting with what you typed,
then anything containing it. Typing `OMA` puts OMAM at the top rather than
burying it. When more than 25 match, the last entry says how many are hidden.
For the whole map at once, use `/airport list`.

### Where the list comes from
Two sources, because neither is complete:

- The bot's own mission data covers every airfield on the terrain plus the
  mission's FARPs and carriers, and is the **only** place the ICAO code exists —
  DCS keeps it in the terrain config, which the mission scripting environment
  can't read. It doesn't say who holds an airfield.
- This plugin's `listAirbases` reads the live `Airbase` objects, which do know
  their coalition.

Merging them gives a list that is both complete and current. Where the two
spell a name differently (`Al Dhafra AB` vs `Al-Dhafra AB`) they are matched on
letters and digits alone, and the **mission's** spelling is the one kept, because
that is what `Airbase.getByName` is given downstream.

If the mission can't be reached, the dropdown falls back to the bot's mission
data without coalitions; failing that, to airbases already set on that server;
and failing that, it shows why it is empty rather than nothing at all. A typed
name always works.

### If the dropdown is empty
The dropdown says which of these it is, rather than showing nothing.

- **"loading airbases from the mission"** — normal on the first use after a
  mission starts. Discord gives autocomplete about 3 seconds and the round trip
  through DCS can take longer, so the first attempt may miss. The reply is
  cached when it lands, so typing another letter fills the list.
- **"cannot read airbases: ..."** — the mission did not answer. Check `dcs.log`
  for `[jsh_airport] loaded`. The bot copies `mission.lua` to
  `Saved Games/<instance>/Scripts/net/DCSServerBot/jsh_airport/mission.lua`
  at **bot startup**, so installing the plugin needs a bot restart followed by a
  DCS server restart. A mission running an older `mission.lua` reports that
  directly.

`dcs.log` also shows `[jsh_airport] listed N airbases` each time the list is
read, which is the quickest way to tell a mission-side problem from a bot-side
one: if that line appears and the dropdown is still empty, the problem is in the
bot.

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

Upgrading to 1.3 changes no tables and no configuration: ICAO input, the new
dropdown ordering and `/airport list` all read data the bot already has. It does
need the **Mission** plugin loaded (it is, by default) — that is what fills the
bot's airbase list, and so where every ICAO code comes from.
