# JSH Awards 1.1 — monthly awards board

On the 1st of every month (UTC), posts last month's leaders in five
categories. It can also give each winner a **logbook** award, with the month
named in the citation.

| Category | Counts |
| --- | --- |
| `credits` — Most credits earned | **Gains** in `credits_log` during the month. Decay, spending and voting costs don't subtract. Transfers between pilots (`donation`) are excluded, or a gift would win it. |
| `csar` — Most CSAR rescues | `jsh_csar_rescues`, every server and CSAR script |
| `hours` — Most flight hours | Time in a slot, with each session **clipped to the month**, so a sortie over midnight on the 1st is split between the two months. Open sessions, and reversed ones the bot leaves behind, don't count. |
| `kd_aa` — Best K/D, air-to-air | Aircraft killed ÷ deaths to aircraft |
| `kd_ag` — Best K/D, air-to-ground | Ground, SAM and ship kills ÷ deaths to those |

**Campaign only.** Every category counts only inside one CreditSystem
campaign:

- its **servers** (`campaigns_servers`)
- its **dates**: a campaign that began on the 12th counts from the 12th
- pilots **registered** in it, meaning they have a `credits` row for that
  campaign. Pilots who never joined it don't appear at all.

That campaign is the one running now, or, when the board is posted just after
a campaign ended, the latest one that ran during the month. Set `campaign_id`
to pin one. The board names the campaign it covers.

K/D sides are matched, so one Su-27 cannot sink a strike pilot's air-to-ground
K/D. With no deaths, K/D equals the number of kills, as the bot itself does it.
A pilot needs `min_kills` kills of that kind (default 5) to appear on a K/D
board. K/D counts sorties that **started** in the month.

The board shows linked pilots as mentions but pings nobody.

## Posting

- **Master node only.**
- Each posted month is recorded in `jsh_awards_boards`. A restart on the 1st
  doesn't post twice, and a bot that was down on the 1st posts when it comes
  back.
- **The first month after install is skipped**, because the plugin wasn't
  there for all of it. `/awards post` posts it by hand if you want it anyway.
- A logbook award is given once per winner per month. Its citation names the
  month, so posting the same month again does not award it twice.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/awards preview [month]` | DCS | Shows a month's board privately. `month` is `YYYY-MM`; the default is last month. |
| `/awards post [month]` | DCS Admin | Posts a month's board to the channel now |

## Setup

1. Copy `plugins/jsh_awards/` and `config/jsh_awards.yaml`, set `channel`, and
   restart the bot.
2. Optionally create logbook awards with `/award create` and map them under
   `awards:`.
