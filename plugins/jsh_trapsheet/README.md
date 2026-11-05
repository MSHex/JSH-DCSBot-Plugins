# jsh_trapsheet

FunkMan-style trapsheets with **no MOOSE in the mission**.

## Why this exists

DCSServerBot already draws the FunkMan picture. `plugins/greenieboard/trapsheet.py`
renders the same three panels — lineup, glideslope, AoA — and its header says
it was "taken and amended from HypeMan". Nothing about it needs MOOSE or the
FunkMan package.

What has never existed without MOOSE is the **trajectory behind the plot**.
greenieboard has three intake paths:

| Path | Needs MOOSE | Trajectory | Plot |
| --- | --- | --- | --- |
| `Moose.AIRBOSS` | yes | yes, from a CSV | yes |
| `FunkMan` | data comes from AIRBOSS | yes | yes |
| `S_EVENT_LANDING_QUALITY_MARK` | **no** | **no** | no |

The stock Super Carrier LSO event carries a grade and a comment string and
nothing else, which is why a MOOSE-free server gets a greenieboard but never
a trapsheet.

This plugin samples the approach itself and hands the result to that same
plotter. There is no second rendering path to drift from the first.

## What it needs

Nothing. No MOOSE in the `.miz`, no FunkMan install, no new Python
dependency — matplotlib and numpy are already there for greenieboard.

## What you get, and where

**Super Carrier decks only.** `S_EVENT_LANDING_QUALITY_MARK` only fires on
Super Carrier modules — that is DCS's limit, not ours, and greenieboard's own
README says the same. A pass on a legacy deck could never be graded, so it
would be an ungraded picture cluttering the board.

Rather than sample a legacy deck and throw the result away, the mission
filters before it samples: `carrier_types` is a list of deck type-name
**prefixes**, defaulting to `CVN_`, `CV_59` and `Forrestal`. A Stennis or a
Kuznetsov therefore costs no sampling work and sends no datagrams at all.

Prefixes, not exact names, because the Super Carrier hulls are numbered one
by one — `CVN_71`, `CVN_72`, `CVN_73`, `CVN_75` — and an exact-name list
would match nothing on a real server.

If a finished pass gets no grade within `grade_wait`, it is **discarded**,
not plotted. On a Super Carrier deck that only happens when the grade was
genuinely lost, such as a disconnect mid-pass.

Rows go into greenieboard's own `traps` table, so `/greenieboard` and
`/traps` show these passes with their trapsheet. There is no second board.

### Posting

Set `channel` to a Discord channel id and the trapsheet is posted there as
an embed: grade and what it means, the LSO comment, wire, groove time, and
the plot itself. Leave it out and nothing is posted.

This is worth having because **greenieboard never posts the picture**. Its
`send_chat_message()` puts a one-line text message in the server's events
channel; the image it stores is only ever seen by somebody running `/traps`.

## How it works

`lua/mission.lua` is injected on `registerDCSServer` and on
`onMissionLoadEnd`. It ticks at `sample_rate`, and for every player aircraft
inside the capture window records four numbers: distance astern, lateral
offset, height above the deck, and AoA. When the pass ends — touchdown,
crash, or simply leaving the window — the samples are sent up as
`jshTrapsheet`. The listener holds them until the LSO grade arrives, or
discards them if `grade_wait` elapses first.

### Angle of attack

There is no `getAoA()` in the mission environment. AoA here is the angle
between the velocity vector and the airframe's longitudinal axis, computed
from `getVelocity()` and the orthonormal basis in `getPosition()` — the same
as MOOSE's `POSITIONABLE:GetAoA()`, which is what AIRBOSS calls.

**The wind is subtracted first**, as MOOSE does. AoA is an aerodynamic
angle, measured against the air and not the ground, and nowhere in DCS does
that matter more: a recovery runs 25–35 kt over the deck. Using ground
velocity biases AoA by most of a degree on a band only 1.4° wide (7.4–8.8),
so an on-speed pilot would plot consistently low.

It is in **degrees**. The Hornet's indexer units track degrees closely
(on-speed 8.1), which is why the 7.4/8.8 reference band in the plot reads
correctly for it. Other airframes use their own units and that band will not
mean the same thing for them.

### Two traps in the plotter worth knowing

Both were found by running it, and neither is documented:

1. **The AoA panel keys off the FILENAME, not `pinfo['aircraft']`.**
   `trapsheet.py:196-203` does `ps = Path(trapfile).stem` and substring-matches
   raw DCS codes — `FA-18`, `EA-18`, `T-45`, `F-14A-135-GR`, `F-14B`,
   `AV8BNA`, `A-4E-C` — to choose the on-speed band. So `render()` is handed a
   synthetic AIRBOSS-shaped name. Nothing is ever opened.

2. **There is no `else` branch.** An airframe matching none of those leaves
   `xmax` unbound and the plotter raises `UnboundLocalError` partway through
   drawing. `sheet.plot_key()` checks before plotting, so an unsupported type
   costs the picture and not the pass.

### Sample rate

`0.1` (10 Hz) is the default and is already well past what the data needs.
Glideslope and lineup are smooth; AoA oscillates at roughly 0.5–2 Hz, so
10 Hz sits comfortably above Nyquist. A smaller interval mostly samples DCS's
own frame jitter — `timer.scheduleFunction` cannot beat the sim step — and
costs that work on every approach on every instance.

Size is not a concern in this direction. `do_script` going bot→DCS is capped
at 8192 bytes and truncates silently, which is why the injection is measured
before sending (it is ~4.9 KB). The samples travel the other way, through
`dcsbot.sendBotTable`, which chunks at 65000 bytes and is reassembled by the
bot with a 2 MB receive buffer. A 600-sample pass is nowhere near it.

## Axes

The frame was checked against MOOSE's source, not guessed:

- `Airboss.lua` `_GetDistances()` projects the carrier→player vector onto the
  **ship's own orientation axes**, so `X` and `Z` are ship frame and `X` is
  *negative* astern.
- `Airboss.lua:19177` then writes `local b = -groove.X`, negating it, so the
  CSV column `trapsheet.py` actually reads is **positive astern**. `Z` is
  written raw — starboard positive.

`relative()` in `lua/mission.lua` produces exactly that, projecting onto the
carrier's own basis vectors (`p.x` forward, `p.z` starboard) rather than
deriving a perpendicular by hand, so there is no second derivation to get
backwards.

`axes.invert_x` / `axes.invert_z` remain in the config as an escape hatch,
but they should not be needed.

### One thing that looks wrong and is not

`trapsheet.py` rotates by `-9°` for the angled deck, and `Z` is relative to
the **ship's axis**, not the landing centreline. So a well-lined-up approach
has `Z` growing with distance astern — roughly `X · tan(9°)` — and plots as a
flat lineup trace. A `Z` of zero all the way down the groove is an aircraft
flying up the ship's *wake*, and plots as about 950 ft of lineup error at
1 NM. That is the geometry working, not a bug.

## Configuration

See `config/jsh_trapsheet.yaml`. Sections key on the **instance** name from
`nodes.yaml`, never the server name from `servers.yaml`.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Sample approaches on this instance |
| `sample_rate` | `0.1` | Seconds between samples |
| `capture_range_nm` | `1.5` | Start sampling inside this range of the deck |
| `capture_ceiling_ft` | `1500` | Only sample below this height above the deck |
| `max_points` | `600` | Cap per pass, so a go-around cannot grow unbounded |
| `carrier_types` | `CVN_`, `CV_59`, `Forrestal` | Deck type-name **prefixes** to sample |
| `grade_wait` | `20` | Seconds to wait for an LSO grade before discarding the pass |
| `night` | `false` | Record passes as night traps |
| `axes.invert_x` | `false` | Flip if the approach plots back to front |
| `axes.invert_z` | `false` | Flip if lineup is mirrored left for right |

## Not done yet

- **Unicorn passes.** AIRBOSS marks a perfect pass with `SH_`/`unicorn_` in
  the filename and the plotter draws a banner for it. Adding that is a
  one-line change in `sheet.trapfile_name()`, left out until the grading
  semantics are confirmed rather than guessed.
- **Wave-offs and bolters** are stored like any other pass. The samples
  distinguish them, but nothing reads that yet.
