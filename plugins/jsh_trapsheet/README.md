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
**prefixes**, defaulting to `CVN_` alone — the four Super Carrier hulls,
`CVN_71`, `CVN_72`, `CVN_73`, `CVN_75`, per AIRBOSS's `CarrierType` table. A
Stennis or a Kuznetsov therefore costs no sampling work and sends no
datagrams at all.

The **Forrestal** is Heatblur's, not part of the Super Carrier module, and
whether DCS grades it is unconfirmed. Add `Forrestal` to `carrier_types` to
find out: an ungraded pass is discarded, so the worst case is wasted
sampling, not clutter.

Prefixes, not exact names, because the Super Carrier hulls are numbered one
by one — `CVN_71`, `CVN_72`, `CVN_73`, `CVN_75` — and an exact-name list
would match nothing on a real server.

If a finished pass gets no grade within `grade_wait`, it is **discarded**,
not plotted. On a Super Carrier deck that only happens when the grade was
genuinely lost, such as a disconnect mid-pass.

### One row per pass, greenieboard's

greenieboard handles `S_EVENT_LANDING_QUALITY_MARK` itself and writes a row
to `traps` for every Super Carrier grade — deriving day or night from mission
time, setting the case, scoring from your configured `grades`, crediting the
points. So this plugin **attaches the trapsheet to greenieboard's row**
rather than writing its own. An earlier version inserted a second row, which
would have put every graded pass on the board twice at two different scores.

Both plugins react to the same event and nothing orders them, so
greenieboard's row may not exist yet: the attach is retried for three
seconds. Only if greenieboard never writes one — which happens when FunkMan
is enabled in `greenieboard.yaml`, since it then ignores Super Carrier events
— is a row inserted here, shaped like greenieboard's.

The match is deliberately narrow — same pilot, mission and grade, no picture
yet, written in the last 30 seconds — so an older picture-less row from a
pass whose samples were lost can never take this pass's picture. `traps.time`
is UTC with no time zone, and the window is computed the same way; a bare
`now()` would be off by the database session's offset. All of this is tested
against a real PostgreSQL built from greenieboard's own `tables.sql` in
`tests/trapsheet_sql_check.py`.

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
in the groove of a sampled deck records four numbers: distance astern,
lateral offset, height above the deck, and AoA. When the pass ends —
touchdown, crash, or leaving the groove — the samples are sent up as
`jshTrapsheet`.

### What counts as "in the groove"

Inside `capture_range_nm` and `capture_ceiling_ft`, astern of the ship, and
**closing on it** at more than 5 m/s along its axis.

The closing test is what keeps the downwind leg out. The downwind runs about
1.2 NM abeam at 600 ft — inside any sensible capture window — but flown the
opposite way to the ship, so it closes at a strongly negative rate. Closing
turns positive past the 90° point, which is where AIRBOSS starts recording
too (its `Final` step). Without it the downwind and the whole 180 turn would
fold back over the groove on the plot. The 5 m/s floor means an aircraft
stopped in the wires reads as not closing, and the pass ends.

A pass is locked to its deck once it starts, so a second carrier nearby
cannot steal it mid-groove.

### Grade and samples can arrive in either order

They travel different paths and nothing orders them. On a trap the samples
flush 0.1 s after touchdown; on a wave-off or a bolter they only flush once
the aircraft has passed the ship, by which time the grade has often landed.
So each half waits up to `grade_wait` for the other. An earlier version only
waited on the grade, and silently lost any pass whose grade came first.

### Height above the deck

The ship's origin is at the **waterline**, so height above the ship is about
20 m (~70 ft) too high on every sample — every pass would plot high, worst
at the ramp. AIRBOSS's `_GetAltCarrier()` is `GetAltitude() - deckheight - 2`,
with deck heights per hull from DCS's own unit database: 20.1494 m for the
Super Carrier Nimitz hulls, 20 m for the Forrestal. This uses the same figures
and the same 2 m reference-point correction.

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

For comparison, **AIRBOSS samples at 0.5 s** (`self.dTstatus = TimeInterval
or 0.5`), so the FunkMan sheets you may be used to are 2 Hz. That is why their
AoA trace looks jagged. These will look smoother: that is resolution, not
filtering.

Size is not a concern in this direction. `do_script` going bot→DCS is capped
at 8192 bytes and truncates silently, which is why the injection is measured
before sending (it is ~5.9 KB — the tightest of the three plugins that
inject, so the one to watch if the sampler grows). The samples travel the other way, through
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
| `carrier_types` | `CVN_` | Deck type-name **prefixes** to sample |
| `grade_wait` | `20` | Seconds to wait for an LSO grade before discarding the pass |
| `axes.invert_x` | `false` | Flip if the approach plots back to front |
| `axes.invert_z` | `false` | Flip if lineup is mirrored left for right |

## Not done yet

- **Unicorn passes.** AIRBOSS marks a perfect pass with `SH_`/`unicorn_` in
  the filename and the plotter draws a banner for it. Adding that is a
  one-line change in `sheet.trapfile_name()`, left out until the grading
  semantics are confirmed rather than guessed.
- **Wave-offs and bolters** are stored like any other pass. The samples
  distinguish them, but nothing reads that yet.
