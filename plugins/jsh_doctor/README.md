# JSH Doctor 1.0 — `/jsh doctor`

Checks JSH's setup for the mistakes that fail **silently**. Each one has
already cost an evening. The command is read-only and changes nothing.

`/jsh doctor` shows only problems and warnings. Add `everything:True` to also
list the checks that passed.

| Area | Check |
| --- | --- |
| Config | Every section in `config/plugins/*.yaml` names a real **instance** from `nodes.yaml` (or a node, with its own instances inside it). A section named after a **server** is reported as a failure, because it validates but does nothing. |
| Config | `validation: strict` in `main.yaml`, so a bad config stops at load and says why |
| Foothold | The external config is named `Foothold Config.lua`, with a **space**. With an underscore, Foothold quietly uses the copy inside the `.miz`. |
| Foothold | `RankingSystem = true` (checked when `jsh_ranks` is loaded). Commented-out lines are ignored. |
| Discord | The members intent is on |
| Discord | Every rank role and officer role exists and sits **below the bot's own role**, so the bot can give and take it |
| Database | No sessions with `hop_off` before `hop_on`. These break `/pilot status` and AwardAutomation, and `/statfix run` repairs them. |
| Database | Sessions open for over a day, which the bot would later credit up to the mission's end |
| Database | `jsh_cq`'s logbook qualifications don't also carry logbook requirements |

Instance files can only be read on the node they live on. An instance on
another node is listed as not checked.

No configuration file is needed.
