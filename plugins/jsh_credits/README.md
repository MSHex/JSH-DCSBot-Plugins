# Plugin "JSH Credits"

Holds the credits a player earns for **kills** and **air-to-air refuelling**
during a sortie and pays them out only when they land at a friendly airbase or
FARP. A sortie that ends badly --
crash, eject, pilot death, self-kill, disconnect or slot change -- pays out only
`death_payout` of what was pending (a quarter by default); the rest is gone.
A mission ending while the player is airborne drops the bucket entirely.

Kills and refuelling are all this plugin credits. Mission awards that arrive through
`addUserPoints` -- CSAR rescues, logistics rewards -- are credited by CreditSystem
and labelled by the plugin that earned them, so each keeps its own `credits_log`
event. CreditSystem remains the ledger: balances, `credits_log`, achievements,
Discord roles, `/credits info` and `-credits` all keep working unchanged.

## Why it exists

The stock combination of CreditSystem `points_on_rtb` and SlotBlocking `payback`
does not do what its name suggests:

- `points_on_rtb` is only checked in the `addUserPoints` handler. Kill points are
  credited immediately regardless, **and** copied into `deposit`, so a player who
  landed was paid for the same kills twice.
- `addUserPoints` awards (CSAR, logistics) went into `deposit` with no audit
  row, and were silently discarded whenever the deposit was cleared by a death or
  slot change. They were never credited at all.

Both failures came from two code paths writing the same credits through a shared
attribute. This plugin has one writer and its own private pending store.

## Requirements

| Setting | File | Value |
| --- | --- | --- |
| `payback` | `slotblocking.yaml` | `false` (or don't load SlotBlocking at all) |
| `points_on_rtb` | `creditsystem.yaml` | `false` |
| `points_per_kill` | `creditsystem.yaml` | **removed** |
| `multiplier` | `creditsystem.yaml` | `1.0` |

Removing `points_per_kill` makes CreditSystem's kill handler a no-op (`ppk` is 0),
so this plugin is the only source of kill points. `points_on_rtb: false` lets
CreditSystem credit mission awards as they arrive, which is what CSAR and
logistics rewards need -- holding those to the next landing destroys them, since
they are earned at a friendly field already and the landing has happened by the
time the award arrives. This plugin zeroes the `deposit` CreditSystem writes
alongside each award, so players never see a phantom "on deposit" figure.

A campaign must be running on the server (see the GameMaster plugin), or no
credits are awarded by anything.

## Installation

1. Copy the `jsh_credits` folder into `plugins/`.
2. Copy `jsh_credits.yaml` into `config/plugins/`.
3. Add `jsh_credits` to `opt_plugins` in `config/main.yaml`.
4. Apply the config changes in the table above.
5. Restart the bot.

On startup you should see `=> Plugin Jsh_credits installed.`, a line reading the
config, `Registering EventListener CreditsEventListener`, and `=> Credits loaded.`

## Configuration

`DEFAULT` applies to every server; a section named exactly as the server appears
in `servers.yaml` overrides any key for that instance. `enabled: false` in a
server section opts that server out.

`multiplier` is applied once, at payout.

`points_per_kill` is evaluated top to bottom and the first match wins, so order
entries from most specific to least. A `default` entry matches everything and must
come last. Supported keys: `category`, `type` (`Player` or `AI`), `unit_type`,
`points`, `default`.

## Air-to-air refuelling

DCS names the **receiving** aircraft as the initiator of `S_EVENT_REFUELING` and
`S_EVENT_REFUELING_STOP`, so the mission script samples the fuel state at connect
and subtracts at disconnect. `getFuel()` is a fraction of internal capacity and
`getDesc().fuelMassMax` is that capacity in kilograms, so the difference converts
straight to pounds.

Two known approximations, neither of which matters for an economy: fuel in
external tanks sits outside `fuelMassMax`, and the aircraft burns while it
drinks. Both make the figure conservative.

**The pilot taking the fuel is paid, not the tanker.** Rewarding a human-crewed
tanker for fuel given away would be a separate feature.

Refuelling is **held like a kill** — collected on landing, reduced to
`death_payout` if the sortie ends badly. That is the point of this plugin, and it
also means tanking has to be flown home to be worth anything.

### Setting the rate

Fuel is counted in thousands of pounds, so a rate that reads small per 100 lbs is
very large per sortie. Against the shipped kill values — 20 for a player
aircraft, 15 for an AI one, 8 for a helicopter:

| Rate | F-16 top-up (~7,000 lbs) | Worth roughly |
| --- | --- | --- |
| 2 per 1000 lbs *(default)* | 14 credits | one AI aircraft |
| 5 per 1000 lbs | 35 credits | two player aircraft |
| 1 per 100 lbs | 70 credits | three player aircraft |
| 5 per 100 lbs | 350 credits | seventeen player aircraft |

Tanking is repeatable and carries no risk, which kills do.

`max_lbs_per_sortie` bounds the obvious exploit: fuel costs the pilot nothing, so
without it a sortie can orbit a tanker, burn the fuel off in afterburner and tank
again indefinitely. The counter rides in the pending bucket, so it resets on
exactly the same boundaries — landing, crash, eject, death, slot change,
disconnect.

### Foothold pays its own

Foothold has a refuelling reward of its own (`RefuelReward`, 2 credits per
100 lbs by default) which pays into its **contribution economy** — a different
ledger from campaign credits, and one that does not feed the Discord rank ladder.
The two therefore do not double-pay. If you would rather only one ran, set
`enabled: false` under `refuelling:` for the Foothold instances, or set
Foothold's `RefuelReward` to 0.

## Landing validation

`lua/mission.lua` is injected into the running mission at mission load and on
server registration, so there is nothing to add to your `.miz` files. It hooks
`S_EVENT_LAND` and reports to the bot only when:

- the initiator is a human player,
- `event.place` exists (so an off-field landing does not count), and
- the place's coalition matches the pilot's.

Ownership is read live, so a base captured mid-campaign is judged by who holds it
at that moment. Ships count as landing places, so carrier traps pay out.

## In-game commands

| Command | Description |
| --- | --- |
| `-pending` | Shows your balance plus anything not yet landed with |

## Events consumed

| Event | Effect |
| --- | --- |
| `kill` | Hold points for killer and crew (no AI killers, self-kills or team-kills) |
| `jshCreditsRefuel` | Hold points for fuel taken from a tanker |
| `addUserPoints` | Clear the unused `deposit` CreditSystem writes; the award itself is not ours |
| `jshCreditsLanding` | Pay out the pending bucket |
| `crash`, `eject`, `pilot_death`, `self_kill` | Pay `death_payout` of the bucket, lose the rest |
| `disconnect`, `onPlayerChangeSlot` | Pay `death_payout` of the bucket, lose the rest |
| `mission_end`, `onMissionLoadEnd` | Drop all buckets on that server |

## Audit trail

Two records are kept, for two different jobs.

### credits_log (CreditSystem's)

One `rtb` row per landing, with a remark naming what the points were for:

```
RTB Al-Asad Airbase: kills (86: 5x SA-18 Igla manpad, 3x tt_ZU-23, 17x Infantry AK, +8 more)
```

`remark_detail` sets how many unit types are named before the `+N more` tail
(0 disables the detail entirely). `remark_max_length` truncates the whole remark
so Discord embeds stay readable.

A sortie that ends badly with `death_payout` above 0 writes its own row, under an
event named after how it ended:

| Ending | `credits_log` event |
| --- | --- |
| eject | `ejected` |
| crash | `crashed` |
| pilot death, self-kill | `died` |
| disconnect, slot change | `salvaged` |

The remark names what it came from, e.g.
`kept 22 of 86 pending after eject (kills (86: 5x SA-18 Igla manpad, +8 more))`.

Nothing here ever deducts from a campaign balance -- this plugin only ever adds.
The only thing at stake is the pending bucket from that sortie.

Nothing is logged as paid until the balance has actually changed.

### jsh_credits_events (this plugin's)

One row per earning event, written the moment it happens, with a `paid` flag that
flips when the player lands. Rows left `false` are credits earned and then lost to
a crash, eject, slot change or disconnect -- something no other record shows.

Useful queries:

```sql
-- what a player lost to deaths this week
SELECT sum(points) FROM jsh_credits_events
 WHERE player_ucid = '...' AND paid = FALSE AND time > now() - interval '7 days';

-- earned vs collected, per player
SELECT player_ucid,
       sum(points) FILTER (WHERE paid) AS collected,
       sum(points) FILTER (WHERE NOT paid) AS lost
  FROM jsh_credits_events GROUP BY player_ucid ORDER BY lost DESC;

-- reconcile against the ledger: these two should agree
SELECT sum(points) FROM jsh_credits_events WHERE player_ucid = '...' AND paid;
SELECT sum(new_points - old_points) FROM credits_log
 WHERE player_ucid = '...' AND event = 'rtb';

-- partial payouts are deliberately NOT in that reconciliation: their ledger
-- rows stay unpaid, because they were earned and never landed with
SELECT sum(new_points - old_points) FROM credits_log
 WHERE player_ucid = '...'
   AND event IN ('ejected', 'crashed', 'died', 'salvaged');
```

The ledger never blocks crediting: if a write fails it is logged as an error and
the payout goes ahead regardless.
