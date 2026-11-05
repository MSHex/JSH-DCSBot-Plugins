# Working on this repository

Custom DCSServerBot plugins for **JSH (Joker Shadow's Hangar)**: seven DCS
servers across two nodes — `NASA-SERVER` (master) and `GoyaGang` (agent).

Everything below is something that cost real time to find. None of it is in
DCSServerBot's documentation.

## Hard rules

- **Commit as `MSHex <mshex@users.noreply.github.com>`.** Never the real name
  or personal email. This applies to every commit and PR in any public repo.
- **Never commit live config.** Real UCIDs, Discord role names, channel IDs
  and tokens stay out. Sample configs use placeholder instance names
  (`your-instance-name`) and empty UCID lists.
- **Foothold is GPL-3.0-only.** Do not copy its code into this MIT repo. Read
  it to understand behaviour; reimplement rather than lift.
- **DCSBattleground has no licence file.** Do not redistribute a patched
  build, source or binary, from here.
- **Do not modify DCSServerBot's own source.** An update reverts it silently.
- **Do not modify Foothold either** — it updates frequently. Reach into it
  from injected Lua instead (see `jsh_ranks`' `cbuy:`), so a Foothold update
  can't quietly undo the change.

## DCSServerBot constraints

### Packaging — both of these fail at startup, not at import

- **Plugin versions are two components.** `core/plugin.py` migrates with
  `ver, rev = installed.split('.')`; a three-part version raises `ValueError`.
  Repository *tags* are full semver — see `VERSIONING.md`.
- **`__init__.py` must export `__version__`.** `core/plugin.py` reads the
  attribute off the **package**, not off `version.py`. We shipped v0.16 broken
  this way.

Both are covered by `tests/test_plugin_packaging.py`. Run it before tagging.

### Config

- **Sections key on the INSTANCE name** from `nodes.yaml`, never the server
  name from `servers.yaml`. A section named after a server silently does
  nothing. This has cost an evening twice. `func: is_node` in the schema now
  rejects it at load — set `validation: strict` in `main.yaml` so it does.
- **Schemas are load-bearing.** `core/plugin.py:450` validates config against
  `schemas/<name>_schema.yaml`; under strict validation a missing key stops
  the plugin. The same file builds the 3.0.5.0 web GUI's config form and the
  docs reference. Every leaf needs `desc` and `example`.
  `tests/schema_check.py` enforces this.
- **Migrations are named after the version upgraded FROM:**
  `db/update_v<installed>.sql`. `tables.sql` runs only on a first install, so
  an existing deployment never sees it. Use `IF NOT EXISTS` in both.

### Runtime traps

- **8192-byte UDP limit, and it is DIRECTIONAL.** This bites only going
  **bot → DCS**: `send_to_dcs` → `transport.sendto`, read by
  `UDPRecvSocket:receive()` in `DCSServerBotMain.lua` with no size argument,
  so LuaSocket's 8192-byte default applies and the rest is **silently
  truncated**. Minify injected Lua and check the datagram before sending.

  Three things this does *not* cover, each of which has already caused a
  wrong conclusion:

  - **It is per datagram, not a budget shared across plugins.** Each
    injection is its own packet. The receive loop is `repeat … until err`,
    draining the whole queue every simulation frame, against an OS buffer of
    64 KB+. Three plugins injecting ~5 KB each at startup is not a problem.
  - **Only `do_script` travels this way.** Lua shipped as files under
    `Scripts/net/DCSServerBot/<plugin>/` is read by `loadfile()` in
    `loadPlugin()` and has no size limit at all. `hardcore`'s `mission.lua`
    is ~11 KB and perfectly fine. Only `jsh_credits`, `jsh_ranks` and
    `jsh_trapsheet` inject.
  - **DCS → bot has no such limit.** `dcsbot.sendBotTable` chunks at
    `MAX_CHUNK = 65000` (`DCSServerBot.lua:14`) and the bot reassembles in
    `services/servicebus/service.py:1110` with `SO_RCVBUF` at 2 MB. Sending a
    few hundred samples up is never at risk.
- **`server.status` is unreliable inside `registerDCSServer`.** The mission
  plugin sets it inside *its own* handler, and listener order across plugins
  is not guaranteed, so `server.status in (RUNNING, PAUSED)` there is always
  false. Don't test it; DCS registering means DCS is there.
- **`asyncio.create_task` with no retained reference can be garbage-collected
  before it runs.** Keep a `_tasks` set with `add_done_callback(discard)`.

### Schema facts worth knowing

- `players.discord_id` is `NOT NULL DEFAULT -1`, **not NULL**. Any query
  matching on it needs `> 0`, or every unlinked pilot matches.
- `statistics` has **no `server_name`**; reach it via `JOIN missions`.
- `SUM(EXTRACT(EPOCH FROM (hop_off - hop_on)))` skips NULL `hop_off`, so open
  sessions **under**-count.

### Playtime is not flight time

`hop_on`/`hop_off` measures **slot occupancy**. Instructor and artillery
commander slots accrue hours like a cockpit, and a parked aircraft does too.
That is real recorded data, not corruption. Changing what counts is a decision
about the rank ladder, not a repair.

Two bugs live in the bot's own `close_all_statistics`, both still present in
3.0.5.0:

1. It sets `hop_off` to the **mission's end time** for any session it never
   saw close. A pilot who dropped out at 19:30 of a mission running to 06:00
   is credited ten and a half hours, and the row looks ordinary afterwards.
2. It does this **without a `GREATEST` guard**, so a session that opened after
   its own mission's recorded end gets `hop_off < hop_on`. Every
   `tsrange(hop_on, hop_off)` in the bot then raises *"range lower bound must
   be less than or equal to range upper bound"*, which breaks `/pilot status`
   and AwardAutomation and points nowhere near the cause.

`jsh_statfix` repairs both on a schedule. Its own floor must be **one second**,
matching `stop_player` — a one-minute floor stretches short sessions past the
next one and *creates* the overlaps it removes.

**Every slot change leaves a ~1-second overlap by design**, because the bot
closes one session and opens the next from two separately-taken timestamps.
That is not corruption. Don't rewrite it.

## DCS engine facts

- **A mark placed from a slot with no controllable unit carries no
  initiator** — Tactical Commander, Game Master, instructor, spectator.
  Mission-side Lua cannot identify such a player at all:
  `coalition.getPlayers()` returns units and a commander has none. Only the
  bot knows, from connection and slot state.
- **CA slot names are numbered per coalition**: `artillery_commander_blue_1`,
  not `artillery_commander`. Match them as **prefixes**; an exact-name list
  matches nothing on a real server.
- **`S_EVENT_REFUELING` names the TANKER as initiator**, not the receiver.
  3.0.5.0 adds a `refuelingstats` table that does key on the player.
- **`net.json2lua` does not exist in the mission environment.**
- `MARK_ADDED` (25) and `MARK_CHANGE` (26) both fire when text is typed into a
  new mark. Remember the `idx` or you act twice.
- **`S_EVENT_LANDING_QUALITY_MARK` only fires on Super Carrier decks.** No LSO
  grade exists on a Stennis, Kuznetsov or Tarawa without MOOSE — that is a DCS
  limit, not something a plugin can route around.
- **There is no `getAoA()`.** Compute it as the angle between `getVelocity()`
  and the longitudinal axis from `getPosition()`'s orthonormal basis, in the
  pitch plane. That is geometric AoA in **degrees**; the Hornet's indexer
  units track degrees closely and nobody else's do.

## greenieboard's trapsheet plotter

`plugins/greenieboard/trapsheet.py` draws the FunkMan picture and needs
neither MOOSE nor the FunkMan package — only `{X, Z, Alt, AoA}` plus
`aircraft`, `callsign`, `time`. Two traps, both found by running it:

- **The AoA panel keys off the FILENAME, not `pinfo['aircraft']`.** It does
  `ps = Path(trapfile).stem` and substring-matches raw DCS codes (`FA-18`,
  `EA-18`, `T-45`, `F-14A-135-GR`, `F-14B`, `AV8BNA`, `A-4E-C`) to pick the
  on-speed band. `jsh_trapsheet` passes a synthetic AIRBOSS-shaped name;
  nothing is ever opened.
- **There is no `else` branch.** An airframe matching none of those leaves
  `xmax` unbound and raises `UnboundLocalError` partway through drawing.
  Check the type before plotting, not after.
- `Z` is relative to the **ship's axis**, and the plotter rotates by `-9°`
  for the angled deck. A well-lined-up approach therefore has `Z` growing
  with distance astern; a `Z` of zero down the groove is an aircraft flying
  up the ship's *wake* and plots as ~950 ft of lineup error at 1 NM. It looks
  wrong and is not.
- **The AIRBOSS frame, checked against MOOSE's source rather than guessed.**
  `Airboss.lua` `_GetDistances()` projects the carrier→player vector onto the
  ship's own orientation axes, so `X`/`Z` are ship frame and `X` is
  **negative** astern — but `Airboss.lua:19177` writes `local b = -groove.X`,
  so the CSV column `trapsheet.py` reads is **positive** astern. `Z` is
  written raw, starboard positive. Project onto the carrier's own `p.x` and
  `p.z` rather than deriving a perpendicular by hand, and there is no second
  derivation to get backwards.

## Foothold

- External config lives at
  `Saved Games\<instance>\Missions\Saves\Foothold Config.lua` — **spaces, not
  underscores**. Wrong name means the `.miz` copy is used and nothing warns.
  On Normandy it's `Foothold Config WW2.lua`.
- It announces which one won: *"Loaded … externally"* vs *"… from mission"*,
  and shouts every 30 seconds on a syntax error.
- `RankingSystem = false` disables **all** rank gates; nothing `jsh_ranks`
  sends matters until it's true.
- The `buy:` marker handler defaults `myRank = 0` when it cannot name the
  player, so **every gated item is refused from a commander slot**. That is
  why `cbuy:` exists.
- `buyShopItem` has **no rank check of its own** — the gate is only in the
  marker handler. It needs `alternateParams` carrying the zone from the
  marker's position, or zone items answer *"choose a zone from shop menu"*.
- `bc` is a mission global (`_G.bc`).

## Our plugins

| Plugin | What it does |
| --- | --- |
| `jsh_credits` | Credits for landings, kills, CSAR and AAR |
| `jsh_trapsheet` | FunkMan-style trapsheets with no MOOSE; Super Carrier decks only |
| `jsh_ranks` | Bridges the Discord rank ladder to Foothold's; granted ranks; `cbuy:` |
| `rankstatus` | `/pilot status`. Reads other plugins' configs, has none of its own |
| `jsh_statfix` | Monthly playtime repair (see above) |
| `battlegroundbridge` | DCS F10 markers and drawings → DCSBattleground |
| `jsh_csar` | Credits for Joker's ADV_CSAR rescues |
| `jsh_airport` | Airbase warehouse levels and SAM installs |
| `jsh_coords` | Coordinate conversion |
| `hardcore` | Joker's hardcore economy |
| `awardautomation` | Automatic medals |

`rankstatus` imports `jsh_ranks.grants` **lazily, inside the method**: at
module level it would make rankstatus fail to load without `jsh_ranks`, and
`jsh_ranks.commands` already imports `plugins.rankstatus.ranks`, so the two
packages would import each other. Shared helpers go in a module that imports
nothing from DCSServerBot or discord. Where two plugins need the same small
helper and either can be installed alone, **duplicate it** rather than couple
them — see `jsh_ranks/slots.py` and `battlegroundbridge/domain.py`.

## How we work

- **Test SQL against a real PostgreSQL before pointing it at the live
  database.** Spin one up in the container. Several queries that looked
  obviously right were not.
- **Prove a test fails without the fix.** Twice, an assertion was correct and
  the *fixture* didn't contain the pathology, so the bug shipped anyway. A
  test that has never been seen to fail is worth little.
- **Quote source as source.** Python `%s` and Go `$1` placeholders pasted as
  explanation have twice been run as SQL. Mark them, or convert them.
- **Check a log line's timestamp** before treating it as proof a restart
  worked.
- **Measure against real data** rather than assuming. Rect corner geometry,
  colour byte order and the overlap distribution were all settled that way.
- Deliver SQL as a file with a read-only preview block first and `ROLLBACK`
  until the user changes it, plus a snapshot table for anything destructive:
  `CREATE TABLE statistics_backup_YYYYMMDD AS SELECT * FROM statistics;`

## Environment

- **Creating releases and pushing tags are refused (403)** from Claude Code
  sessions. Write the notes to a file, hand them over, and let the user create
  the release. Creating and merging PRs via the REST API works.
- There is **no Lua interpreter and no pip network** in the container. Hence
  `tests/lua_balance_check.py` and the hand-rolled `tests/schema_check.py`.
- The bot's source can be cloned fresh for reference:
  `git clone --depth 1 https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot.git`
