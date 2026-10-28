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

### When ranks are sent

Three triggers, not one. The bot pushes on **connect** (`onPlayerStart`) and on
**slot change** (`onPlayerChangeSlot`), both after a two-second settle so the
player row is readable, and then every `refresh_minutes` as a backstop. The
interval exists only for the case events cannot catch: a pilot earning a
promotion mid-flight, without connecting or changing slot.

Each push **merges** into the mission's table rather than replacing it. A push
only carries the pilots the bot can see at that moment, and replacing wholesale
meant any push that omitted someone still flying — a gap in the player list, a
slot transition — dropped their rank until the next one, up to
`refresh_minutes` away. In game that is indistinguishable from the plugin having
failed: the pilot is just told their rank is insufficient. An empty push is
ignored outright, since it only means the server is empty. An entry left behind
for a pilot who has gone costs nothing: it is overwritten when they return.

Since Foothold 5.3.0 `getPlayerRank` takes and returns a second value, the
pilot's career record, which newer callers thread back in to avoid re-reading
it. The wrapper always calls through and passes that record back with our
level, so Foothold's own bookkeeping is unaffected. `getPlayerRank` never
creates a record, so calling through is a read.

The wrapper is tagged and re-checked on every push rather than latched behind a
flag. Foothold assigns its rank methods when `zoneCommanderv2.lua` loads, so in
practice ours goes on top and stays — but if anything ever replaced the
function, a flag would leave the plugin silently detached for the rest of the
mission with nothing in the log to say so.

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

## If ranks are not being enforced

The log distinguishes the three ways this fails, which otherwise all look
identical in game — pilots stuck at `default_level`, unable to buy anything
above it.

| What `dcs.log` shows | What it means |
| --- | --- |
| no `attached to BattleCommander` | The hook never went on: `BattleCommander` did not exist when the script ran. Check `[jsh_ranks] loaded` appears at all, and that the bot was restarted before DCS. |
| `received 0 rank(s)` | The bot pushed nothing — nobody was on the server, or no campaign is running. |
| `no rank pushed for <pilot>` | The hook is working and being consulted, but that pilot had no level in the push. Look in the bot log for `no rank_map entry for ...`: the ladder isn't mapping. |
| `<pilot>: level N (Foothold would have said M)` | Working as intended. If N is still too low for the shop, the problem is `rank_map` or the pilot's credits, not the hook. |

Each pilot is logged once per mission, on the first lookup, so these lines
don't accumulate on every menu refresh.

## Configuration

| Key | Meaning |
| --- | --- |
| `enabled` | Off by default. Turn it on per instance — only Foothold servers want it. |
| `refresh_minutes` | How often to re-send ranks while people fly. Credits and playtime both move during a session, so a pilot can be promoted mid-flight. 0 updates only on connect and slot change. |
| `default_level` | Used for a pilot with no rank yet, and for any rank missing from `rank_map`. |
| `rank_map` | Maps the Discord ladder onto Foothold's rank numbers. Keys are the achievement's `role`, exactly as written in `creditsystem.yaml`. See **Matching the two ladders** below. |
| `level_names` | Optional. Names shown where Foothold would print its own rank name. Several Discord ranks share a level, so these name the band, not the pilot's exact rank. |

An unmapped rank logs a warning and falls back to `default_level` rather than
locking anyone out of the shop. If you edit the achievements list, check
`rank_map` still covers every `role`.

## Matching the two ladders

**Foothold has 20 ranks**, Recruit (1) to General of the Air Force (20) — see
`rankNames` and `rankThresholds` in `zoneCommanderv2.lua`. Earlier versions of
this README said it stopped at 7; that was wrong, and a `rank_map` built on it
leaves the top items permanently unbuyable.

Twelve of Foothold's ranks share a name with a Discord rank, so the shipped
`rank_map` matches by name. That matters because Foothold prints its **own**
names in shop messages via `getRankName`, so a pilot mapped to 12 is told
"Captain" and it agrees with their Discord role. The only mismatch is Command
Chief Master Sergeant, which Foothold has no equivalent for; it shares level 9,
because giving it its own level would shift all six officer ranks up one and
make every displayed name wrong.

### Check what your map requires

Two tables set the gates, and they differ per map:

| Table | File | Range |
| --- | --- | --- |
| `ShopRankRequirements` | `Foothold Config.lua` | 1–10 on current versions |
| `CTLDPrices[...].reqRank` | `Foothold CTLD.lua` | 1–3 |

**If your highest mapped level is below a table's highest requirement, those
items can never be bought by anyone.** On Syria 5.2.0 that was `zpat` and
`zhimars` (8), `gslot` (9) and `cruisemsl` (10) — four items dead behind a
`rank_map` that stopped at 7, with no error anywhere to say so.

Worth knowing the reverse too: since nothing gates above 10, every rank above
Second Lieutenant unlocks nothing new. If you want senior officers to keep
earning access, raise some `ShopRankRequirements` values into the 11–20 range.

## Commands (DCS Admin / GameMaster)

| Command | Description |
| --- | --- |
| `/ranks show server` | The rank, level and origin for each player on that server |
| `/ranks refresh server` | Re-inject the hook and re-send every rank now |
| `/ranks grant user rank [reason]` | Assign a rank by hand |
| `/ranks revoke user` | Remove a manually assigned rank |
| `/ranks grants` | List every manually assigned rank |

## Granting a rank by hand

The ladder in `creditsystem.yaml` is earned — credits and playtime decide it.
A rung set beyond either can therefore never be reached by flying, which is
exactly what JSH's sixteenth rank is for. `/ranks grant` is how such a rank
gets handed out.

`user` accepts either a Discord member or a DCS player, and the autocomplete
offers both. **Whichever you pick, both identifiers are stored when both are
known**, because they fail in opposite directions:

- the **UCID** survives a Discord account being unlinked, relinked or left
  behind;
- the **Discord ID** follows the person onto a second DCS install, and can be
  written before they have ever connected — which a UCID cannot.

One row per pilot: granting again replaces the earlier grant rather than
stacking. If a pilot somehow holds both forms, the UCID one wins, being the
more specific of the two.

`rank` must be a name from the ladder, and the command refuses anything else —
an unrecognised name would fall through `rank_map` and quietly leave the pilot
on `default_level` instead of granting anything. It also warns when `rank_map`
has no line for the rank on a server where the plugin is enabled, which is the
same failure one step later.

A grant **wins outright** over the earned rank, so it pins a rank down as well
as up — and it freezes that pilot's progression until it is revoked.
`/ranks show` marks a granted rank for that reason. The grant takes effect on
the pilot's next slot change, or immediately with `/ranks refresh`.

Grants live in `jsh_rank_grants` and are not tied to a campaign or a server, so
they survive a campaign reset — unlike the credits and playtime the earned
ladder is read from.

## What this does not change

Foothold's own rank credits keep accruing underneath, and
`RankLoseWhenKilled` still subtracts them on death. They are simply no longer
what the gates consult. If you want deaths to cost campaign credits instead,
that belongs in `jsh_credits`, not here.

**A granted rank does not assign the Discord role.** CreditSystem owns role
assignment and recomputes it from credits and playtime, so a role granted here
would be stripped on its next pass. `/ranks grant` governs the rank inside the
mission and what `/ranks show` reports; the Discord role stays manual.
