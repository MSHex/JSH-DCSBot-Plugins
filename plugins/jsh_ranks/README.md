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
| `/ranks sync` | Re-apply the Discord role for every granted rank |

## Granting a rank by hand

The ladder in `creditsystem.yaml` is earned — credits and playtime decide it —
so there was no way to give someone a rank that should not depend on either.
Moderator and admin ranks are the case that matters: they are a position, not
an achievement.

### Granted-only ranks

Define them under `granted_ranks` in `jsh_ranks.yaml`, and **leave them
commented out of `creditsystem.yaml`'s `achievements`**:

```yaml
granted_ranks:
  General (O-10):
    role: General (O-10)        # the Discord role; a name or a numeric ID
    level: 19                   # the Foothold rank number
    badge:                      # what /pilot status shows as its thumbnail
      name: General (O-10)
      img: https://upload.wikimedia.org/.../US_Air-force_O10_class_b.svg.png
  Founder: 20                   # shorthand: role named after the rank, no badge
```

`badge` is the same shape `creditsystem.yaml` uses, so a rank you commented out
of `achievements` keeps its artwork by pasting the block across unchanged.
`name` defaults to the rank, so in practice only `img` is needed.

Being absent from `achievements` is precisely what makes these safe for this
plugin to own. CreditSystem's `process_achievements` walks
`config['achievements']` and only ever calls `manage_role` with a role named by
one of those entries — so a role that is not on that list is never added or
removed by it. There is nothing to fight over. Put one back under
`achievements` and CreditSystem will start stripping it from anyone who has not
earned it.

`/ranks grant` assigns the Discord role as well as the in-mission rank, and
`/ranks revoke` removes it. The bot needs **Manage Roles**, and its own role
must sit **above** these roles in Server Settings → Roles; the command tells
you when that is what went wrong rather than failing silently.

Discord colours a member by their highest-positioned role, so put these above
the ladder roles if the moderator rank should be the one that shows.

### Ladder ranks

A rung of the earned ladder can also be granted, which pins the in-mission rank
regardless of credits or hours. Its **Discord role is deliberately left alone**
— that role belongs to CreditSystem, which recomputes it from credits and
playtime and would strip anything added here on its next pass, producing a role
that flickers. For a moderator rank, use `granted_ranks` instead.

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

`rank` must be a granted-only rank or a name from the ladder, and the command
refuses anything else — an unrecognised name would fall through `rank_map` and
quietly leave the pilot on `default_level` instead of granting anything. For a
ladder rank it also warns when `rank_map` has no line for it on a server where
the plugin is enabled, which is the same failure one step later. A granted-only
rank carries its own `level` and needs no `rank_map` entry.

`/ranks sync` re-applies every granted rank's Discord role. Worth running when
roles drift in ways a grant cannot see: someone leaves and rejoins and loses
every role, a role is deleted and recreated, a grant was written by Discord ID
before that person had linked, or the bot lacked permission at the time and the
rank was recorded without the role.

A grant **wins outright** over the earned rank, so it pins a rank down as well
as up — and it freezes that pilot's progression until it is revoked.
`/ranks show` marks a granted rank for that reason. The grant takes effect on
the pilot's next slot change, or immediately with `/ranks refresh`.

Grants live in `jsh_rank_grants` and are not tied to a campaign or a server, so
they survive a campaign reset — unlike the credits and playtime the earned
ladder is read from.

## `cbuy:` — buying from a commander slot

Foothold's own `buy:` marker **cannot** work from a Tactical Commander or Game
Master slot, and that is a DCS limitation rather than a Foothold bug. DCS
attaches no initiator to a mark placed from a slot with no controllable unit,
so `zoneCommanderv2.lua` leaves the rank at `0` and refuses every gated item.
Mission-side Lua cannot identify such a player either —
`coalition.getPlayers()` returns units, and a commander has none.

The bot can, because it knows who is connected and in which slot. It pushes the
commanding level per coalition on every refresh, and the injected mission script
authorises `cbuy:` against that before calling Foothold's own `buyShopItem`,
which has no rank check of its own.

From a commander slot, place a marker reading:

```
cbuy: zhawk
```

using the same item ids as Foothold's `buy:`. The marker is removed once
handled and the result is announced to the coalition.

**Nothing in Foothold is modified**, so a Foothold update cannot undo it. The
cost is that this leans on Foothold's internals — `_G.bc`,
`bc.shops[coalition][id].reqRank` and the `buyShopItem` signature. If a future
Foothold renames those, `cbuy` stops working and says so in `dcs.log`, which is
a better failure than a patched file silently reverting on update and nobody
noticing until someone cannot buy a Patriot.

`cbuy:` rather than `buy:` is deliberate: Foothold's handler still fires on
`buy:` and would print *"insufficient rank"* beside a successful purchase, and
its handler cannot be stopped without editing it. Foothold matches nothing on
`cbuy:`, so only this code answers.

The **highest-ranked** commander on a coalition sets the level. Unlike
attributing an intel report, two commanders is not a reason to refuse — nobody's
name goes on a purchase, and refusing while two admins are on the map is the
failure this exists to remove.

Requires `RankingSystem = true` in `Foothold_Config.lua`. With it `false`,
Foothold applies no gates at all and `cbuy` simply buys.

## What this does not change

Foothold's own rank credits keep accruing underneath, and
`RankLoseWhenKilled` still subtracts them on death. They are simply no longer
what the gates consult. If you want deaths to cost campaign credits instead,
that belongs in `jsh_credits`, not here.

**A granted LADDER rank does not assign the Discord role.** CreditSystem owns
the roles named in `achievements` and recomputes them from credits and
playtime, so a role granted here would be stripped on its next pass. Granting
a ladder rank governs the rank inside the mission and what `/ranks show`
reports; its Discord role stays with CreditSystem.

Granted-**only** ranks are different: they are absent from `achievements`, so
CreditSystem never touches them and this plugin assigns their roles itself.
That is the whole reason to define a moderator rank under `granted_ranks`
rather than as a ladder rung nobody can reach.

## `/pilot status`

`rankstatus` 1.5 and later shows a granted rank as the pilot's current rank,
with its `badge.img` as the embed thumbnail:

```
🎖️ Current Rank          💳 Available Credits     ⏱️ Campaign Flight Time
General (O-10)           17,577                   313h 2m

📈 Promotion Progress
🏆 Admin — outside the campaign ladder.
```

**A granted rank replaces the earned one outright.** It is a position rather
than a milestone, so there is nothing left to progress toward: no target rank,
no credits or hours remaining, and the earned rung underneath is not shown at
all. It reads the way the top of the ladder reads.

### Wording

Each granted rank carries a short `status:` label — `Admin`, `Staff`,
`Moderator` — and one format string wraps them all:

```yaml
granted_status_format: '🏆 {status} — outside the campaign ladder.'
default_granted_status: Granted rank
```

`{status}` is the rank's own label and `{rank}` its name; both placeholders are
optional, and a format string with neither is used literally. Changing the
phrasing for every rank is a one-line edit; changing it for one rank is its
`status:`. A rank with no `status:` falls back to `default_granted_status`.

A typo in the format string cannot take down `/pilot status`: an unrecognised
placeholder falls back to the bare label.

Credits and flight time are still displayed, because they are facts about the
pilot either way — and they keep accruing, so revoking a grant returns the
pilot to the correct earned rank with no catching up to do.

A granted rank with no `badge.img` falls back to the earned rank's artwork
rather than leaving the embed without a thumbnail.

The lookup lives in `jsh_ranks/grants.py`, which imports nothing from
DCSServerBot, discord or any other plugin. `rankstatus` imports it lazily, at
call time: at module level it would make `rankstatus` fail to load wherever
`jsh_ranks` is absent, and it would tie the two packages into an import cycle,
since `jsh_ranks` already imports `plugins.rankstatus.ranks`. Without
`jsh_ranks` installed, `rankstatus` simply reports no grant and carries on.
