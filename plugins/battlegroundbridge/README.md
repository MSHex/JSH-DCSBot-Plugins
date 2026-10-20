# Plugin "battlegroundbridge"

Connects DCS F10 map markers and mission drawings to
[DCSBattleground](https://github.com/Frigondin/DCSBattleground), so a pilot can
report intel from the cockpit and have it appear on the map the rest of the
coalition is looking at.

> **Read "What is not verified" before deploying this.** It has never run
> against a live DCS server or a live Battleground instance.

## Reporting intel

Place an F10 marker and type:

```text
-intel: Suspected SAM
```

The text after the colon is the title. Following lines become the report's
initial description:

```text
-intel: Suspected SAM
Two launchers near the northern road.
Radar appears active.
```

The bot replies in chat with the assigned intel ID.

| Action | Effect |
| --- | --- |
| Edit the marker's title or position | The same report moves and retitles. The ID does not change. |
| **Remove the F10 marker** | **Nothing happens to the report.** Intel outlives the marker. |
| `-intel delete` on that marker | Deletes the report that marker created. |
| `-intel delete 1000000` | Deletes that intel ID on this server. Works from any marker. |

After creation, the marker owns only the **title and position**. Descriptions,
screenshots, colour and status added on the website are preserved through every
later marker edit — editing a marker never overwrites someone's notes.

The command prefix follows the bot's own `chat_command_prefix`, so a server that
uses something other than `-` keeps working.

## Permissions

Ownership is recorded against the reporting pilot's **UCID**, not their name.

- Authors can update and delete their own reports.
- Admins (`admin_roles`, or a UCID in `admin_ucids`) can manage anyone's.
- Nobody can touch another player's report otherwise, another server's reports,
  website-created geometry, or imported drawings.

The marker's visible text is never treated as proof of identity. Identity comes
from the event initiator, and both the player name **and** the unit name must
match exactly one active player. Anything ambiguous is rejected rather than
guessed, because the failure mode of guessing is one pilot deleting another's
reports.

### GM and spectator markers

Marker events from GM and spectator slots frequently arrive with **no
identifiable initiator**. Those marker commands are refused and logged; they are
not silently applied to whoever seems likely.

This is not claimed to work from a GM slot — it has not been tested there. Two
authenticated fallbacks exist for exactly this case:

- `-intel delete <id>` in **in-game chat**, which carries a real UCID.
- `/bgbridge delete` in Discord, for an admin.

## Mission drawings

Drawings on the **Common** layer are imported to blue on every new mission, on
the first successful initialisation after a bot restart, and on
`/bgbridge refresh`.

| DCS drawing | Becomes | Notes |
| --- | --- | --- |
| Open line / polyline | `line` | Curved line modes are drawn straight between the exported vertices. |
| Closed line, polygon with vertices | `zone` | |
| Circle (centre + radius) | `circle` | |
| Text box | `markpoint` | Labelled with its text. Font and appearance are not reproduced. |
| Icon | `markpoint` | Labelled with its name. The DCS icon artwork is not reproduced. |
| Rectangle | `zone` | DCS exports no vertices for these; the corners are derived from `width`, `height` and `angle`. See below. |
| Ellipse / arrow | *skipped* | Described by `r1`/`r2`/`length` rather than width and height. Not guessed at. |
| Anything else | *skipped* | |

### Colours

A drawing keeps the colour it has in the Mission Editor. DCS stores it as
`0xRRGGBBAA` and only the RGB is used: Battleground geometry has a single
`color` field for the outline with its own fill rules, so there is nowhere for
a separate fill colour or an opacity to go — and an outline-only box still
needs a visible outline.

Anything missing or unparseable falls back to the default yellow rather than
failing the import. Marker intel is unaffected; a report has no mission colour
and keeps the recon default.

### Derived rectangles

A DCS rectangle is stored as a point plus `width`, `height` and `angle`, with no
corner list at all, so its four corners have to be calculated. Three things had
to be settled first, and all three were settled against a real mission
(`Egypt_Went_Rogue` 1.3.8, 50 rects on its Common layer) rather than assumed:

- **`mapX`/`mapY` is the centre, not a corner.** Measured by counting how many
  mission units each interpretation encloses: centre placed units inside 15 of
  65 rects and enclosed 490 units; corner managed 10 rects and 139 units.
- **`height` runs north-south, `width` east-west.** The same test preferred this
  on all rects, on elongated ones, and on very elongated ones, where
  orientation matters most.
- **`angle` is degrees anticlockwise** about the centre.

Corner offsets are converted to degrees with a flat local approximation rather
than the terrain's true projection, since the only exact converter lives in the
mission environment and drawings arrive already converted. Over a box of a few
kilometres that costs a couple of metres, which is why rectangles are reported
as approximated rather than exact.

Skipped and approximated shapes are **named and counted** in the log and in
`/bgbridge status`. Nothing is converted into invented geometry: a wrong line on
a map gets briefed as if it were real, which is worse than a gap.

A refresh replaces only drawings this bridge imported. Marker intel and
website-created entries are untouched. DCS is authoritative for drawings, which
is why they are replaced outright rather than merged the way intel is.

## Threat rings

Off by default. When `threat_rings.enabled` is on, anything whose **title names
a SAM system** gets a circle drawn around it at that system's range — both
pilot-reported intel and imported mission labels.

```yaml
threat_rings:
  enabled: true
  colour: "#ff0000"
  label: "{name} Range"
  systems:
    SA-5: 297100
    SA-10: 119265
    SA-11: 51275
```

The radii are **metres and a doctrine choice**, not a physical constant. These
seed values are the ones JSH was already drawing by hand, so a generated ring
lands on top of the manual one instead of disagreeing with it. Edit them and
add systems freely.

### Friendly coverage

A system's value can be a mapping instead of a plain radius, overriding the
colour and the wording — which is how friendly coverage is distinguished from
enemy threat:

```yaml
systems:
  SA-10: 119265
  Patriot:
    radius: 160000
    colour: "#0068ff"
    label: "{name} Coverage"
```

Only the **colour** changes. Every ring is written to the same coalition as the
rest of the server's geometry, because a ring written to the other side would
simply be invisible to the pilots who need it.

The sample config ships friendly systems commented out. Unlike the SA-5/10/11
radii, which came off the JSH map, those are starting points rather than
measured values — set them to whatever your briefings assume before enabling
them.

Matching is case-insensitive and accepts `SA-10`, `SA10` and `SA 10`. Longer
designations win, so a config containing both `SA-1` and `SA-10` never gives an
SA-10 the shorter one's radius — and `SA-100` matches neither.

Rings attach to **point features only**: intel reports and markpoints, never
zones, lines or circles. A title that already describes a ring never gets
one, so hand-drawn range circles and the plugin's own rings are both left
alone. `skip_pattern` has to cover the wording your labels use — `coverage` is
in the default list precisely because friendly rings are labelled
`<name> Coverage`, and without it a mission label reading `Patriot Coverage`
would be given a ring of its own.

An intel ring follows its report. Move the marker and the ring moves; retitle
`SA-10` to `empty revetment` and the ring disappears; delete the report and the
ring goes with it. Rings generated from mission drawings are replaced wholesale
on every import, like the drawings themselves.

## Commands

| Command | Role |
| --- | --- |
| `/bgbridge refresh` | `admin_roles` |
| `/bgbridge delete` | `admin_roles` |
| `/bgbridge status` | `admin_roles` |

All replies are ephemeral.

## Configuration

| Key | Meaning |
| --- | --- |
| `enabled` | Off by default. Opt in per instance. |
| `server_names` | Allowlist of DCS server names. **Required** — empty means the bridge stays off. |
| `poll_seconds` | How often the mission is drained. Clamped to 3–60. |
| `import_drawings` | Whether to import mission drawings at all. |
| `drawing_layers` | Which layers to import. `[Common]` by default. |
| `retain_intel_on_mission_change` | `true` keeps reports across missions. `false` clears bridge-created intel when a genuinely new mission starts. |
| `intel_expiry_hours` | `0` disables expiry. Measured from the last DCS-driven change, so website edits do not extend it. |
| `threat_rings` | Automatic range circles for titles naming a SAM system. Off by default. |
| `admin_roles`, `admin_ucids` | Who may manage other players' reports. |

Two gates, not one: `enabled` **and** membership of `server_names`. A
Battleground display alias is not necessarily the DCS server name.

## Installation

1. Back up your bot configuration and database.
2. Stop the bot and the affected DCS instance.
3. Copy this folder into `<bot>/plugins/battlegroundbridge/`.
4. Copy `config/battlegroundbridge.yaml` into `<bot>/config/plugins/` and set the
   instance name and `server_names` to your own.
5. Add `battlegroundbridge` to `opt_plugins` in `config/main.yaml`, **after**
   `battleground2`, keeping every other optional plugin in place.
6. Start the bot, then start DCS.

On a multi-node setup, do this on **every** node. The bot copies a plugin's
`lua/` folder to the servers of nodes that list it in `opt_plugins`, and only at
bot startup — a node that was missed has no mission Lua, so marker commands
there are never seen.

The bridge's tables are created by normal plugin initialisation, under the bot's
own database role. Do not create them by hand as `postgres`: the objects end up
owned by the wrong role and the first write fails.

DCSBattleground must be configured with `serverbot: true`, pointed at the same
database as the bot, and have a server entry whose `dcsname` exactly matches the
DCS server name. This plugin adds no authentication of its own and does not
change the existing coalition access configuration.

### Checking it took

- `dcs.log` — `[bgb] mission environment loaded`
- bot log — `BgBridge: <server>: drawings -- N imported, ...`
- `/bgbridge status` — shows a mission token once the first poll has run

### Rollback

1. Remove `battlegroundbridge` from `opt_plugins` and restart the bot.
2. Restart DCS to unload its mission Lua handler.
3. Leave the `bgb_*` tables in place unless you deliberately want them gone;
   they are what identifies which map records the bridge created.

Records already created or imported stay on the map. Removing the plugin does
not remove them.

## Known limits

- **A deleted report stays visible on an already-open map until the page is
  reloaded.** DCSBattleground removes a record from a live map only when its ID
  arrives in the `Delete` list of a `SESSION_SHARED_GEOMETRY` message, and that
  list is only ever populated by the website's own share handler — a row deleted
  directly in SQL simply stops being re-sent. Creation and updates propagate
  normally. Fixing deletion needs a change to DCSBattleground itself; a patch is
  supplied separately and is **not** part of this plugin.
- Marker events from before the plugin was first enabled are not recovered.
  Editing the marker again picks it up.
- Events held in mission memory do not survive a DCS crash before the bot has
  committed them.
- At most 500 pending markers and 50 events per poll. A full queue drops **new**
  marker commands and logs an error; it is visible, not silent.
- `bgb_receipts` and `bgb_audit` grow with use. There is no automatic pruning.
- Red-side publication is not implemented. Everything is published to blue.

## Verification, and what is not verified

Run the pure-logic tests from the repository root:

```text
python -m pytest -q tests/test_battlegroundbridge.py
```

The SQL is tested separately against a **real** PostgreSQL instance — not a
stub, and not PGlite:

```text
python3 tests/bgb_sql_check.py "host=/tmp port=5433 dbname=bgbtest user=postgres"
```

Point that at a disposable database. It inserts, updates and deletes, and it
runs every statement the plugin uses through `PREPARE` against the live schema.

**What was actually run:** 114 pure-logic assertions, and 20 SQL assertions
against PostgreSQL 16.15 — covering the create/update/delete lifecycle,
ownership, cross-server rejection, receipt idempotency, deletion replay
protection, expiry, and preservation of website descriptions and screenshots.

**What was NOT run, at all:**

- No DCS server. The Lua has never executed — there was no Lua interpreter
  available, so it is reviewed code, not tested code.
- No live DCSBattleground. Nothing has been seen to appear on a real map.
- `commands.py`, `listener.py` and `store.py` have never been imported, because
  `discord.py` and `psycopg` could not be installed in the build environment.
  They are syntax-checked only.
- `posPoint` ordering is `[lat, lon]`, matching Leaflet and the website's own
  editor, but has not been confirmed against the game. **A swapped pair would
  put reports somewhere plausible rather than somewhere obviously wrong**, so
  this is the first thing to check live.
- The sample configuration has not been run through DCSServerBot's own schema
  validators.

## Live acceptance checklist

Automated tests prove none of the real behaviour. Before general use:

1. Load a mission with a known Common line, polygon, circle and text label.
   Compare the import against the logged skipped and approximated counts.
2. In a blue aircraft, create intel over a known airfield. **Compare the
   coordinates against the DCS F10 map** before trusting anything else.
3. Note the returned ID. Edit the marker; confirm the ID is unchanged.
4. Add a note and a screenshot on the website, edit the marker again, and
   confirm both survive.
5. Remove the F10 marker; confirm the report remains.
6. Delete it by ID; confirm it goes (expect to need a page reload — see Known
   limits).
7. With a second pilot, confirm ownership is enforced.
8. Confirm unknown IDs, drawing IDs and another server's IDs are all refused.
9. Restart only the bot with DCS still running; confirm later marker edits
   update rather than duplicate.
10. Reload the mission; confirm retention and that drawings are replaced.
11. Restart Battleground; confirm stored records return and deleted ones do not.

## Requirements

- DCSServerBot 3.x — developed against **3.0.4.27**
- The `mission` and `battleground2` plugins
- PostgreSQL
- DCSBattleground with `serverbot: true`, against the same database

Inspected against DCSServerBot `7e598e4` and DCSBattleground `b3d6aed`.
