# Plugin "jsh_coords"

Converts a coordinate pasted from DCS into every other format DCS uses.

Paste any one of them and get the rest back:

| Format | Example |
| --- | --- |
| MGRS grid | `32U MV 12345 67890` |
| Lat/Long (precise) | `N 49°26'01.48" E 8°13'06.09"` |
| Lat/Long (standard) | `N 49°26'01" E 8°13'06"` |
| Lat/Long decimal minutes | `N 49°26.024' E 8°13.101'` |
| Decimal degrees | `49.433744, 8.218358` |
| DCS metric | `X-00506931 Z-00890072` |

The run-together form works too, which is usually faster to type off a UFC or a
kneeboard than hunting for the degree symbol:

```text
N 492601.48 E 81306.09
```

## Commands

| Command | Role |
| --- | --- |
| `/coords convert <coordinate> [server] [public]` | anyone |

The format is detected from what you paste — there is nothing to pick first.
Spacing, the degree and minute marks, hemisphere letters before or after the
number, and colons instead of symbols are all accepted, so pasting straight out
of the game, a kneeboard or a Discord message works:

```text
/coords convert N 49°26'01.48" E 8°13'06.09"
/coords convert 32UMV1234567890
/coords convert 49.433744 8.218358
/coords convert N49 26 01.48 E8 13 06.09
```

`public: true` posts the result in the channel; by default only you see it,
which is what you want mid-flight.

### Degree-digit padding

Aircraft disagree about how many digits the longitude degrees get. DCS's own
coordinate box prints `E 8°13'06.09"`; the Hornet, Viper and A-10C pad to three
and show `E 008°13.101'`. In the run-together form that is the difference
between `81306.09`, `081306.09` and `0081306.09`.

All three are accepted, and so is a padded latitude. Nothing needs configuring
and the plugin never has to know which airframe you read it off, because the
number is split from the **right**: minutes and seconds are always two digits
each, so whatever is left over is the degrees. Reading left to right would have
to know the padding in advance; reading right to left does not care.

Four digits or fewer means there are no seconds and the trailing pair is decimal
minutes instead, so `N 4926.024 E 813.101` is read as `N 49°26.024' E 8°13.101'`.

The one casualty is a genuine zero-degree reading: leading zeros are stripped
before the split, so `00813` is read as `8°13'` rather than `0°08'13"`. Write
that one with separators. It falls on the prime meridian in the Gulf of Guinea
and on no DCS map.

## The `server` option

Four of the six formats are plain arithmetic and need nothing — no server
selected, no mission running, nothing loaded in DCS.

**DCS metric X/Z and terrain altitude are different.** They depend on the loaded
terrain's own projection and heightmap, so only a running mission can produce
them. Pass `server` to get them:

- Converting **to** metric: the four lat/long formats always come back; metric
  and altitude are added when a server is given, and the footer says why they
  are missing when it isn't.
- Converting **from** metric: a server is **required**, because `X/Z` means
  nothing without knowing which map it is on. Without one the command says so
  instead of guessing.

Metric is reported with the theatre name in the footer, so a grid pasted from
the wrong map is obvious rather than silently plausible.

## Precision

DCS **truncates** rather than rounds when it displays a coordinate: an internal
`13.1015'` appears as `13.101'`, not `13.102'`. This plugin truncates the same
way, so what it prints matches what the game showed for the same point rather
than differing in the last digit.

Precision cannot be recovered from a format that never carried it. The DCS box
prints all of its formats from one full-precision value, so the precise and
decimal-degree lines agree there — but `49.433744` on its own works back out to
`01.4784"`, which displays as `01.47"`, not the `01.48"` the game showed
alongside it. That is a third of a metre, and it is arithmetic, not a bug: paste
the most precise form you have if it matters.

| Pasted format | Pins the point to about |
| --- | --- |
| Lat/Long precise | 0.3 m |
| Decimal degrees (6 dp) | 0.1 m |
| Lat/Long decimal minutes | 1.8 m |
| Lat/Long standard | 30 m |
| MGRS (10 figure) | 1 m |

## Installation

1. Copy this folder into `<bot>/plugins/jsh_coords/`.
2. Copy `config/jsh_coords.yaml` into `<bot>/config/plugins/`.
3. Add `jsh_coords` to `opt_plugins` in `config/main.yaml`.
4. Restart the bot.

On a multi-node setup, do this on **every** node. The bot copies a plugin's
`lua/` folder to the servers of nodes that list it in `opt_plugins`, and only
at bot startup — so a node that was missed has no `mission.lua`, and metric and
altitude time out there while the rest of the conversion keeps working. The log
of that node says so explicitly:

```text
Coords: mission.lua NOT FOUND at ...
```

No database tables and no DCS restart are needed; `mission.lua` is loaded into
the running mission on mission start and on bot restart.

## Requirements

- DCSServerBot 3.x
- The `mgrs` package, which DCSServerBot already requires (`core/utils/dcs.py`
  imports it) — the MGRS maths is the bot's own `dd_to_mgrs` / `mgrs_to_dd`,
  not a second implementation here.

## Configuration

| Key | Meaning |
| --- | --- |
| `enabled` | Whether this server answers the mission half (metric, altitude). On by default. Does not affect MGRS or lat/long. |

## Tests

From the repository root:

```text
python -m pytest -q tests/test_jsh_coords.py
```

`coords.py` holds the parsing and formatting and imports neither discord nor
DCSServerBot, so it is testable on its own. The reference values in the tests are
taken from the DCS coordinate box showing one point in all four formats at once,
so they check agreement with the game's display rather than merely internal
consistency.
