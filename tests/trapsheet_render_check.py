"""
jsh_trapsheet's render(), driven through greenieboard's REAL plotter.

The first live trap stored a blank 3048x1350 image instead of the 1200x900
trapsheet. The plot had been saved with plt.savefig(), which saves pyplot's
process-wide CURRENT figure -- and the trap had just made greenieboard
redraw its board in another thread, so a report figure was current at the
moment we saved.

This races exactly that: a board-sized figure is created in another thread
while ours is being drawn, then checks that what comes back is OUR figure.
Restoring plt.savefig() makes it fail.

Needs a DCSServerBot checkout for plugins/greenieboard:

    python3 -I tests/trapsheet_render_check.py <path to DCSServerBot>
"""
import io
import math
import pathlib
import sys
import threading

BOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
if not BOT or not (BOT / 'plugins' / 'greenieboard' / 'trapsheet.py').exists():
    sys.exit("usage: trapsheet_render_check.py <path to a DCSServerBot checkout>")

import os  # noqa: E402
os.chdir(BOT)                       # the plotter loads its carrier art relative to cwd
sys.path.insert(0, str(BOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / 'plugins' / 'jsh_trapsheet'))

import matplotlib  # noqa: E402
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image  # noqa: E402

import sheet  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(label, ok, detail=''):
    checks.append((label, ok, detail))


n = 240
pts = [[1852.0 * (1 - i / n),
        1852.0 * (1 - i / n) * math.tan(math.radians(9)),
        1852.0 * (1 - i / n) * math.tan(math.radians(3.5)) * 3.28084,
        8.1] for i in range(n)]
ts = sheet.to_trapsheet(pts)
pinfo = {'aircraft': 'F/A-18C', 'callsign': 'Test', 'time': '2026-11-06 10:56:44'}
trapfile = sheet.trapfile_name('FA-18C_hornet', 'Test')

# 1. on its own
png = sheet.render(ts, pinfo, trapfile)
size = Image.open(io.BytesIO(png)).size
check("render() returns OUR 1200x900 figure", size == (1200, 900), f"got {size}")

# 2. greenieboard's board report starts drawing WHILE ours is being drawn.
#
# The timing is the whole point. A board figure created before we start is
# harmless -- plt.subplots() makes ours current again. What happened live is
# that the board's figure appeared AFTER we started drawing and BEFORE we
# saved, so the old plt.savefig() found it current. The plotter is wrapped
# so the board appears in another thread exactly then.
import plugins.greenieboard.trapsheet as gb  # noqa: E402

real_plot = gb.plot_trapsheet


def board():
    # What core/report/elements.py does: a pyplot-managed figure, left open.
    f = plt.figure(figsize=(20.32, 9), dpi=150)
    f.patch.set_facecolor('#2b2b2b')


def plot_then_board(*args, **kwargs):
    real_plot(*args, **kwargs)
    t = threading.Thread(target=board)
    t.start()
    t.join()


gb.plot_trapsheet = plot_then_board
try:
    png = sheet.render(ts, pinfo, trapfile)
finally:
    gb.plot_trapsheet = real_plot
size = Image.open(io.BytesIO(png)).size
check("a board figure created mid-draw in another thread does not replace ours",
      size == (1200, 900),
      f"got {size} -- that is the board's figure, saved as our trapsheet")
plt.close('all')

# 3. the AoA band is restyled FunkMan fashion: red limits, green on-speed
#    at their midpoint -- 7.4 / 8.1 / 8.8 for the Hornet.
from matplotlib.figure import Figure  # noqa: E402
fig = Figure(dpi=50)
axs = fig.subplots(3, 1, sharex=True)
gb.plot_trapsheet(axs, ts, pinfo, trapfile)
check("found greenieboard's two band lines", sheet.style_aoa_band(axs[2]))
levels = sorted({round(float(l.get_ydata()[0]), 2) for l in axs[2].get_lines()
                 if l.get_linestyle() == '--' and len(l.get_ydata()) == 2})
check("Hornet band is 7.4 / 8.1 / 8.8 with on-speed added", levels == [7.4, 8.1, 8.8],
      f"got {levels}")
check("the limits are red", all(l.get_color() == sheet.BAND_LIMIT for l in axs[2].get_lines()
      if l.get_linestyle() == '--' and round(float(l.get_ydata()[0]), 2) in (7.4, 8.8)))

# 4. ...and render() actually applies it to the picture it returns.
seen = []
real_style = sheet.style_aoa_band
sheet.style_aoa_band = lambda ax: seen.append(ax) or real_style(ax)
try:
    sheet.render(ts, pinfo, trapfile)
finally:
    sheet.style_aoa_band = real_style
check("render() restyles the AoA panel of the picture it returns", len(seen) == 1,
      "the band would be drawn greenieboard's way, thin and with no on-speed line")

failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    print(('  ok  ' if ok else ' FAIL '), label, f'-- {detail}' if detail and not ok else '')
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
