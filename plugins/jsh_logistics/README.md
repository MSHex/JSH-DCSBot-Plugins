# JSH Logistics 1.0

Server credits for logistics pilots, priced **per instance**:

1. **Every Foothold delivery** they bank by landing: zone captures, upgrades and
   resupply, by cargo or by troops; warehouse deliveries; and CTLD builds.
2. **Time flown in a logistics aircraft**, for example 1 credit per minute
   airborne in a Chinook, paid on landing.

Foothold already pays for these, but in its own in-mission currency: coalition
funds and Foothold rank credits. None of that reaches DCSServerBot. This plugin
pays **server credits** as well, so logistics work counts towards the bot's rank
ladder, the shop, `/logistics status` and the monthly board, the same way kills do.

## How Foothold rewards logistics (checked against Foothold's source)

Every logistics job in Foothold ends the same way. The script credits the
pilot's **pending** reward (`bc:addContribution`) and counts a **temp stat**
under the job's name (`bc:addTempStat(player, "<stat>", 1)`). When the pilot
lands at a friendly base, Foothold **banks** both: it pays the coalition,
announces the landing, and commits the temp stats
(`BattleCommander:commitTempStats`). If the pilot dies first, they're lost.

| Foothold stat | What the pilot did | Where |
| --- | --- | --- |
| `Zone capture` | Landed supplies (helo cargo, C-130 cargo static, or CTLD crate) in a **neutral** zone and took it | zoneCommanderv2, Foothold CTLD |
| `Zone upgrade` | Landed supplies that **upgraded** a friendly zone | zoneCommanderv2 |
| `Zone supply delivery` | Landed supplies that **restocked** a friendly zone, or joined a capture already under way | zoneCommanderv2, Foothold CTLD |
| `Zone capture (troops)` | Dropped CTLD troops that took a neutral zone | Foothold CTLD |
| `Zone supply delivery (troops)` | Dropped CTLD troops into a friendly zone | Foothold CTLD |
| `Warehouse delivery` | Delivered weapons or fuel-tank stock to an airbase warehouse | Foothold CTLD |
| `Built FARP`, `Built HAWK`, `Built NASAMS`, `Built IRIS-T`, `Built Pantsir S1`, `Built SA-3`, `Built SA-8`, `Built Tor M2` | Built a CTLD crate set | Foothold CTLD |
| `Upgraded HAWK`, `Upgraded NASAMS`, `Upgraded IRIS-T` | Upgraded a built SAM site with crates | Foothold CTLD |

jsh_logistics wraps that one banking method on the live `bc`, from injected Lua.
**Foothold's source is not touched.** So:

- a delivery is paid **once**, at the moment Foothold banks it, and only if
  Foothold banks it: a pilot shot down with undelivered rewards gets nothing
  from either
- **multicrew** works: Foothold banks each crew member separately, and each one
  is paid
- stats that aren't logistics are ignored, including `Pilot Rescue`
  (`jsh_csar` pays for rescues), kills, achievements and flight time

## Delivery prices

```yaml
DEFAULT:
  deliveries:
    Zone capture: 50
    Zone upgrade: 30
    Zone supply delivery: 25
    Zone capture (troops): 30
    Zone supply delivery (troops): 15
    Warehouse delivery: 25
    Built FARP: 40
    Built *: 30            # any other CTLD build
    Upgraded *: 20
```

- Keys are Foothold's own stat names.
- `*` is a wildcard. An exact name beats a wildcard, and between wildcards the
  more specific (longer) one wins. So `Built FARP: 40` and `Built *: 30` pay 40
  for a FARP and 30 for anything else built.
- **An instance lists only what it changes.** Everything else keeps the DEFAULT
  price.
- A price of `0` switches that delivery off.
- If a Foothold update adds a new logistics stat, give it a price and it's paid.
  No code change is needed.

## Airtime

```yaml
DEFAULT:
  airtime:
    enabled: true
    minutes: 1           # every 1 minute airborne...
    credits: 1           # ...pays 1 credit
    aircraft: [C-130J-30, Hercules, CH-47Fbl1, UH-1H, Mi-8MT]
    sample_seconds: 10
```

- Only **airborne** time counts. Sitting on a pad or taxiing doesn't.
- The mission checks every `sample_seconds` who is airborne in a listed type.
  When an aircraft that was airborne is on the ground again and intact, that's
  a landing, and the time flown is paid. This works for a helicopter landing in
  a field as well as at a base, because it doesn't rely on DCS's landing event.
- Losing the aircraft, or leaving the slot while airborne, **forfeits the
  unlanded time**. This matches how Foothold and `jsh_credits` treat pending
  rewards.
- Leftover seconds carry over to the next landing instead of being rounded
  away. Ten 50-second hops at 1 credit per minute pay 8.
- `aircraft` uses DCS type names, case-insensitive. The default list is the
  one awardautomation's Airman's Medal uses.
- The pilot is paid. Multicrew seats other than the pilot aren't reported by
  DCS as the unit's player.

## Per instance

Sections key on the **instance name** from `nodes.yaml`, never the server name.
Instance values are deep-merged over DEFAULT:

```yaml
your-instance-name:
  airtime:
    credits: 2                         # double airtime here
  deliveries:
    Zone capture (troops): 0           # no troop rewards here
    Zone supply delivery (troops): 0
```

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/logistics status [member] [days]` | DCS | Deliveries by type, airtime by aircraft, and credits earned (default: last 30 days) |

## Records

Every payout is recorded in `jsh_logistics_events`, one row per delivery type
per landing or per airtime landing. Airtime is recorded even when it earned
nothing yet, so hours stay complete. Later features, such as a "most
deliveries" board, can be built from this table.

## Installation

1. Copy this folder to `<bot>/plugins/jsh_logistics/` and
   `config/jsh_logistics.yaml` to `<bot>/config/plugins/`, on **every node**.
2. Add `jsh_logistics` to `opt_plugins` in `main.yaml`.
3. Restart the bot. The mission side is loaded into running missions on that
   restart, so no mission restart is needed. On a mission with Foothold, the
   DCS log shows `[jsh_logistics] attached to BattleCommander`.

Without Foothold, delivery rewards are simply off and airtime still works.
