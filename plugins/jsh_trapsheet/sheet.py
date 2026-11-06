"""
Turning sampled points into what the plotter wants, and reading the LSO string.

This module imports nothing from DCSServerBot, discord, psycopg or any other
plugin -- only numpy, which matplotlib already pulls in. That keeps it
testable without the bot on the path, and keeps the parsing honest: every
rule here is checkable against a string, with no server to stand up.
"""
import re

from typing import Any, Optional

import numpy as np

#: The four arrays plugins/greenieboard/trapsheet.py reads. Nothing else in
#: the trapsheet dict is touched by it.
COLUMNS = ('X', 'Z', 'Alt', 'AoA')

#: How DCS type names appear on the plot. Mirrors the mapping in
#: greenieboard/trapsheet.py parse_filename(), because 'AV-8B' is load
#: bearing there: it is the one value that sets theta_brc to 0 instead of -9,
#: the Harrier having no angled-deck offset to correct for.
AIRCRAFT = {
    'AV8BNA': 'AV-8B',
    'A-4E-C': 'A-4',
    'F-14A-135-GR': 'F-14A-135-GR',
    'F-14B': 'F-14B',
    'T-45': 'T-45C',
    'FA-18C_hornet': 'F/A-18C',
    'FA-18E': 'F/A-18E',
    'FA-18F': 'F/A-18F',
    'EA-18G': 'E/A-18G',
}

#: Deck type-name PREFIXES the mission samples against.
#:
#: Only Super Carrier decks raise S_EVENT_LANDING_QUALITY_MARK, so a pass
#: anywhere else could never be graded and would be clutter on the board.
#: Filtering mission-side means a legacy deck costs no sampling work and
#: sends no datagrams at all, rather than being discarded after the fact.
#:
#: Prefixes, not exact names: the Super Carrier hulls are numbered
#: individually (CVN_71, CVN_72, CVN_73, CVN_75) and an exact-name list would
#: match none of them on a real server.
#: `CVN_` covers the four Super Carrier hulls -- CVN_71, CVN_72, CVN_73 and
#: CVN_75, per AIRBOSS's CarrierType table. Nothing else is in by default.
#: Forrestal is Heatblur's, not part of the Super Carrier module, and whether
#: DCS grades it has not been confirmed; add it in config to find out. An
#: ungraded pass is discarded, so the cost of trying is wasted sampling, not
#: clutter.
DEFAULT_DECKS = ('CVN_',)


def lua_list(values) -> str:
    """A Lua table literal of strings, for injection into the sampler."""
    out = []
    for v in values or ():
        s = str(v).replace('\\', '\\\\').replace('"', '\\"')
        out.append(f'"{s}"')
    return '{' + ','.join(out) + '}'


#: The AoA panel of greenieboard's plotter picks its on-speed reference band
#: by substring-matching the TRAPFILE NAME -- not pinfo['aircraft'] -- against
#: these raw DCS codes (trapsheet.py:196-203, `ps = Path(trapfile).stem`).
#:
#: There is NO else branch. An aircraft that matches none of these leaves
#: `xmax` unbound and the plotter raises UnboundLocalError partway through
#: drawing. So the type must be checked BEFORE plotting, not caught after:
#: see plot_key(). This is why render() is handed a synthetic filename.
AOA_CODES = ('FA-18', 'EA-18', 'T-45', 'F-14A-135-GR', 'F-14B', 'AV8BNA', 'A-4E-C')

#: Matches DCSServerBot's own mapping in greenieboard/db/tables.sql, so a row
#: we write scores the same as a row the bot wrote.
POINTS = {
    '_OK_': 5.0, 'OK': 4.0, '(OK)': 3.0, 'B': 2.5, '--': 2.0,
    'WO': 1.0, 'C': 0.0, 'NC': 0.0,
}
"""Ratings from greenieboard's const.GRADES, used only when greenieboard wrote
no row of its own. When it did, its points stand and these are never read."""

#: Any grade token, as greenieboard's get_element() takes it, but tolerating
#: the space DCS puts after the colon on some grades -- seen live:
#: `LSO: GRADE: NC : No proper communications`. An earlier version listed the
#: allowed characters instead, copying the bot's old migration, and `N` was
#: not among them: a no-count simply did not parse.
_GRADE = re.compile(r'GRADE:\s*([^:\s]+)')
_WIRE = re.compile(r'WIRE#\s*([1-4])')

#: greenieboard rewrites these before storing (listener.py, process_lso_event).
#: The attach matches greenieboard's row ON THE GRADE, so ours must be
#: rewritten identically -- otherwise an OWO looks for 'OWO', never finds the
#: 'NC' row greenieboard wrote, and falls back to inserting a second one.
ALIASES = {'WOP': 'NC', 'OWO': 'NC', 'TWO': 'NC', 'TLU': 'NC', 'WOFD': 'WO'}


def grade_of(comment: str) -> Optional[str]:
    """The LSO grade from a DCS landing-quality comment, or None.

    Normalised exactly as greenieboard normalises it -- `---` to `--`, then
    the aliases above -- so the grade we match on is the grade it stored.
    """
    if not comment:
        return None
    m = _GRADE.search(comment)
    if not m:
        return None
    grade = m.group(1).replace('---', '--')
    return ALIASES.get(grade, grade)


def wire_of(comment: str) -> Optional[int]:
    """The wire caught, or None for a bolter or a wave-off."""
    if not comment:
        return None
    m = _WIRE.search(comment)
    return int(m.group(1)) if m else None


def points_of(grade: Optional[str]) -> Optional[float]:
    return POINTS.get(grade) if grade else None


def aircraft_label(unit_type: str) -> str:
    return AIRCRAFT.get(unit_type, unit_type or 'unknown')


def plot_key(unit_type: str) -> Optional[str]:
    """
    The AOA_CODES entry the plotter would match for this type, or None.

    None means the plotter cannot draw this airframe: it would get partway
    through and raise UnboundLocalError on `xmax`. Checked up front so the
    pass is still recorded, just without a picture.
    """
    if not unit_type:
        return None
    return next((c for c in AOA_CODES if c in unit_type), None)


def trapfile_name(unit_type: str, callsign: str) -> str:
    """
    A synthetic trapfile name for the plotter to read the airframe out of.

    plot_trapsheet() takes a path only to recover the aircraft code and to
    look for AIRBOSS's `SH_`/`unicorn_` markers. Nothing is opened, so this
    never touches the disk -- it exists purely to satisfy that substring
    match. The real AIRBOSS name looks like
    `AIRBOSS-<carrier>-<callsign>_<type>-1.csv`, and this mirrors it closely
    enough for the branch to fire.
    """
    safe = ''.join(ch for ch in (callsign or 'pilot') if ch.isalnum() or ch in '-_')
    return f"AIRBOSS-jsh-{safe}_{unit_type}-1.csv"


def to_trapsheet(points: list, axes: Optional[dict] = None) -> dict[str, Any]:
    """
    The {X, Z, Alt, AoA} dict of numpy arrays that plot_trapsheet() expects.

    `points` is the mission's list of [x, z, alt, aoa] rows. `axes` can flip
    either horizontal sign, because which way is 'astern' and which way is
    'starboard' is a convention that has to be confirmed against a real trap
    rather than reasoned about -- see the README.
    """
    axes = axes or {}
    sx = -1.0 if axes.get('invert_x') else 1.0
    sz = -1.0 if axes.get('invert_z') else 1.0

    rows = [p for p in points if isinstance(p, (list, tuple)) and len(p) >= 4]
    if not rows:
        return {c: np.array([]) for c in COLUMNS}

    arr = np.array([[float(v) for v in p[:4]] for p in rows], dtype=float)
    return {
        'X': sx * arr[:, 0],
        'Z': sz * arr[:, 1],
        'Alt': arr[:, 2],
        'AoA': arr[:, 3],
    }


def groove_time(points: list, rate: float) -> Optional[float]:
    """
    Seconds from the start of the groove to touchdown.

    The groove starts at the wake, 3/4 NM astern by the usual reckoning, so
    this counts the samples inside that and multiplies by the sampling
    interval. It is only as accurate as the sample rate, which is why it is
    rounded to a tenth -- the same precision the LSO reads it to.
    """
    if not points or rate <= 0:
        return None
    wake = 0.75 * 1852.0
    inside = sum(1 for p in points
                 if isinstance(p, (list, tuple)) and len(p) >= 1
                 and 0 < float(p[0]) <= wake)
    return round(inside * rate, 1) if inside else None


#: What each LSO grade means, for the embed. Wording follows the standard
#: LSO grade sheet rather than being invented here.
GRADE_TEXT = {
    '_OK_': 'Unicorn — a perfect pass',
    'OK': 'Fair pass, reasonable deviations',
    '(OK)': 'Fair pass with one or more corrected deviations',
    '--': 'No-grade. Below average but safe',
    'B': 'Bolter',
    'WO': 'Wave-off',
    'C': 'Cut pass — unsafe',
    'NC': 'No count — the pass was not graded',
}

#: Embed colour by how the pass went. Green good, amber marginal, red unsafe.
GRADE_COLOUR = {
    '_OK_': 0x2ECC71, 'OK': 0x2ECC71, '(OK)': 0xF1C40F, '--': 0xE67E22,
    'B': 0xE67E22, 'NC': 0x95A5A6,
    'WO': 0xE74C3C, 'C': 0xE74C3C,
}


def grade_line(grade: Optional[str]) -> str:
    """The grade and what it means, or an honest blank."""
    if not grade:
        return 'Not graded'
    text = GRADE_TEXT.get(grade)
    return f"**{grade}** — {text}" if text else f"**{grade}**"


def colour_of(grade: Optional[str]) -> int:
    return GRADE_COLOUR.get(grade or '', 0x95A5A6)
