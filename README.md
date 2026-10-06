# JSH DCSServerBot Plugins

Three plugins for [DCSServerBot](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot),
written for the JSH servers and used in production there.

| Plugin | What it does |
| --- | --- |
| [`jsh_credits`](plugins/jsh_credits/) | Holds kill credits until the pilot lands at a friendly airbase or FARP. Dropped on crash, eject, death, slot change or disconnect. |
| [`jsh_csar`](plugins/jsh_csar/) | Pays credits for CSAR rescues and records them per pilot status. Supports Joker ADV_CSAR, Foothold, Ciribob CSAR and MOOSE Ops.CSAR. |
| [`jsh_airport`](plugins/jsh_airport/) | GM commands to set an airbase, FARP or ship warehouse to a level (0-3) from Excel sheets, and track the level per airbase. |

They are independent — install any one of them on its own. Each has its own
README with install steps, commands and configuration.

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
- `jsh_credits` and `jsh_csar` require the CreditSystem plugin
- `jsh_airport` needs the level sheets in `config/plugins/jsh_airport/`

Nothing here grants awards or medals: `jsh_csar`'s tables
(`jsh_csar_totals`, `jsh_csar_dynamic`) are published for another plugin to build
award rules from.

## Versioning

Each plugin's `version.py` is the source of truth and is printed by the bot at
startup, so a running bot's log identifies exactly what is deployed. Bump it with
every behavioural change.

## Contributing

Live configs — real UCIDs, Discord role names, channel IDs, tokens — must never
be committed. The YAML files in each plugin's `config/` are samples with
placeholder instance names; keep them that way.

## License

MIT. See [LICENSE](LICENSE).
