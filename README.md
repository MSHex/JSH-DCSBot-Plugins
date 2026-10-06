# JSH DCSServerBot Plugins

Plugins for [DCSServerBot](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot),
written for the JSH servers and used in production there.

| Plugin | What it does |
| --- | --- |
| [`jsh_credits`](plugins/jsh_credits/) | Holds kill credits until the pilot lands at a friendly airbase or FARP. Dropped on crash, eject, death, slot change or disconnect. |
| [`jsh_csar`](plugins/jsh_csar/) | Pays credits for CSAR rescues and records them per pilot status. Supports Joker ADV_CSAR, Foothold, Ciribob CSAR and MOOSE Ops.CSAR. |
| [`jsh_airport`](plugins/jsh_airport/) | GM commands to set an airbase, FARP or ship warehouse to a level (0-3) from Excel sheets, apply battle damage, and track the level per airbase. |
| [`hardcore`](plugins/hardcore/) | Hardcore mode and the flight economy: pays per flown block, with penalties for losses. |
| [`awardautomation`](plugins/awardautomation/) | Grants Logbook medals automatically from campaign statistics, qualifications and Hardcore sessions. |
| [`rankstatus`](plugins/rankstatus/) | `/pilot status` — rank, credits, promotion progress, combat record, airframes, awards. |
| [`jsh_ranks`](plugins/jsh_ranks/) | Makes a Foothold mission enforce the Discord rank ladder instead of its own in-mission rank credits. |

Each plugin is independent and has its own README with install steps, commands
and configuration. They do interact through credits, though — see below.

## Who writes credits

Credits live in [CreditSystem](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot/blob/master/plugins/creditsystem/README.md)'s `credits` table, with every change recorded in
`credits_log`. More than one plugin writes there, and the whole point of the
arrangement is that no two of them ever pay for the same thing:

| Writer | Pays for | `credits_log` event |
| --- | --- | --- |
| `jsh_credits` | kills, held until the pilot lands at a friendly base | `rtb` |
| CreditSystem | mission-script awards via `addUserPoints`, relabelled by whoever earned them | `csar`, … |
| `hardcore` | flight economy settlement, and the Hardcore buy-in | `flight economy`, `hardcore buy-in` |
| `awardautomation` | medal rewards | its own ledger |

`jsh_credits` deliberately stays out of every path but kills. If you add another
credit source, give it its own event name and make sure nothing else pays the
same event.

## Installation

Each plugin folder follows the standard DCSServerBot layout:

1. Copy `plugins/<name>/` into `<bot>/plugins/<name>/`.
2. Copy `plugins/<name>/config/<name>.yaml` into `<bot>/config/plugins/`.
3. Add `<name>` to `opt_plugins` in `<bot>/config/main.yaml`.
4. Restart the bot.

On a multi-node setup, do this on every node.

`DEFAULT` in each config applies to all servers; a section named after an
**instance** (the names under `instances:` in `nodes.yaml`, not the server names
in `servers.yaml`) overrides it for that server. `enabled: false` in an instance
section opts that server out.

## jsh_credits: required CreditSystem configuration

> **Read this before deploying `jsh_credits`, and don't restore a stock
> `creditsystem.yaml` afterwards.** These settings are what stop credits being
> written twice, and getting them wrong produces no error anywhere — just wrong
> balances.

| Setting | File | Value |
| --- | --- | --- |
| `points_per_kill` | `creditsystem.yaml` | **removed** |
| `points_on_rtb` | `creditsystem.yaml` | `false` |
| `multiplier` | `creditsystem.yaml` | `1.0` |
| `payback` | `slotblocking.yaml` | `false`, or don't load SlotBlocking |

Removing `points_per_kill` makes CreditSystem's kill handler a no-op, leaving
`jsh_credits` as the only source of kill points. `points_on_rtb: false` lets
CreditSystem credit mission awards as they arrive — CSAR and logistics rewards
are earned at a friendly field already, so holding them to the next landing only
destroys them. `payback: false` stops SlotBlocking paying out a deposit nothing
else uses.

A campaign must be running on the server (see the GameMaster plugin), or
CreditSystem awards nothing at all.

### Why the interlock exists

Stock CreditSystem and SlotBlocking write credits through the same shared
attribute, `CreditPlayer.deposit`, from two code paths that don't know about each
other. Two opposite failures result:

- **Kills paid twice.** `points_on_rtb` is only checked in the `addUserPoints`
  handler. The kill handler credits the balance immediately *and* copies the same
  points into `deposit`, which SlotBlocking then pays out again on landing.
- **Mission awards paid zero times.** With `points_on_rtb: true` they go only
  into `deposit`, with no audit row, and are discarded whenever a death or slot
  change clears it.

Measured on one active player over two days: 206 points overpaid on kills, 44
points of CSAR never credited at all. `jsh_credits` fixes this by owning kills
outright, in its own private pending store that no other plugin can reach, and
staying out of every other credit path.

## Requirements

- DCSServerBot 3.x
- PostgreSQL (each plugin creates its own tables on first load)
- `jsh_credits` and `jsh_csar` require the [CreditSystem](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot/blob/master/plugins/creditsystem/README.md) plugin
- `jsh_airport` needs the level sheets in `config/plugins/jsh_airport/`

Nothing here grants awards or medals: `jsh_csar`'s tables
(`jsh_csar_totals`, `jsh_csar_dynamic`) are published for another plugin to build
award rules from.

## Versioning

Each plugin's `version.py` is the source of truth and is printed by the bot at
startup, so a running bot's log identifies exactly what is deployed. Bump it with
every behavioural change.

**Two components only — `MAJOR.MINOR`.** DCSServerBot's migration path in
`core/plugin.py` does `ver, rev = installed.split('.')`, so a three-part version
like `1.0.2` raises `too many values to unpack (expected 2)` and the plugin fails
to load. The version is also written to the `plugins` table on first install, so
a bad value there keeps breaking later versions until the row is corrected:

```sql
DELETE FROM plugins WHERE plugin = '<plugin_name>';
```

That is safe for a plugin with no `db/tables.sql`; for one that owns tables, set
the row to a valid two-part version instead of deleting it.

## Contributing

Live configs — real UCIDs, Discord role names, channel IDs, tokens — must never
be committed. The YAML files in each plugin's `config/` are samples with
placeholder instance names; keep them that way.

## Credits

These plugins are built on
[DCSServerBot](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot) by
**Special-K**, and would not exist without it. The plugin API, the
[CreditSystem](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot/blob/master/plugins/creditsystem/README.md)
ledger they all write to, and the mission-side plumbing they hook into are his
work.

Several of these plugins integrate with
[Leka's Foothold](https://github.com/leka1986/Lekas-Foothold) by **leka1986** --
`jsh_csar` reads its rescue and enemy-pilot-capture stats, `jsh_ranks` replaces
its rank gates, and `jsh_airport` drives the airbase warehouses behind it.
Thanks also to the Joker ADV_CSAR author, whose script `jsh_csar` also supports.

## License

MIT. See [LICENSE](LICENSE).
