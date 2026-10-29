"""
Pure logic for the Battleground bridge: marker command parsing, mission drawing
conversion, and the shape of a bg_geometry2 payload.

Nothing here imports discord, psycopg or DCSServerBot, so all of it is testable
without a bot, a database or DCS. The parts that genuinely need those live in
store.py and commands.py.

The payload shape is not invented. It is taken from DCSBattleground's own Go
structs at commit b3d6aed -- `DataJson`/`Author`/`Fields` in server/session.go --
and cross-checked against what the stock battleground2 plugin writes. Getting a
key name wrong here produces a row the website silently ignores rather than an
error, so the names are pinned in ONE place (FIELD_DEFAULTS) and never spelled
out again.
"""
import math
import re

from dataclasses import dataclass, field
from typing import Any, Optional, Union

# ----------------------------------------------------------------- limits
#
# The Lua transport truncates somewhere around 2,300 characters. Truncation is
# worse than rejection: a silently shortened title looks like it worked. So the
# parser refuses anything over INPUT_MAX and says so, well under that ceiling.
TITLE_MAX = 160
DESCRIPTION_MAX = 1800
INPUT_MAX = 2200

# bg_geometry2.id is a SERIAL (int4), so this is the real ceiling, not a guess.
ID_MIN = 1
ID_MAX = 2147483647

# DCSBattleground's own delete path guards on `geo.Id > 10000` (server/http.go),
# so bridge IDs must sit above that to be deletable through the website too.
BRIDGE_ID_START = 1_000_000

# The vocabulary the frontend actually switches on (src/, commit b3d6aed).
# Anything outside this list renders as nothing at all.
GEOMETRY_TYPES = frozenset({
    'border', 'circle', 'line', 'markpoint', 'quest', 'recon', 'threat',
    'warning', 'waypoints', 'zone',
})
SIDES = frozenset({'blue', 'red'})

#: Every key in Fields, with the zero value the website expects. Building a
#: payload from this (rather than writing a partial dict) means a missing key
#: can't reach the database and confuse the Go unmarshaller.
FIELD_DEFAULTS: dict[str, Any] = {
    'posMGRS': '',
    'posPoint': [],
    'position_type': 'MGRS',
    'screenshot': [],
    'description': [],
    'side': 'blue',
    'status': 'Shared',
    'clickable': True,
    'hidden': False,
    'color': '#e4e70a',
    'subType': '',
    'type': 'recon',
    'points': [],
    'pointNames': [],
    'pointGroundFt': [],
    'pointGroundFtSet': [],
    'center': [],
    'radius': 0,
    'marker': '',
    'groundFt': 0,
    'groundFtSet': False,
}

#: Metres per degree of latitude. Longitude is this scaled by cos(latitude).
#: Used only to turn a rectangle's corner offsets into lat/lon -- see
#: _rect_corners for why that approximation is acceptable there and nowhere else.
METRES_PER_DEGREE = 111320.0

#: Names DCS generates when nobody has renamed the drawing: Polygon-2-1-1-1-1,
#: Line-25, Icon-2-2-2-1, Label-3. On a busy mission these cover the map in
#: strings that tell a pilot nothing, so they are imported without a label.
#:
#: The trailing (-\d+)* is what makes this safe. It matches only the untouched
#: default shape, so anything a human actually typed -- "Line of Death",
#: "Polygon Alpha", "TRAINING AREA" -- keeps its name. Renaming a drawing in the
#: Mission Editor is still how you give it a label on the map.
DEFAULT_UNLABELLED = r'^(Polygon|Line|Label|Icon|TextBox)(-\d+)*$'


def is_default_name(name: str, pattern: str = DEFAULT_UNLABELLED) -> bool:
    """True when DCS named this drawing and nobody has since."""
    if not name or not pattern:
        return False
    try:
        return re.match(pattern, name.strip(), re.IGNORECASE) is not None
    except re.error:
        # A broken pattern in the config must not take the whole import down.
        return False


#: Fields the F10 marker owns. On an update, everything else in an existing row
#: is left exactly as the website left it -- that is what preserves notes,
#: screenshots and colour changes made in the browser.
MARKER_OWNED_FIELDS = ('posMGRS', 'posPoint', 'position_type', 'center')


# ------------------------------------------------------- marker commands

@dataclass(frozen=True)
class IntelUpsert:
    """`-intel: Title` plus any following lines as the initial description."""
    title: str
    description: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeleteThisMarker:
    """`-intel delete` -- removes whatever this marker created."""


@dataclass(frozen=True)
class DeleteById:
    """`-intel delete 1000000` -- can be typed on any marker."""
    intel_id: int


@dataclass(frozen=True)
class ParseError:
    """Recognised as ours, but unusable. The reason is shown to the player."""
    reason: str


MarkerCommand = Union[IntelUpsert, DeleteThisMarker, DeleteById, ParseError]

#: `-intel` by default. The prefix comes from the bot's own
#: `chat_command_prefix` (core/listener.py reads the same key), because a server
#: that has changed it would otherwise find this plugin's commands unreachable
#: while every other chat command works.
KEYWORD = 'intel'


def _command_pattern(prefix: str) -> re.Pattern:
    return re.compile(rf'^\s*{re.escape(prefix)}{KEYWORD}\b(?P<rest>.*)$',
                      re.IGNORECASE | re.DOTALL)


def parse_marker_command(text: str, prefix: str = '-') -> Optional[MarkerCommand]:
    """
    Reads a marker's text.

    Returns None when the text is not a bridge command at all -- which is the
    overwhelmingly common case, since players use F10 markers for everything.
    Returns ParseError when it IS ours but malformed, so the player gets told
    rather than being left wondering.
    """
    if not text:
        return None
    match = _command_pattern(prefix).match(text)
    if not match:
        return None

    if len(text) > INPUT_MAX:
        return ParseError(f"that command is {len(text)} characters; the limit is {INPUT_MAX}")

    rest = match.group('rest')
    first_line, _, following = rest.partition('\n')
    first_line = first_line.strip()

    # -intel delete [id]
    delete = re.match(r'^delete\s*(?P<id>\S+)?$', first_line, re.IGNORECASE)
    if delete:
        raw_id = delete.group('id')
        if raw_id is None:
            return DeleteThisMarker()
        if not raw_id.isdigit():
            return ParseError(f"`{raw_id}` is not an intel ID")
        intel_id = int(raw_id)
        if not ID_MIN <= intel_id <= ID_MAX:
            return ParseError(f"intel ID {intel_id} is out of range")
        return DeleteById(intel_id)

    # -intel: Title
    if not first_line.startswith(':'):
        return ParseError(f"use `{prefix}{KEYWORD}: <title>` or `{prefix}{KEYWORD} delete [id]`")

    title = first_line[1:].strip()
    if not title:
        return ParseError("that report needs a title")
    if len(title) > TITLE_MAX:
        return ParseError(f"that title is {len(title)} characters; the limit is {TITLE_MAX}")

    description = [line.rstrip() for line in following.split('\n')] if following else []
    while description and not description[-1]:
        description.pop()
    joined = '\n'.join(description)
    if len(joined) > DESCRIPTION_MAX:
        return ParseError(
            f"that description is {len(joined)} characters; the limit is {DESCRIPTION_MAX}")

    return IntelUpsert(title=title, description=description)


# --------------------------------------------------------- recon payloads

def build_recon_payload(title: str, author_name: str, mgrs: str, lat: float, lon: float,
                        description: list[str], intel_id: int, reported_at: str,
                        side: str = 'blue', author_icon: str = '') -> dict:
    """
    A native Battleground recon record.

    posPoint carries [lat, lon] in Leaflet's order, which is what the website's
    own editor writes. Of everything in this plugin that is the one value worth
    checking against the game on a live server, because a swapped pair puts the
    report somewhere plausible rather than somewhere obviously wrong.
    """
    fields = dict(FIELD_DEFAULTS)
    fields.update({
        'posMGRS': mgrs,
        'posPoint': [lat, lon],
        'position_type': 'MGRS',
        'side': side if side in SIDES else 'blue',
        'type': 'recon',
        'description': _initial_description(description, intel_id, author_name, reported_at),
    })
    return {
        'title': title,
        'author': {'name': author_name, 'icon_url': author_icon},
        'fields': fields,
    }


def _initial_description(lines: list[str], intel_id: int, author_name: str,
                         reported_at: str) -> list[str]:
    """
    `description` is an array of strings, not a string -- so the provenance line
    is its own entry and the player's own lines stay separate and editable.
    """
    header = f"Intel #{intel_id} - reported by {author_name} at {reported_at}"
    return [header] + [line for line in lines if line]


def merge_marker_update(existing: dict, title: str, mgrs: str, lat: float,
                        lon: float) -> dict:
    """
    Apply a marker edit to a row the website may have enriched since.

    Only the title and the position are taken from DCS. Descriptions,
    screenshots, colour and status are whatever the browser last wrote, which is
    the explicit requirement: editing a marker must never wipe someone's notes.

    The incoming `existing` is the row as stored, so an unknown future field
    added by a later Battleground version survives untouched rather than being
    dropped by a rebuild from defaults.
    """
    merged = dict(existing)
    merged['title'] = title

    fields = dict(FIELD_DEFAULTS)
    fields.update(merged.get('fields') or {})
    fields['posMGRS'] = mgrs
    fields['posPoint'] = [lat, lon]
    fields['position_type'] = 'MGRS'
    merged['fields'] = fields
    return merged


# ------------------------------------------------------- drawing import

@dataclass(frozen=True)
class Converted:
    kind: str
    payload: dict
    approximated: Optional[str] = None
    #: The drawing's own name, kept even when the map label is blanked, so the
    #: import summary and bgb_items still say which shape this was.
    name: str = ''


@dataclass(frozen=True)
class Unsupported:
    name: str
    primitive: str
    reason: str


def convert_drawing(drawing: dict, author_name: str, side: str = 'blue',
                    unlabelled_pattern: str = DEFAULT_UNLABELLED) -> Union[
        Converted, Unsupported]:
    """
    Turn one entry from the mission plugin's `getMissionDrawings` into a
    Battleground geometry payload.

    That interface already converts to lat/lng with Terrain.convertMetersToLatLon
    and gives absolute `points`, so there is no coordinate maths to get wrong
    here and no custom Lua needed for this half of the plugin.

    Anything this cannot represent comes back as Unsupported with a reason, to
    be counted and named in the import summary. Inventing geometry for a shape
    DCS did not export is worse than skipping it: a wrong line on a map gets
    briefed as if it were real.
    """
    name = str(drawing.get('name') or 'Unnamed')
    # Blank on the map, not in our records: see DEFAULT_UNLABELLED.
    label = '' if is_default_name(name, unlabelled_pattern) else name
    primitive = str(drawing.get('primitiveType') or 'Unknown')
    points = _latlon_list(drawing.get('points'))
    location = drawing.get('location') or {}
    centre = ([location['lat'], location['lng']]
              if isinstance(location, dict) and 'lat' in location and 'lng' in location
              else None)

    colour = parse_dcs_colour(drawing.get('colorString')) or FIELD_DEFAULTS['color']

    def payload(kind: str, **extra) -> dict:
        fields = dict(FIELD_DEFAULTS)
        fields.update({'type': kind, 'side': side, 'clickable': True, 'color': colour})
        fields.update(extra)
        return {'title': label, 'author': {'name': author_name, 'icon_url': ''},
                'fields': fields}

    if primitive == 'Line':
        if len(points) < 2:
            return Unsupported(name, primitive, 'no exported vertices')
        closed = bool(drawing.get('closed'))
        kind = 'zone' if closed else 'line'
        approximated = None
        # DCS can draw segmented and curved line modes. The vertices are what
        # the mission exports, so a curve arrives as its control points and is
        # drawn straight between them.
        if str(drawing.get('lineMode') or '').lower() not in ('', 'segments', 'segmented'):
            approximated = f"lineMode '{drawing.get('lineMode')}' drawn as straight segments"
        return Converted(kind, payload(kind, points=points), approximated, name)

    if primitive == 'Polygon':
        mode = str(drawing.get('polygonMode') or '').lower()
        radius = _number(drawing.get('radius'))
        if mode == 'circle' and centre and radius:
            return Converted('circle', payload('circle', center=centre, radius=radius),
                             None, name)
        if len(points) >= 3:
            approximated = (f"polygonMode '{mode}' approximated from its vertices"
                            if mode in ('rect', 'oval', 'arrow') else None)
            return Converted('zone', payload('zone', points=points), approximated, name)

        width, height = _number(drawing.get('width')), _number(drawing.get('height'))
        if mode == 'rect' and centre and width and height:
            corners = _rect_corners(centre, width, height, _number(drawing.get('angle')))
            return Converted('zone', payload('zone', points=corners),
                             'rect corners derived from width, height and angle', name)

        # oval and arrow are described by r1/r2/length rather than width/height,
        # and neither appears on a Common layer in any mission seen so far. They
        # stay skipped and named rather than guessed at.
        return Unsupported(name, primitive,
                           f"polygonMode '{mode}' has no exported vertices")

    if primitive in ('TextBox', 'Icon'):
        if not centre:
            return Unsupported(name, primitive, 'no origin')
        # A text box's own text is what a human wrote, so it always wins. With
        # no text, fall back to the label -- which is blank for a drawing DCS
        # named itself, leaving an unlabelled point rather than 'Icon-2-2-2-1'.
        text = str(drawing.get('text') or '').strip()
        fields_extra: dict[str, Any] = {'posPoint': centre, 'position_type': 'POINT'}
        converted = payload('markpoint', **fields_extra)
        converted['title'] = (text or label)[:TITLE_MAX]
        note = ('text box rendered as a labelled point'
                if primitive == 'TextBox' else 'icon rendered as a labelled point')
        return Converted('markpoint', converted, note, name)

    return Unsupported(name, primitive, 'unsupported primitive type')


# --------------------------------------------------------- threat rings
#
# Seeded from the rings already drawn by hand on the JSH campaign map, so the
# generated ones match what the GMs were producing manually rather than
# introducing a second, competing set of numbers. These are a doctrine choice,
# not a physical constant -- they belong in the config and are meant to be
# edited there.
DEFAULT_THREAT_SYSTEMS: dict[str, int] = {
    'SA-5': 297100,
    'SA-10': 119265,
    'SA-11': 51275,
}
DEFAULT_RING_COLOUR = '#ff0000'
DEFAULT_RING_LABEL = '{name} Range'
#: A point already describing a ring must not acquire a ring of its own. This
#: has to cover the wording rings are actually labelled with -- 'coverage' is
#: here because friendly rings are labelled '<name> Coverage', and without it a
#: mission label reading 'Patriot Coverage' would be given a ring.
DEFAULT_RING_SKIP = r'\b(range|ring|coverage|envelope|umbrella|mez|wez)\b'


def _system_pattern(system: str) -> re.Pattern:
    """
    Match a system designation the way people actually type it.

    `SA-10` has to match `SA10` and `SA 10`, and must NOT match inside `SA-100`
    or be matched by a shorter `SA-1` -- which is why the trailing lookahead
    excludes digits rather than relying on \\b.
    """
    body = re.sub(r'[^A-Za-z0-9]+', r'[-\\s]?', system.strip())
    return re.compile(rf'(?<![A-Za-z0-9]){body}(?![0-9])', re.IGNORECASE)


@dataclass(frozen=True)
class ThreatRing:
    """A matched system and everything needed to draw its ring."""
    system: str
    radius: float
    colour: str
    label: str

    @property
    def title(self) -> str:
        return self.label.format(name=self.system)


def match_threat_system(title: str, systems: dict[str, Any],
                        skip_pattern: str = DEFAULT_RING_SKIP,
                        colour: str = DEFAULT_RING_COLOUR,
                        label: str = DEFAULT_RING_LABEL) -> Optional[ThreatRing]:
    """
    The threat system a title names, with its radius, colour and label.

    A system's value is either a plain radius in metres, or a mapping that
    overrides the colour and label as well -- which is how friendly coverage is
    told apart from enemy threat on the map:

        SA-10: 119265
        Patriot:
          radius: 160000
          colour: "#0068ff"
          label: "{name} Coverage"

    Longest designation first, so `SA-10` wins over `SA-1` when a config holds
    both. Returns None when nothing matches, or when the title already
    describes a ring.

    Note that the override is the COLOUR, never the side. Every ring is written
    to the same coalition as the rest of this server's geometry; a ring written
    to the other side would simply be invisible to the pilots who need it.
    """
    if not title:
        return None
    if skip_pattern and re.search(skip_pattern, title, re.IGNORECASE):
        return None

    for system in sorted(systems, key=len, reverse=True):
        setting = systems[system]
        if isinstance(setting, dict):
            radius = _number(setting.get('radius'))
            system_colour = setting.get('colour') or setting.get('color') or colour
            system_label = setting.get('label') or label
        else:
            radius = _number(setting)
            system_colour, system_label = colour, label
        if radius > 0 and _system_pattern(system).search(title):
            return ThreatRing(system, radius, str(system_colour), str(system_label))
    return None


def build_ring_payload(ring: ThreatRing, centre: list[float], author_name: str,
                       side: str = 'blue') -> dict:
    """A circle of `ring.radius` metres around `centre`, in Battleground's shape."""
    fields = dict(FIELD_DEFAULTS)
    fields.update({
        'type': 'circle',
        'side': side if side in SIDES else 'blue',
        'center': [centre[0], centre[1]],
        'radius': ring.radius,
        'color': ring.colour,
        'clickable': True,
    })
    return {'title': ring.title,
            'author': {'name': author_name, 'icon_url': ''},
            'fields': fields}


#: DCS writes a drawing's colour as 0xRRGGBBAA. Confirmed against a real
#: mission rather than assumed: 0xff0000ff and 0x0000ffff dominate its Common
#: layer, which read as opaque red and opaque blue, and the outline-only boxes
#: carry fillColorString 0xff000000 -- red at zero alpha. Any other byte order
#: would make those nonsense.
_COLOUR_RE = re.compile(r'^0x([0-9a-f]{6})([0-9a-f]{2})?$', re.IGNORECASE)


def parse_dcs_colour(value: Any) -> Optional[str]:
    """
    A DCS colorString as the CSS colour Battleground expects, or None.

    The alpha byte is dropped. Battleground geometry carries a single `color`
    field used for the outline, with its own fill rules, so there is nowhere for
    a separate fill colour or an opacity to go -- and a shape whose fill is
    transparent still needs a visible outline.
    """
    if not isinstance(value, str):
        return None
    match = _COLOUR_RE.match(value.strip())
    return f"#{match.group(1).lower()}" if match else None


def _rect_corners(centre: list[float], width: float, height: float,
                  angle_degrees: float) -> list[list[float]]:
    """
    The four corners of a DCS rectangle drawing, as [lat, lon].

    DCS stores a rect as a point plus `width`, `height` and `angle`, and exports
    no vertices at all -- so unlike every other shape here, the corners have to
    be derived. Three things had to be settled before this could be written
    honestly, and all three were settled against a real mission file
    (Egypt_Went_Rogue 1.3.8, 50 rects on its Common layer) rather than assumed:

    - `mapX`/`mapY` is the **centre**, not a corner. Tested by counting how many
      mission units each interpretation encloses: centre put units inside 15 of
      the 65 rects, corner only 10, and enclosed 490 units against 139.
    - `height` runs north-south (map x), `width` east-west (map y). The same
      test preferred this on all rects, on elongated ones, and on very elongated
      ones -- where orientation matters most -- and it matches the plain reading
      of the words.
    - `angle` is degrees, applied anticlockwise about the centre.

    The offsets are converted to degrees with a flat local approximation rather
    than the terrain's true projection, because the only exact converter lives
    in the mission environment and these drawings arrive already converted. Over
    a box of a few kilometres that costs a couple of metres, which is why rects
    are reported as approximated. A kill box is not a survey.
    """
    latitude, longitude = centre[0], centre[1]
    half_north, half_east = height / 2.0, width / 2.0
    radians = math.radians(angle_degrees)
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    metres_per_degree_lon = METRES_PER_DEGREE * max(math.cos(math.radians(latitude)), 1e-6)

    corners: list[list[float]] = []
    for north, east in ((half_north, half_east), (half_north, -half_east),
                        (-half_north, -half_east), (-half_north, half_east)):
        # Rotate out of the rectangle's own frame into the map's.
        d_north = north * cos_a - east * sin_a
        d_east = north * sin_a + east * cos_a
        corners.append([latitude + d_north / METRES_PER_DEGREE,
                        longitude + d_east / metres_per_degree_lon])
    corners.append(list(corners[0]))     # closed ring
    return corners


def _latlon_list(value: Any) -> list[list[float]]:
    if not isinstance(value, list):
        return []
    out: list[list[float]] = []
    for item in value:
        if isinstance(item, dict) and 'lat' in item and 'lng' in item:
            out.append([float(item['lat']), float(item['lng'])])
    return out


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class ImportSummary:
    """What a drawing import actually did, for `/bgbridge status` and the log."""
    imported: int = 0
    rings: int = 0
    approximated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    removed: int = 0
    layers: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"{self.imported} imported"]
        if self.rings:
            parts.append(f"{self.rings} threat rings")
        if self.removed:
            parts.append(f"{self.removed} replaced")
        if self.approximated:
            parts.append(f"{len(self.approximated)} approximated")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        return ', '.join(parts)


#: Slots DCS gives no controllable unit for. A mark placed from one of these
#: carries NO initiator, which is why Tactical Commander and Game Master
#: reports were refused outright.
#:
#: DCS numbers them per coalition -- artillery_commander_blue_1,
#: artillery_commander_blue_2 -- so these are matched as prefixes, not as exact
#: names. An exact-name list silently matches nothing on a real server.
COMMANDER_SLOTS = frozenset({
    'instructor',
    'artillery_commander',
    'forward_observer',
    'game-master',
    'gamemaster',
    'observer',
    'spectator',
})


def is_commander_slot(unit_type, slot=None, slots=COMMANDER_SLOTS) -> bool:
    """
    True when the player occupies a slot with no controllable unit.

    Both the unit type and the slot name are checked, because DCSServerBot
    reports these differently depending on how the slot was entered and we
    only need one of them to name it.
    """
    for value in (unit_type, slot):
        name = str(value or '').strip().lower()
        if not name:
            continue
        for commander in slots:
            if name == commander or name.startswith(commander + '_'):
                return True
    return False
