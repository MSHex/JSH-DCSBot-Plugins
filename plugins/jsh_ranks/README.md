# Plugin "JSH Ranks"

Makes a Foothold mission enforce the Discord rank ladder from
`creditsystem.yaml` instead of its own in-mission rank credits. A pilot who is
a Major on Discord is a Major in the shop.

## How it works

Foothold funnels every rank gate through one function,
`BattleCommander:getPlayerRank(playerName)` — shop items, tanker control,
carrier navigation and the F10 menus all call it, directly or through
`_getGroupRank`. The mission-side script wraps that function and answers from a
table the bot pushes in. Everything downstream follows, with no other Foothold
change.

A pilot the bot has not sent falls through to Foothold's own answer, so an
unknown player is never worse off than before.

The ladder itself comes from `rankstatus`, which is what `/pilot status` uses.
There is deliberately no second implementation here: a parallel calculation
would drift from the Discord roles the moment anyone edited the achievements.

## Requirements

- `rankstatus` installed (the rank ladder is imported from it)
- CreditSystem, with `achievements:` configured — that list *is* the ladder
- A running campaign; credits and playtime are both campaign-scoped
- **`RankingSystem = true` in `Foothold_Config.lua`.** With it false Foothold
  applies no rank gates at all and these levels govern nothing.

## Installation

1. Copy this folder into `<bot>/plugins/jsh_ranks/`.
2. Copy `config/jsh_ranks.yaml` into `<bot>/config/plugins/`.
3. Add `jsh_ranks` to `opt_plugins` in `config/main.yaml`.
4. Set `enabled: true` under the instance name of each Foothold server.
5. Restart the bot, then restart the DCS servers.

`dcs.log` should show `[jsh_ranks] loaded`, then `attached to BattleCommander`
and `received N rank(s)`.

## Configuration

| Key | Meaning |
| --- | --- |
| `enabled` | Off by default. Turn it on per instance — only Foothold servers want it. |
| `refresh_minutes` | How often to re-send ranks while people fly. Credits and playtime both move during a session, so a pilot can be promoted mid-flight. 0 updates only on connect and slot change. |
| `default_level` | Used for a pilot with no rank yet, and for any rank missing from `rank_map`. |
| `rank_map` | The Discord ladder has 16 rungs; Foothold's `ShopRankRequirements` goes to 7. This maps one onto the other. Keys are the achievement's `role`, exactly as written in `creditsystem.yaml`. |
| `level_names` | Optional. Names shown where Foothold would print its own rank name. Several Discord ranks share a level, so these name the band, not the pilot's exact rank. |

An unmapped rank logs a warning and falls back to `default_level` rather than
locking anyone out of the shop. If you edit the achievements list, check
`rank_map` still covers every `role`.

## Commands (DCS Admin / GameMaster)

| Command | Description |
| --- | --- |
| `/ranks show server` | The rank level each player on that server currently has |
| `/ranks refresh server` | Re-inject the hook and re-send every rank now |

## What this does not change

Foothold's own rank credits keep accruing underneath, and
`RankLoseWhenKilled` still subtracts them on death. They are simply no longer
what the gates consult. If you want deaths to cost campaign credits instead,
that belongs in `jsh_credits`, not here.
