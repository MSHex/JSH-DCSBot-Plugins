# RankStatus

RankStatus is a standalone DCSServerBot plugin that adds `/pilot status` without modifying the built-in CreditSystem.
It reads campaign credits, campaign flight time, rank thresholds, combat statistics, preferred airframes, and squadron
membership, then adds the pilot's Logbook awards and generated ribbon rack to the status card. When the optional
Punishment and Hardcore plugins are enabled, it can also show private discipline and Hardcore information.

## Requirements

- DCSServerBot 3.x
- Mission, UserStats, GameMaster, CreditSystem, and Logbook plugins
- An active campaign
- CreditSystem `achievements` configured in `config/plugins/creditsystem.yaml`

No database migration or RankStatus configuration file is required.

Punishment and Hardcore remain optional. RankStatus will continue to load and `/pilot status` will continue to work
when either plugin is absent or temporarily unavailable.

## Installation

1. Copy `rankstatus.zip` into the DCSServerBot `plugins` directory.
2. Add `rankstatus` to `opt_plugins` in `config/main.yaml`:

```yaml
opt_plugins:
  - logbook
  - rankstatus
```

3. Restart DCSServerBot. The archive is unpacked automatically and `/pilot status` is registered with Discord.

When updating an existing RankStatus installation, fully stop the bot, remove the old `plugins/rankstatus` directory,
rename the downloaded archive to `rankstatus.zip`, place it in `plugins`, and start the bot again. This prevents a web
browser's duplicate filename or an old extracted copy from leaving the previous command active.

Players need the configured DCS Discord role. The selected user must have a linked DCS account.

## Command

| Command      | Role | Description |
|--------------|------|-------------|
| /pilot status [user] | DCS | Shows a linked pilot's campaign status, combat record, profile, awards, and ribbon rack. |

## Data and privacy

- Rank, credits, flight time, K/D, friendly kills, and preferred airframes use the selected active campaign.
- Preferred airframes are the three aircraft with the most completed campaign flight time.
- Squadron membership, current penalty points, and Hardcore mode represent the pilot's current state.
- Awards are Logbook totals.
- Pilots can see their own Punishment and Hardcore information. Only members with the DCS Admin role can see those
  private fields for another pilot, matching the source commands' permissions.
- Campaign credits lost to friendly kills are calculated from actual CreditSystem deductions whose stored punishment
  remark matches the configured `kill` or `collision_kill` reason. Older entries may not match if those reason strings
  were changed after the deduction was recorded.
