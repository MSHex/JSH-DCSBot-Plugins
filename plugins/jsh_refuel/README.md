# Plugin "JSH Refuel"

Pays campaign credits for fuel taken from a tanker, on every server — including
the ones with no Foothold.

## How it works

DCS reports `S_EVENT_REFUELING` when an aircraft connects to a tanker and
`S_EVENT_REFUELING_STOP` when it comes off, naming the **receiving** aircraft as
the initiator. The mission script records the fuel state at the first and
subtracts at the second.

`getFuel()` is a fraction of internal capacity and `getDesc().fuelMassMax` is
that capacity in kilograms, so the change between the two readings converts
straight to pounds. Two consequences worth knowing:

- Fuel in **external tanks** sits outside `fuelMassMax`, so a drop-tank fill
  reads low.
- The aircraft **burns while it drinks**, so the figure slightly understates the
  real transfer.

Neither matters for an economy — the number only has to be consistent.

The mission side measures and nothing else. Every policy decision — the rate,
the minimum worth paying, the per-sortie cap — is made by the bot from
`jsh_refuel.yaml`, so changing the economy needs no DCS restart.

**The pilot taking the fuel is paid, not the tanker.** Rewarding a human-crewed
tanker for fuel given away would be a separate feature.

## Requirements

- CreditSystem, with a campaign running on the server
- No Foothold dependency: this works on a dynamic campaign server too

## Installation

1. Copy this folder into `<bot>/plugins/jsh_refuel/`.
2. Copy `config/jsh_refuel.yaml` into `<bot>/config/plugins/`.
3. Add `jsh_refuel` to `opt_plugins` in `config/main.yaml`.
4. Restart the bot, then restart the DCS servers.

On a multi-node setup, do this on **every** node — the bot only copies a
plugin's Lua to the servers of nodes that list it.

`dcs.log` should show `[jsh_refuel] loaded`, then a line per transfer:
`Hexe 2-2 | Ajax took 6240 lbs`.

## Configuration

| Key | Meaning |
| --- | --- |
| `enabled` | `false` turns the plugin off on that server: nothing measured, nothing paid. |
| `block_lbs` / `credits_per_block` | The rate, paid in whole blocks. A partial block is never rounded up. |
| `minimum_lbs` | Transfers below this are recorded but not paid, so brushing the basket earns nothing. |
| `max_lbs_per_sortie` | Most pounds that can be **paid for** in one sortie. `0` removes the cap. |
| `announce_in_game` | Tells the pilot what they earned when the boom comes out. |
| `credits_log_event` | The `credits_log` event name. Must stay distinct from every other credit writer. |

### Setting the rate

Fuel is measured in thousands of pounds, so a rate that looks small per 100 lbs
is very large per sortie. Against the `jsh_credits` defaults — 20 for a player
aircraft, 15 for an AI one, 8 for a helicopter:

| Rate | F-16 top-up (~7,000 lbs) | Worth roughly |
| --- | --- | --- |
| 2 per 1000 lbs *(shipped default)* | 14 credits | one AI aircraft |
| 5 per 1000 lbs | 35 credits | two player aircraft |
| 1 per 100 lbs | 70 credits | three player aircraft |
| 5 per 100 lbs | 350 credits | seventeen player aircraft |

Tanking is repeatable and risk-free, which kills are not. The default is
deliberately modest; raise it knowing what a full F-15E (~13,000 lbs) is then
worth.

### Why there is a sortie cap

Fuel costs the pilot nothing, so without a cap a bored pilot can orbit a tanker,
burn it off in afterburner and tank again, indefinitely. `max_lbs_per_sortie`
bounds what one airframe can earn. The counter is kept by the bot and resets on
slot change, disconnect, crash, eject, pilot death and self-kill — a new
airframe is a new sortie, however the last one ended.

## Relationship to Foothold

Foothold pays its own refuelling reward (`RefuelReward`, default 2 credits per
100 lbs) into **its own** contribution economy, which is a different ledger from
campaign credits and does not feed the Discord rank ladder. The two therefore do
not double-pay.

If you would rather only one of them ran, either set `enabled: false` here for
the Foothold instances, or set Foothold's `RefuelReward` to 0.

## Commands (DCS Admin / GameMaster)

| Command | Description |
| --- | --- |
| `/refuel stats [user]` | Refuellings, fuel taken and credits earned |
| `/refuel top` | Who has taken the most fuel |

## Database

- `jsh_refuel_log` — one row per completed refuelling, paid or not. Rows with
  `points = 0` are the ones below `minimum_lbs` or past the sortie cap; keeping
  them makes the totals a true record of fuel taken rather than fuel that
  happened to pay.
- `jsh_refuel_totals` — lifetime totals per player, across every server.

## What this does not do

Nothing is held until landing. Unlike kills in `jsh_credits`, fuel is paid as
soon as the boom comes out — a pilot who tanks and then crashes keeps the
credits, the same way CSAR rewards work.
