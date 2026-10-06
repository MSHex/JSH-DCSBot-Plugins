
## 1.7 session-settlement hotfix

This build intentionally remains version **1.7**. It adds no schema migration.

- Caches `(server, player_id) -> UCID` so spectator/disconnect events that omit UCID can still be settled.
- Uses cached UCID before `server.get_player(id=...)` during disconnect settlement.
- Pauses earning segments on `slot=-1` / spectator transitions even when the event omits UCID.
- Preserves illegal-airborne-reslot detection across those slot transitions.
- Adds explicit settlement audit logging, including elapsed seconds, completed blocks, losses, penalty, payout, and balances.
- Logs zero-payout anomalies and unresolved disconnect identities instead of silently dropping them.
- Rebuilds the identity cache when a DCS server registers and clears it on simulation stop.

# Hardcore Economy v2.0 for DCSServerBot

Persistent Dynamic Campaign flight rewards layered on top of DCSServerBot's
native CreditSystem.

## Rules implemented

- Normal: 25 credits per complete 15-minute eligible cockpit block.
- Hardcore: 37.5 credits per complete 15-minute block.
- Hardcore buy-in: 500 credits, non-refundable.
- Normal loss penalties:
  - 0 losses: 0% deduction
  - 1 loss: 20% deduction
  - 2 losses: 50% deduction
  - 3+ losses: 90% deduction
- First Hardcore loss:
  - immediately revokes persistent Hardcore status;
  - reprices the whole current session at the Normal block rate;
  - normal death/loss penalties then apply.
- Crash, pilot death, eject and illegal airborne reslot count as loss incidents.
- Closely-spaced crash/death/eject events are deduplicated.
- Settlement occurs on disconnect/player stop and on simulation stop.
- Receipts are sent by Discord DM when possible.
- Fractional 0.5 credits are persisted and carried into future payouts.

## Install

Copy:

    plugins/hardcore/

into your DCSServerBot `plugins` directory and copy:

    config/plugins/hardcore.yaml

into your bot configuration directory.

Add `hardcore` to the enabled plugins in `config/main.yaml` if your installation
does not load it automatically.

The native `creditsystem` plugin must also be enabled.

Restart the bot or use the bot's plugin management commands to install/reload it.
On first install DCSServerBot will create the plugin tables from `db/tables.sql`.

## Server configuration

Exactly one server should have:

    enabled: true

in `hardcore.yaml`. The name must match the DCSServerBot server name.

## Commands

- `/hardcore status`
- `/hardcore join`
- `/hardcore leave`
- `/hardcore admin_check`
- `/hardcore admin_set`

## Important behavior

### Eligible time

A connection opens an economy session, but time is earned only while the player
occupies an eligible non-spectator/non-CA aircraft slot.

### Hardcore join/leave

A player cannot change mode while an economy session is open. This avoids
mid-session rate manipulation.

### Native credits

This plugin does not invoke `/credits donate`. It updates the same campaign-aware
`credits` and `credits_log` records used by CreditSystem.

### Fractional credits

CreditSystem uses INTEGER points. Hardcore's 37.5/block is represented internally
as half-credit units. A remaining 0.5 is stored in `hardcore_wallet_carry` and
added to the next payout.

## Recommended first test

Use a temporary test campaign/server and temporarily set:

    block_minutes: 1
    normal_reward: 25
    hardcore_reward: 37.5

Then verify:

1. spectator time earns nothing;
2. 1 complete minute in Normal pays 25;
3. 1 complete minute Hardcore yields 37 with 0.5 carry;
4. next 1-minute Hardcore session yields 38;
5. one Normal loss pays 80% of gross;
6. one Hardcore loss revokes Hardcore and reprices the session to Normal;
7. takeoff -> slot change before landing is counted as an illegal reslot;
8. `/credits info` shows the `flight economy` and `hardcore buy-in` audit entries.

Restore `block_minutes: 15` after testing.
