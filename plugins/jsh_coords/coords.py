"""
Parsing and formatting for the coordinate formats DCS displays.

Deliberately free of discord and DCSServerBot imports so it can be exercised on
its own. MGRS is not implemented here: the bot already ships dd_to_mgrs and
mgrs_to_dd in core.utils, backed by the `mgrs` package, and a second
implementation would be one more thing to get subtly wrong.

DCS **truncates** rather than rounds when it displays coordinates -- 13.1015'
shows as 13.101', and 6.09" shows as 06". Since the whole point of this tool is
that people paste what DCS showed them and compare, the formatters truncate to
match. Parsing keeps full precision either way.
"""
import math
import re

from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------- shapes


@dataclass(frozen=True)
class LatLon:
    lat: float
    lon: float


class CoordError(ValueError):
    """Raised when a string cannot be read as any supported format."""


# --------------------------------------------------------------------- helpers

def _truncate(value: float, places: int) -> float:
    """
    Cut, don't round -- see the module docstring.

    The epsilon is not cosmetic. 49°26'01.48" parses to 49.43374444... and
    converting that back gives 1.4799999999 seconds, which a naive floor turns
    into 01.47 -- the tool would hand back a different string from the one the
    user pasted in. Nudging by a ulp-ish amount before flooring keeps a value
    that is "really" 1.48 at 1.48, while a value genuinely below the cut
    (1.4799) still truncates down.
    """
    factor = 10 ** places
    return math.floor(abs(value) * factor + 1e-6) / factor * (1 if value >= 0 else -1)


def _hemi(value: float, positive: str, negative: str) -> str:
    return positive if value >= 0 else negative


# --------------------------------------------------------------------- parsing
#
# One regex per format, tried in order of how distinctive they are. MGRS and
# metric are unmistakable; the lat/long forms are told apart by whether the
# minutes carry a decimal point.

_MGRS_RE = re.compile(
    r'^\s*(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z]{2})\s*'
    r'(\d{2,10})(?:\s+(\d{2,5}))?\s*$', re.IGNORECASE)

# DCS writes metric as "X-00506931 Z-00890072". That hyphen is a separator, not
# a sign -- the value above is positive. A negative axis therefore shows a second
# hyphen, so one dash means separator and two mean negative.
_METRIC_RE = re.compile(
    r'^\s*X[\s:=]*(-?)(-?)(\d+(?:\.\d+)?)[\s,]+Z[\s:=]*(-?)(-?)(\d+(?:\.\d+)?)\s*$',
    re.IGNORECASE)

# N 49°26'01.48"  /  N49 26 01.48  /  49°26'01"N
_DMS_PART = r'''
    (?:(?P<pre>[NSEW])\s*)?
    (?P<deg>-?\d{1,3})\s*[°\s:]\s*
    (?P<min>\d{1,2}(?:\.\d+)?)\s*(?P<minmark>['′m:]?)\s*
    (?:(?P<sec>\d{1,2}(?:\.\d+)?)\s*["″s]?)?\s*
    (?:(?P<post>[NSEW]))?
'''
_LATLON_RE = re.compile(
    r'^\s*' + _DMS_PART + r'[\s,/]+' + _DMS_PART.replace('>', '2>') + r'\s*$',
    re.IGNORECASE | re.VERBOSE)

# The run-together form pilots actually type: N 492601.48 E 81306.09, and the
# same thing as a jet displays it with the longitude padded to three degree
# digits: E 0081306.09. Split from the RIGHT -- see _split_compact.
_COMPACT_RE = re.compile(
    r'^\s*(?:(?P<pre>[NSEW])\s*)?(?P<first>\d{3,9}(?:\.\d+)?)\s*(?P<post>[NSEW])?'
    r'[\s,/]+'
    r'(?:(?P<pre2>[NSEW])\s*)?(?P<second>\d{3,9}(?:\.\d+)?)\s*(?P<post2>[NSEW])?\s*$',
    re.IGNORECASE)

_DECIMAL_RE = re.compile(
    r'^\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([NS])?\s*[,;\s]\s*'
    r'(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([EW])?\s*$', re.IGNORECASE)


def _signed(deg: float, minutes: float, seconds: float, hemi: Optional[str]) -> float:
    value = abs(deg) + minutes / 60.0 + seconds / 3600.0
    negative = (deg < 0) or (hemi and hemi.upper() in ('S', 'W'))
    return -value if negative else value


def _hemispheres(*letters: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Sort the N/S/E/W letters found anywhere in the string into (lat, lon).

    Position can't be trusted. In `N 00°00'00.01" W 179°59'59.99"` the regex
    hands the W to the *latitude's* trailing-hemisphere slot, because that slot
    is optional and sits right before the separator -- which silently turned a
    western longitude into an eastern one. Assigning by letter class instead of
    by position is immune to that, and as a side effect accepts the halves in
    either order.
    """
    found = [letter.upper() for letter in letters if letter]
    lat = next((letter for letter in found if letter in ('N', 'S')), None)
    lon = next((letter for letter in found if letter in ('E', 'W')), None)
    return lat, lon


def _split_compact(number: str) -> tuple[float, float, float]:
    """
    Split a run-together coordinate into degrees, minutes and seconds.

    Parsed from the RIGHT, which is what makes this unambiguous. Minutes and
    seconds are always two digits, so whatever is left over is the degrees --
    and that is why a jet padding its longitude to three digits needs no special
    case at all:

        81306.09  -> 8 deg 13' 06.09"      (DCS's own box)
        081306.09 -> 08 deg 13' 06.09"     (Hornet, Viper, A-10C)
        0081306.09 -> 008 deg 13' 06.09"   (padded to three)

    All three are the same longitude. Reading left to right would have to know
    in advance how many degree digits the aircraft prints; reading right to left
    does not care.

    Four digits or fewer means there are no seconds and the trailing pair is
    decimal minutes instead -- 4926.024 is 49 deg 26.024'.

    Leading zeros are stripped first, so a padded 00813 is read as 8 deg 13'
    rather than as 0 deg 08' 13". The cost is that a genuine 0 deg 08' 13" has
    to be written with separators. That point is in the Gulf of Guinea, on the
    prime meridian, and on no DCS map.
    """
    integer, _, fraction = number.partition('.')
    fraction = f'0.{fraction}' if fraction else '0'

    trimmed = integer.lstrip('0') or '0'
    # Keep enough digits for degrees + two minute digits.
    integer = trimmed.rjust(3, '0')

    if len(integer) >= 5:
        degrees, minutes, seconds = integer[:-4], integer[-4:-2], integer[-2:]
        return float(degrees), float(minutes), float(seconds) + float(fraction)

    degrees, minutes = integer[:-2], integer[-2:]
    return float(degrees), float(minutes) + float(fraction), 0.0


def parse_mgrs(text: str) -> Optional[str]:
    """Normalises an MGRS string to the compact form the mgrs package wants."""
    match = _MGRS_RE.match(text)
    if not match:
        return None
    zone, band, square, first, second = match.groups()
    digits = first + (second or '')
    if len(digits) % 2:
        raise CoordError(f"MGRS needs an even number of digits, got {len(digits)}")
    return f"{int(zone):02d}{band.upper()}{square.upper()}{digits}"


def parse_metric(text: str) -> Optional[tuple[float, float]]:
    """DCS map metres as (x, z). Terrain-specific -- only a mission can place it."""
    match = _METRIC_RE.match(text)
    if not match:
        return None
    x_sep, x_sign, x_val, z_sep, z_sign, z_val = match.groups()
    x = float(x_val) * (-1 if x_sign else 1)
    z = float(z_val) * (-1 if z_sign else 1)
    return x, z


def parse_latlon(text: str) -> Optional[LatLon]:
    """Any of the three lat/long forms DCS shows, plus plain decimal degrees."""
    match = _LATLON_RE.match(text)
    if match:
        g = match.groupdict()
        lat_hemi, lon_hemi = _hemispheres(g['pre'], g['post'], g['pre2'], g['post2'])
        first = (float(g['deg']), float(g['min']), float(g['sec'] or 0))
        second = (float(g['deg2']), float(g['min2']), float(g['sec2'] or 0))
        # A leading E/W on the first half means the halves are the other way round.
        if (g['pre'] or g['post'] or '').upper() in ('E', 'W'):
            first, second = second, first
        return LatLon(_signed(*first, lat_hemi), _signed(*second, lon_hemi))

    match = _COMPACT_RE.match(text)
    if match:
        g = match.groupdict()
        letters = (g['pre'], g['post'], g['pre2'], g['post2'])
        # A hemisphere letter is required here. Without one, two run-together
        # numbers are just two numbers, and guessing at them would hijack
        # anything else a user might paste.
        if any(letters):
            lat_hemi, lon_hemi = _hemispheres(*letters)
            first = _split_compact(g['first'])
            second = _split_compact(g['second'])
            if (g['pre'] or g['post'] or '').upper() in ('E', 'W'):
                first, second = second, first
            return LatLon(_signed(*first, lat_hemi), _signed(*second, lon_hemi))

    match = _DECIMAL_RE.match(text)
    if match:
        lat_v, lat_h, lon_v, lon_h = match.groups()
        lat, lon = float(lat_v), float(lon_v)
        lat_hemi, lon_hemi = _hemispheres(lat_h, lon_h)
        if lat_hemi == 'S':
            lat = -abs(lat)
        if lon_hemi == 'W':
            lon = -abs(lon)
        return LatLon(lat, lon)
    return None


def validate(point: LatLon) -> LatLon:
    if not -90.0 <= point.lat <= 90.0:
        raise CoordError(f"latitude {point.lat} is outside -90..90")
    if not -180.0 <= point.lon <= 180.0:
        raise CoordError(f"longitude {point.lon} is outside -180..180")
    return point


# ------------------------------------------------------------------ formatting

def to_dms(point: LatLon, precision: int = 2) -> str:
    """N 49°26'01.48"  E 8°13'06.09" -- precision 0 gives DCS's 'standard'."""
    def part(value: float, positive: str, negative: str, deg_width: int) -> str:
        hemi = _hemi(value, positive, negative)
        value = abs(value)
        deg = int(value)
        minutes_full = (value - deg) * 60
        minutes = int(minutes_full)
        seconds = _truncate((minutes_full - minutes) * 60, precision)
        if precision:
            width = 3 + precision
            return f"{hemi} {deg:0{deg_width}d}°{minutes:02d}'{seconds:0{width}.{precision}f}\""
        return f"{hemi} {deg:0{deg_width}d}°{minutes:02d}'{int(seconds):02d}\""

    return f"{part(point.lat, 'N', 'S', 2)}  {part(point.lon, 'E', 'W', 1)}"


def to_ddm(point: LatLon, precision: int = 3) -> str:
    """N 49°26.024'  E 8°13.101'"""
    def part(value: float, positive: str, negative: str, deg_width: int) -> str:
        hemi = _hemi(value, positive, negative)
        value = abs(value)
        deg = int(value)
        minutes = _truncate((value - deg) * 60, precision)
        width = 3 + precision
        return f"{hemi} {deg:0{deg_width}d}°{minutes:0{width}.{precision}f}'"

    return f"{part(point.lat, 'N', 'S', 2)}  {part(point.lon, 'E', 'W', 1)}"


def to_dd(point: LatLon, precision: int = 6) -> str:
    """49.433744, 8.218358 -- what most outside tools want."""
    return f"{point.lat:.{precision}f}, {point.lon:.{precision}f}"


def to_metric(x: float, z: float) -> str:
    """
    DCS writes these zero-padded to eight digits, after a hyphen separator --
    so a negative axis carries two hyphens. See _METRIC_RE.
    """
    def part(label: str, value: float) -> str:
        sign = '-' if value < 0 else ''
        return f"{label}-{sign}{abs(int(round(value))):08d}"

    return f"{part('X', x)} {part('Z', z)}"


def format_altitude(metres: float) -> str:
    return f"{int(round(metres))} m / {int(round(metres * 3.280839895))} feet"
