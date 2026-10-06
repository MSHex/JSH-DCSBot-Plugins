# AwardAutomation 1.6

AwardAutomation is a standalone DCSServerBot 3.x plugin. Version 1.6 provides
the reusable award/credit ledger and automates seven Logbook medals from
campaign statistics, qualifications, and optional Hardcore sessions. It does
not modify CreditSystem, Logbook, Pilot Status, Hardcore, or any core bot file.

## Automated awards

| Rule | Award ID | Requirement | Credits | Repeats |
|---|---:|---|---:|---|
| Air Force Cross | 12 | Campaign K/D strictly over 50 and at least 1,000 campaign flight hours | 4,500 | No |
| Airman's Medal | 14 | Every 150 campaign hours in configured cargo aircraft | 2,500 | Yes |
| Aerial Achievement Medal | 17 | First 50 campaign A/A kills | 1,500 | No |
| Air Achievement Medal | 28 | 10 consecutive qualifying Hardcore sessions | 1,000 | No |
| Air Commendation Medal | 24 | 20 consecutive qualifying Hardcore sessions | 1,500 | No |
| Combat Action Medal | 18 | Every 500 campaign flight hours | 750 | Yes |
| Combat Readiness Medal | 26 | Hold valid AAR and IFR qualifications together | 500 | After expiry/reset cycle |

## Campaign statistical rules

Statistical rules read the UserStats `statistics` table and restrict every
measurement to the configured `campaign_id`. Each rule can use `scope: campaign`
to combine every server assigned to that campaign, or `scope: server` plus a
server/instance selector to count only one campaign server. Their state and
duplicate ledger are scope-aware, so server totals never mix with campaign totals.

- Flight time is the overlap between each sortie and the campaign period.
- A/A kills are `kills_planes + kills_helicopters`; friendly kills are not
  included by UserStats.
- K/D matches DCSServerBot's convention: total kills divided by categorized
  combat deaths. With zero deaths, K/D equals total kills.
- Air Force Cross uses a strict `K/D > 50`, not `>= 50`.
- Airman's Medal and Combat Action grant every crossed milestone. If a scan
  sees several newly crossed milestones, each missing cycle is granted once.

Cargo time is determined from exact, case-insensitive `statistics.slot` values.
The supplied list is:

```yaml
aircraft_types:
  - C-130J-30
  - Hercules
  - CH-47Fbl1
  - UH-1H
  - Mi-8MT
```

Adjust this list to the exact internal slot names used by the server. This is an
aircraft classification, not proof that a particular sortie carried cargo.

## Combat Readiness rule

- A valid AAR qualification and a valid IFR qualification must exist together.
- The first qualifying combination grants the configured Logbook award and 500
  credits in one database transaction.
- The cycle remains locked if only AAR or only IFR expires.
- The cycle resets only after there are no valid AAR qualifications and no valid
  IFR qualifications at the same scan.
- After reset, the pilot must earn both categories again.
- A unique `(pilot, rule, cycle)` ledger prevents duplicate medals and payouts.

An expired qualification is detected from `expires_at`, even if Logbook has not
deleted the expired row yet. Because the reset requires the plugin to observe
both categories invalid together, keep the default one-minute scan interval and
do not stop the bot while qualifications are being expired and reissued.

## Hardcore streak rules

When the standalone Hardcore v2.0 plugin is loaded, AwardAutomation reads its
settled `hardcore_sessions` records. Hardcore medals are always restricted to
the configured campaign ID and the single server under `hardcore.server`:

| Streak | Award ID | Award | Credits |
|---:|---:|---|---:|
| 10 sessions | 28 | Air Achievement Medal | 1,000 |
| 20 sessions | 24 | Air Commendation Medal | 1,500 |

A session counts only when all of the following are true:

- it is settled;
- it started in Hardcore mode;
- Hardcore was not revoked during the session; and
- it contains at least one completed reward block (`gross_half_units > 0`).

The streak is consecutive. A revoked Hardcore session resets it, as does a
billable Normal session. Zero-block disconnects are ignored. These medals are
one-time milestones; the automation ledger prevents repeat payouts.

For this installation, the only Hardcore server is `DCS.dcs_serverrelease`.
Sessions from other servers or campaigns cannot extend or break this streak.

Hardcore is optional. If it is absent or inaccessible, the two streak rules are
skipped while Combat Readiness and all four statistical rules continue normally.
The bot database account needs `SELECT` permission on `hardcore_sessions` for
streak evaluation.

## Installation

1. Stop DCSServerBot.
2. Put `awardautomation.zip` in the bot's `plugins` directory.
3. Add the plugin after its dependencies in `config/main.yaml`:

```yaml
opt_plugins:
  - logbook
  - awardautomation
```

`mission` and `creditsystem` must also be enabled. Start the bot; DCSServerBot
will unpack the archive and create the plugin tables automatically.

For Hardcore medals, also enable `hardcore`. Loading it before AwardAutomation
is recommended:

```yaml
opt_plugins:
  - logbook
  - hardcore
  - awardautomation
```

For customization, copy `plugins/awardautomation/config/config.yaml` to
`config/plugins/awardautomation.yaml`. Configuration stored under `config/`
is separate from the plugin and survives plugin replacement.

## Required Logbook records

Awards are selected by stable `logbook_awards.id` values. Names are retained as
informational labels and produce a warning if they disagree with Logbook, but
the ID is authoritative. The supplied configuration uses the IDs exported from
this installation:

| Rule | Award ID | Expected Logbook name |
|---|---:|---|
| Air Force Cross | 12 | Air Force Cross |
| Every 150 campaign cargo hours | 14 | Airman's Medal |
| First 50 campaign A/A kills | 17 | Aerial Achievement Medal |
| Combat Readiness | 26 | Combat Readiness Medal |
| Hardcore 10 sessions | 28 | Air Achievement Medal |
| Hardcore 20 sessions | 24 | Air Commendation Medal |
| Every 500 campaign flight hours | 18 | Combat Action Medal |

`Aerial Achievement Medal` is a separate award with ID 17 and is not the
10-session Hardcore medal. When an external v1.3 configuration has only the old
`award:` field, AwardAutomation automatically uses the built-in ID for each
known rule and logs a migration warning. When an older external configuration
does not contain the four statistical rules, v1.6 uses built-in campaign-scoped
defaults. Legacy per-rule `server:` values remain accepted, but only a rule
explicitly set to `scope: server` uses its server as a statistical filter.

Qualifications are also matched by their stable database IDs. This server's
configured qualification IDs are:

```yaml
qualifications:
  aar_ids: [15]
  ifr_ids: [16]
```

The startup log warns if either ID does not exist in `logbook_qualifications`.

## Existing pilots

The safe default is:

```yaml
bootstrap_existing: false
```

Pilots already holding valid AAR and IFR when the plugin first scans them are
registered as locked without a retroactive medal or payout. They become eligible
after both categories expire together and are earned again. Existing pilots who
already meet a Hardcore milestone are likewise baselined without retroactive
payment. That inherited streak must end before a fresh streak can earn the
baselined medal. A pilot below the next milestone can still earn it normally.

The same safety applies to statistical rules. Existing one-time achievements
are locked without payment. For recurring awards, completed milestones become
the baseline and only the next newly crossed milestone is awarded. For example,
a pilot first seen with 370 cargo hours has the next Airman's Medal due at 450
hours.

Set `bootstrap_existing: true` **before the plugin's first scan** if current
holders should receive retroactive medals and credits. Recurring rules grant
every completed milestone when bootstrapping: 370 cargo hours produces the
150-hour and 300-hour cycles. Changing the setting later does not alter progress
already registered in the state table.

## Campaign and server scopes

The reward destination is a stable campaign database ID. This installation uses:

```yaml
campaign_id: 1

hardcore:
  server: DCS.dcs_serverrelease
```

Campaign ID 1 currently represents `Jokers' Initial Campaign`. Its name is read
only for Discord messages and citations, so renaming it does not break tracking.
The plugin requires the campaign to exist and be active before granting a medal
or credit reward.

Every statistical medal chooses one tracking scope:

```yaml
rules:
  air_force_cross:
    scope: campaign

  aerial_achievement:
    scope: server
    server: DCS.dcs_serverrelease
```

`scope: campaign` combines missions from every `campaigns_servers` row for
campaign ID 1. `scope: server` resolves an exact DCSServerBot server name or an
exact DCS instance name, verifies that the resulting server belongs to campaign
ID 1, and filters `missions.server_name` accordingly. Credits from either scope
still go into the same campaign ID 1 wallet.

Air Achievement and Air Commendation do not accept independent scopes. They
always filter `hardcore_sessions` by both `campaign_id: 1` and the resolved
`hardcore.server`. Combat Readiness remains global because Logbook qualification
records do not contain server provenance; its credits still go to campaign ID 1.

At startup the plugin validates the campaign, server membership, scope values,
Hardcore server, award IDs, and qualification IDs. Invalid configuration holds
the affected award instead of issuing a medal or credits against the wrong scope.

Example award configuration:

```yaml
rules:
  combat_readiness:
    award_id: 26
    award_name: Combat Readiness Medal
  air_force_cross:
    award_id: 12
    award_name: Air Force Cross
  airmans_medal:
    award_id: 14
    award_name: Airman's Medal
  aerial_achievement:
    award_id: 17
    award_name: Aerial Achievement Medal
  air_achievement:
    award_id: 28
    award_name: Air Achievement Medal
  air_commendation:
    award_id: 24
    award_name: Air Commendation Medal
  combat_action:
    award_id: 18
    award_name: Combat Action Medal
```

## Unrelated Cloud schema errors

An error logged as `Plugin "Cloud not loaded"` with `Parsed key: type: two
times in schema files` is raised while validating the Cloud plugin's own schema.
AwardAutomation has its own isolated schema directory and does not load or alter
Cloud. Check the installed `plugins/cloud/schemas` directory for duplicate or
stale schema files if that error continues.

## Admin commands

| Command | Access | Purpose |
|---|---|---|
| `/awardautomation status user` | DCS Admin | Show qualifications, campaign-stat progress, recurring next thresholds, Hardcore streak, and rule states. |
| `/awardautomation scan [user]` | DCS Admin | Run reconciliation immediately for one pilot or everyone. |

Automatically granted medals are stored in `logbook_pilot_awards`, so they and
their ribbons appear in `/pilot status` without changing RankStatus.
