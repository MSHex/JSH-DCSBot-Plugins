# Plugin "Hardcore" (Hardcore Economy 2.1)

Hardcore mode and the flight economy: pays credits per completed block of time,
with deductions for aircraft losses, and revokes Hardcore membership when a
sortie ends badly outside friendly ground.

Written by **Joker**. 2.1 adds a separate support clock, a telemetry-driven
protection system, a first-ejection allowance, and mission-side Lua.

## What changed in 2.1

| | 2.0 | 2.1 |
| --- | --- | --- |
| Time tracked | one clock | **two** — aircraft and support, independently |
| Support slots | not rewarded | paid at the Normal rate, never the Hardcore rate |
| Protection | bot-side inference | **mission telemetry** — live coalition, airfield/FARP/carrier geometry |
| First ejection | revoked membership | **allowance**: retained once per membership |
| Duplicate incidents | possible double-charge | event keys and a 3-second merge window |
| Mission Lua | none | `lua/mission.lua`, `commands.lua`, `callbacks.lua` |

Historical balances, losses and revoked memberships are **not** reclassified or
refunded.

## Rewards

Aircraft and support time accrue in **separate clocks**, keeping partial time
through role switches within a session. Each category pays only complete blocks
(15 minutes by default), so 7.5 minutes of aircraft plus 7.5 of support
completes neither. Returning to a category resumes its total.

| | Rate per block |
| --- | --- |
| Normal aircraft | `normal_reward` (25) |
| Hardcore aircraft | `hardcore_reward` (37.5), while membership is retained |
| Support | `normal_reward` — **never** the Hardcore rate |

Aircraft occupancy includes startup and taxi; this is not an airborne-only
clock. Empty or dead aircraft earn nothing. Eligible support slots are
`instructor`, `artillery_commander`, `forward_observer`, `game-master` and
`gamemaster` — ordinary observer and spectator slots earn nothing.

Support payment is based on slot occupancy. It is not a measurement of active
JTAC work, radio use or AFK behaviour, and support time is not flight time.

Credits are integers in CreditSystem, so the economy is computed in **half
credits** and any remainder carries to a later session. Existing CreditSystem
maximum balances are respected, without reducing a balance already above the
maximum.

## Protection and incidents

An aircraft **grounded at a currently friendly airfield, carrier or FARP, inside
its configured boundary** is protected: death, ejection and reslot are recorded
with no deduction, no loss count, no ejection allowance spent and no revocation.
Airborne, at a neutral or enemy field, or on open terrain, there is no
protection.

Unprotected:

| Incident | Session loss count | Hardcore membership |
| --- | --- | --- |
| First ejection in a membership | +1 | Retained |
| Second separate ejection in that membership | +1 | Revoked |
| Death or crash with no associated ejection | +1 | Revoked immediately |
| Reslot while airborne or off friendly ground | +1 | Revoked immediately |

The allowance lives in `hardcore_status.ejections_used` and survives reconnects,
bot restarts, mission changes and settlements. A paid new membership resets it,
as does admin activation from inactive — setting an already-active player active
again does not.

Deductions are per session: 0 / 20 / 50 / 90% for 0 / 1 / 2 / 3+ counted losses.
A first ejection keeps the Hardcore rate and then applies the deduction;
revocation reprices that session's aircraft earnings at the Normal rate first. A
second ejection in a *different* session revokes membership but counts as only
one loss in that new session — 20%, not 50%.

Incident messages are grouped per aircraft incarnation with a 3-second window,
and ejection takes precedence within a group, so a later wreck or death message
for the same airframe cannot add a second loss. Ground impacts are judged on
pre-incident evidence: a wreck lying on the ground does not turn an airborne
crash into a protected event.

**If evidence is missing or stale the incident is logged as unverified and
nothing is charged.** That conservative default is deliberate — watch those log
lines, because they mean telemetry is not arriving.

## Friendly boundaries

Friendly means the facility's **current** coalition matches the player's;
ownership is read live, not cached at spawn.

- **Airfields** — convex footprint of the runway and parking coordinates plus
  `airfield_margin_m` (75 m), which includes aprons and taxiways.
- **FARPs** — `farp_radius_m` (150 m) around the FARP.
- **Carriers** — inside the moving ship's oriented deck footprint and elevated
  above sea level. An aircraft in the sea or airborne nearby is not protected.
- **Override** — for a land base with poor geometry, map the exact airbase name
  to a **circular** mission trigger zone in `protection_zones`. The zone never
  overrides coalition ownership or the ground-contact requirement.

## Requirements

- CreditSystem, with a campaign running on the server
- PostgreSQL (tables are created and migrated on load)
- A **full DCS restart** after install — 2.1 adds Lua hooks

## Installation

1. Copy this folder into `<bot>/plugins/hardcore/`.
2. Copy `config/hardcore.yaml` into `<bot>/config/plugins/` — or keep your
   existing one, since missing keys fall back to the defaults above.
3. Add `hardcore` to `opt_plugins` in `config/main.yaml`.
4. Restart the bot, **then restart the DCS servers**.

On a multi-node setup, do this on **every** node. The bot only copies a plugin's
`lua/` folder to the servers of nodes that list it in `opt_plugins`, and without
that Lua there is no telemetry and no protection.

The schema migrates automatically from 2.0 via `db/update_v2.0.sql`. No manual
SQL, balance reset or medal reset is needed.

### Checking it took

- `dcs.log` — `Hardcore 2.1 mission telemetry loaded`
- bot log — absence of `Hardcore telemetry missing`
- `/hardcore status` — shows separate aircraft and support time, and the
  first-ejection allowance

Missing telemetry pauses earning at the last confirmed activity. It never
authorises an automatic penalty.

### First live checks

The automated suite uses simulated DCS objects, not a real installation. Before
general use, check one pilot at a friendly land base, a FARP and a carrier, plus
one instructor/JTAC slot — and with a test account, a friendly landing, a
neutral or off-field landing, and the two-ejection policy. That is what verifies
the geometry your maps and carrier modules actually supply.

## Configuration

| Key | Meaning |
| --- | --- |
| `enabled` | Off by default; opt in per instance. |
| `buyin_fee` | Cost of a Hardcore membership. |
| `block_minutes` | Length of a payable block, per category. |
| `normal_reward` / `hardcore_reward` | Credits per block. Half-credit increments are allowed. |
| `death_penalties` | Percentage deducted from **aircraft** earnings by counted loss count. |
| `support_roles` | Slot identifiers that earn at the Normal rate. |
| `airfield_margin_m` / `farp_radius_m` | Protection boundary sizes. |
| `protection_zones` | Exact airbase name → circular trigger zone, for land bases with poor geometry. |
| `evidence_max_age_seconds` | How stale telemetry may be before an incident is unverified. |
| `incident_merge_seconds` | Window for grouping incident messages per airframe. |
| `dm_receipt` | One grouped DM at settlement. |

## Receipts and commands

A single configurable DM (`dm_receipt`) at settlement groups participation,
deaths and crashes, ejections, safe and unprotected reslots, the aircraft
deduction, support earnings, the deposit, the balance and Hardcore status. There
are no per-incident DMs.

| Command | Role |
| --- | --- |
| `/hardcore status`, `join`, `leave` | the configured DCS role |
| `/hardcore admin_check`, `admin_set` | DCS Admin |

All replies are ephemeral.

## AwardAutomation

If AwardAutomation is installed, run from the bot root with the bot's Python:

```text
python tools/patch_awardautomation.py
```

It stops support-only sessions counting toward Hardcore flight-session medal
streaks. The script backs up and changes only the known streak query, and stops
without modifying anything if your installed version differs. It is idempotent,
works with both the old and new schemas, and must be reapplied after replacing
AwardAutomation.

A support-only session neither increments nor breaks the flight streak; a
revoked Hardcore session still breaks it; a mixed session qualifies only if it
contains a completed paid aircraft block.

## Credits written

| Event | When |
| --- | --- |
| `flight economy` | Session settlement |
| `hardcore buy-in` | Membership purchase |

Unchanged from 2.0, and distinct from every other writer in this repository —
see the "who writes credits" table in the repository README.

## Tests

From the **repository root**:

```text
python -m pip install pytest lupa discord.py "psycopg[binary]"
python -m pytest -q tests
```

The Lua tests execute the real `lua/*.lua` files against mocked DCS objects. For
the database tests, also install Node, run `npm install --prefix tests`, and set
`HARDCORE_PGLITE_BRIDGE` to the absolute path of `tests/pglite_bridge.mjs`; they
use PostgreSQL compiled to WASM, never a live database, and are skipped without
that variable.

`VALIDATION.md` records the author's own validation run.

## Notes

- Normal bot death handlers no longer issue a second, independent penalty.
- Disconnect settlement pauses earning immediately and waits briefly for pending
  incident messages; mission stop flushes them first.
- Re-registration closes time at the last confirmed heartbeat, excluding
  downtime, and resumes only on fresh eligible telemetry.
- Legacy `illegal_airborne_reslot` is superseded. It is not an exemption for
  off-field reslots — all unprotected reslots follow the table above.
- Classification depends on DCS delivering its events. Late notifications beyond
  the merge window, missing birth or landing events, and unusual modules need
  live acceptance testing.

DCS API reference for airbase categories and carrier-unit access:
<https://www.digitalcombatsimulator.com/it/support/faq/1813/>
