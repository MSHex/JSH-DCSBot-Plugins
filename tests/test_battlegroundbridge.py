"""
Tests for the bridge's pure logic: marker parsing, payload merging and mission
drawing conversion.

    python -m pytest -q tests/test_battlegroundbridge.py

The SQL is covered separately by tests/bgb_sql_check.py, which needs a real
PostgreSQL instance. Neither suite touches DCS or DCSBattleground.
"""
import math
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins' / 'battlegroundbridge'))

import domain as d  # noqa: E402


class TestNotOurs:
    """By far the commonest case: markers people use for ordinary things."""

    @pytest.mark.parametrize('text', [
        'SAM here', '', 'BRAA 270/20', 'intel: no prefix', '-intelligence report',
        '-csar pickup', 'delete', '--intel: double prefix',
    ])
    def test_ignored(self, text):
        assert d.parse_marker_command(text) is None


class TestCreate:
    def test_title_only(self):
        result = d.parse_marker_command('-intel: Suspected SAM')
        assert result == d.IntelUpsert(title='Suspected SAM', description=[])

    def test_multiline_description(self):
        result = d.parse_marker_command(
            '-intel: Suspected SAM\nTwo launchers near the northern road.\nRadar appears active.')
        assert result.title == 'Suspected SAM'
        assert result.description == ['Two launchers near the northern road.',
                                      'Radar appears active.']

    def test_trailing_blank_lines_dropped(self):
        result = d.parse_marker_command('-intel: Thing\nline\n\n\n')
        assert result.description == ['line']

    def test_case_and_spacing(self):
        assert d.parse_marker_command('  -INTEL:   Spaced  ').title == 'Spaced'

    def test_colon_is_required(self):
        result = d.parse_marker_command('-intel Suspected SAM')
        assert isinstance(result, d.ParseError)

    def test_empty_title_rejected(self):
        assert isinstance(d.parse_marker_command('-intel:'), d.ParseError)
        assert isinstance(d.parse_marker_command('-intel:   '), d.ParseError)

    def test_configured_prefix_is_honoured(self):
        """A server that changed chat_command_prefix must still reach this."""
        assert d.parse_marker_command('!intel: Thing', prefix='!').title == 'Thing'
        assert d.parse_marker_command('-intel: Thing', prefix='!') is None


class TestLimits:
    """
    Oversized input is rejected, never truncated. A silently shortened title
    looks like it worked, and the Lua transport cuts off around 2,300 bytes.
    """

    def test_title_too_long(self):
        result = d.parse_marker_command('-intel: ' + 'x' * (d.TITLE_MAX + 1))
        assert isinstance(result, d.ParseError)
        assert str(d.TITLE_MAX) in result.reason

    def test_title_at_the_limit_is_fine(self):
        assert d.parse_marker_command('-intel: ' + 'x' * d.TITLE_MAX).title

    def test_description_too_long(self):
        result = d.parse_marker_command('-intel: ok\n' + 'y' * (d.DESCRIPTION_MAX + 1))
        assert isinstance(result, d.ParseError)

    def test_whole_input_too_long(self):
        result = d.parse_marker_command('-intel: ok\n' + 'z' * d.INPUT_MAX)
        assert isinstance(result, d.ParseError)
        assert str(d.INPUT_MAX) in result.reason


class TestDelete:
    def test_delete_this_marker(self):
        assert d.parse_marker_command('-intel delete') == d.DeleteThisMarker()

    def test_delete_by_id(self):
        assert d.parse_marker_command('-intel delete 1000000') == d.DeleteById(1000000)

    def test_non_numeric_id(self):
        result = d.parse_marker_command('-intel delete abc')
        assert isinstance(result, d.ParseError)

    @pytest.mark.parametrize('value', ['0', '2147483648', '99999999999'])
    def test_out_of_range(self, value):
        result = d.parse_marker_command(f'-intel delete {value}')
        assert isinstance(result, d.ParseError)

    def test_negative_is_not_an_id(self):
        assert isinstance(d.parse_marker_command('-intel delete -5'), d.ParseError)


class TestPayload:
    def test_every_field_the_website_expects_is_present(self):
        payload = d.build_recon_payload('Suspected SAM', 'Curse', '38TLN0274366889',
                                        42.0, 42.5, [], 1000000, '2026-10-20 18:00Z')
        assert set(payload['fields']) == set(d.FIELD_DEFAULTS)
        assert payload['fields']['type'] == 'recon'
        assert payload['fields']['side'] == 'blue'
        assert payload['title'] == 'Suspected SAM'
        assert payload['author']['name'] == 'Curse'

    def test_description_is_a_list_with_provenance_first(self):
        payload = d.build_recon_payload('T', 'Curse', 'M', 1.0, 2.0,
                                        ['two launchers'], 1000000, '2026-10-20 18:00Z')
        description = payload['fields']['description']
        assert description[0].startswith('Intel #1000000 - reported by Curse')
        assert description[1] == 'two launchers'

    def test_unknown_side_falls_back_to_blue(self):
        payload = d.build_recon_payload('T', 'A', 'M', 1.0, 2.0, [], 1, 't', side='purple')
        assert payload['fields']['side'] == 'blue'


class TestMergePreservesWebsiteWork:
    """
    The explicit requirement: editing a marker updates the title and position
    and must not wipe anything added in the browser.
    """

    def existing(self):
        return {
            'title': 'Suspected SAM',
            'author': {'name': 'Curse', 'icon_url': 'http://a/i.png'},
            'fields': dict(d.FIELD_DEFAULTS, **{
                'type': 'recon', 'side': 'blue',
                'description': ['Intel #1000000', 'web note added later'],
                'screenshot': ['http://img/1.png'],
                'color': '#ff0000', 'status': 'Confirmed',
            }),
        }

    def test_title_and_position_change(self):
        merged = d.merge_marker_update(self.existing(), 'Confirmed SA-11', 'NEWMGRS', 9.0, 8.0)
        assert merged['title'] == 'Confirmed SA-11'
        assert merged['fields']['posMGRS'] == 'NEWMGRS'
        assert merged['fields']['posPoint'] == [9.0, 8.0]

    @pytest.mark.parametrize('key,expected', [
        ('description', ['Intel #1000000', 'web note added later']),
        ('screenshot', ['http://img/1.png']),
        ('color', '#ff0000'),
        ('status', 'Confirmed'),
    ])
    def test_website_fields_survive(self, key, expected):
        merged = d.merge_marker_update(self.existing(), 'New', 'M', 1.0, 2.0)
        assert merged['fields'][key] == expected

    def test_author_is_not_rewritten(self):
        merged = d.merge_marker_update(self.existing(), 'New', 'M', 1.0, 2.0)
        assert merged['author']['name'] == 'Curse'

    def test_unknown_future_fields_survive(self):
        """A later Battleground version may add keys. Dropping them is a bug."""
        existing = self.existing()
        existing['fields']['somethingNew'] = {'a': 1}
        existing['unexpectedTopLevel'] = 'keep me'
        merged = d.merge_marker_update(existing, 'New', 'M', 1.0, 2.0)
        assert merged['fields']['somethingNew'] == {'a': 1}
        assert merged['unexpectedTopLevel'] == 'keep me'


def line(points, **extra):
    return dict({'name': 'FEBA', 'primitiveType': 'Line',
                 'location': {'lat': 1.0, 'lng': 2.0},
                 'points': [{'lat': lat, 'lng': lng} for lat, lng in points]}, **extra)


class TestDrawingConversion:
    def test_open_line(self):
        result = d.convert_drawing(line([(1, 2), (3, 4)]), 'JSH')
        assert isinstance(result, d.Converted) and result.kind == 'line'
        assert result.payload['fields']['points'] == [[1.0, 2.0], [3.0, 4.0]]

    def test_closed_line_becomes_a_zone(self):
        result = d.convert_drawing(line([(1, 2), (3, 4), (5, 6)], closed=True), 'JSH')
        assert result.kind == 'zone'

    def test_curved_line_is_flagged_as_approximated(self):
        result = d.convert_drawing(line([(1, 2), (3, 4)], lineMode='curve'), 'JSH')
        assert result.kind == 'line'
        assert result.approximated and 'curve' in result.approximated

    def test_line_without_vertices_is_skipped(self):
        result = d.convert_drawing(line([]), 'JSH')
        assert isinstance(result, d.Unsupported)
        assert 'vertices' in result.reason

    def test_circle(self):
        result = d.convert_drawing({'name': 'Threat ring', 'primitiveType': 'Polygon',
                                    'polygonMode': 'circle', 'radius': 25000,
                                    'location': {'lat': 1.5, 'lng': 2.5}}, 'JSH')
        assert result.kind == 'circle'
        assert result.payload['fields']['center'] == [1.5, 2.5]
        assert result.payload['fields']['radius'] == 25000.0

    def test_polygon_with_vertices(self):
        result = d.convert_drawing({'name': 'Kill box', 'primitiveType': 'Polygon',
                                    'polygonMode': 'free', 'location': {'lat': 1.0, 'lng': 2.0},
                                    'points': [{'lat': 1, 'lng': 2}, {'lat': 3, 'lng': 4},
                                               {'lat': 5, 'lng': 6}]}, 'JSH')
        assert result.kind == 'zone'
        assert result.approximated is None

    def test_rectangle_without_vertices_is_derived(self):
        result = d.convert_drawing({'name': 'Box', 'primitiveType': 'Polygon',
                                    'polygonMode': 'rect', 'width': 1000, 'height': 500,
                                    'angle': 0, 'location': {'lat': 30.0, 'lng': 32.0}}, 'JSH')
        assert isinstance(result, d.Converted)
        assert result.kind == 'zone'
        assert result.approximated and 'derived' in result.approximated
        assert len(result.payload['fields']['points']) == 5      # closed ring

    def test_oval_is_still_skipped(self):
        """r1/r2/length, not width/height. Not guessed at."""
        result = d.convert_drawing({'name': 'Oval', 'primitiveType': 'Polygon',
                                    'polygonMode': 'oval', 'r1': 100, 'r2': 50,
                                    'location': {'lat': 30.0, 'lng': 32.0}}, 'JSH')
        assert isinstance(result, d.Unsupported)

    def test_rect_without_dimensions_is_skipped(self):
        result = d.convert_drawing({'name': 'Box', 'primitiveType': 'Polygon',
                                    'polygonMode': 'rect',
                                    'location': {'lat': 30.0, 'lng': 32.0}}, 'JSH')
        assert isinstance(result, d.Unsupported)


class TestRectGeometry:
    """
    The derived corners have to come back out at the size DCS put in.

    Values are from Egypt_Went_Rogue 1.3.8, drawing `Polygon-2-1-1-1-1-1`.
    """
    WIDTH, HEIGHT, ANGLE = 3822.281059557, 2805.4860938688, 356.0
    CENTRE = [30.0, 32.0]

    @staticmethod
    def metres_between(a, b, latitude=30.0):
        north = (b[0] - a[0]) * d.METRES_PER_DEGREE
        east = (b[1] - a[1]) * d.METRES_PER_DEGREE * math.cos(math.radians(latitude))
        return math.hypot(north, east)

    def corners(self, angle=None):
        return d._rect_corners(self.CENTRE, self.WIDTH, self.HEIGHT,
                               self.ANGLE if angle is None else angle)

    def test_ring_is_closed(self):
        corners = self.corners()
        assert len(corners) == 5
        assert corners[0] == corners[-1]

    def test_sides_match_the_stored_dimensions(self):
        corners = self.corners()
        # corner order is (+N+E), (+N-E), (-N-E), (-N+E)
        assert self.metres_between(corners[0], corners[1]) == pytest.approx(self.WIDTH, rel=1e-3)
        assert self.metres_between(corners[1], corners[2]) == pytest.approx(self.HEIGHT, rel=1e-3)
        assert self.metres_between(corners[2], corners[3]) == pytest.approx(self.WIDTH, rel=1e-3)

    def test_diagonals_match(self):
        corners = self.corners()
        expected = math.hypot(self.WIDTH, self.HEIGHT)
        assert self.metres_between(corners[0], corners[2]) == pytest.approx(expected, rel=1e-3)
        assert self.metres_between(corners[1], corners[3]) == pytest.approx(expected, rel=1e-3)

    def test_rotation_preserves_size(self):
        """Turning the box must not stretch it."""
        for angle in (0, 45, 90, 180, 356):
            corners = self.corners(angle)
            assert self.metres_between(corners[0], corners[1]) == pytest.approx(
                self.WIDTH, rel=1e-3)
            assert self.metres_between(corners[1], corners[2]) == pytest.approx(
                self.HEIGHT, rel=1e-3)

    def test_unrotated_box_is_axis_aligned(self):
        """height runs north-south, width east-west -- the settled convention."""
        corners = self.corners(0)
        assert corners[0][0] == pytest.approx(corners[1][0])      # same latitude
        assert corners[0][1] == pytest.approx(corners[3][1])      # same longitude
        north_south = self.metres_between(corners[0], corners[3])
        east_west = self.metres_between(corners[0], corners[1])
        assert north_south == pytest.approx(self.HEIGHT, rel=1e-3)
        assert east_west == pytest.approx(self.WIDTH, rel=1e-3)

    def test_centred_not_cornered(self):
        """The centre must sit in the middle, not on an edge."""
        corners = self.corners(0)
        mean_lat = sum(c[0] for c in corners[:4]) / 4
        mean_lon = sum(c[1] for c in corners[:4]) / 4
        assert mean_lat == pytest.approx(self.CENTRE[0], abs=1e-9)
        assert mean_lon == pytest.approx(self.CENTRE[1], abs=1e-9)

    def test_rectangle_with_vertices_is_approximated(self):
        result = d.convert_drawing({'name': 'Box', 'primitiveType': 'Polygon',
                                    'polygonMode': 'rect', 'location': {'lat': 1.0, 'lng': 2.0},
                                    'points': [{'lat': 1, 'lng': 2}, {'lat': 1, 'lng': 4},
                                               {'lat': 3, 'lng': 4}, {'lat': 3, 'lng': 2}]}, 'JSH')
        assert result.kind == 'zone'
        assert result.approximated and 'rect' in result.approximated

    def test_text_box_becomes_a_labelled_point(self):
        result = d.convert_drawing({'name': 'label1', 'primitiveType': 'TextBox',
                                    'text': 'FARP ALPHA',
                                    'location': {'lat': 1.0, 'lng': 2.0}}, 'JSH')
        assert result.kind == 'markpoint'
        assert result.payload['title'] == 'FARP ALPHA'
        assert result.payload['fields']['posPoint'] == [1.0, 2.0]

    def test_text_box_falls_back_to_its_name(self):
        result = d.convert_drawing({'name': 'label1', 'primitiveType': 'TextBox', 'text': '  ',
                                    'location': {'lat': 1.0, 'lng': 2.0}}, 'JSH')
        assert result.payload['title'] == 'label1'

    def test_icon(self):
        result = d.convert_drawing({'name': 'Hostile', 'primitiveType': 'Icon',
                                    'file': 'x.png', 'location': {'lat': 1.0, 'lng': 2.0}}, 'JSH')
        assert result.kind == 'markpoint'
        assert result.approximated

    def test_unknown_primitive(self):
        result = d.convert_drawing({'name': 'Mystery', 'primitiveType': 'Hologram'}, 'JSH')
        assert isinstance(result, d.Unsupported)

    def test_converted_types_are_all_in_the_frontend_vocabulary(self):
        """A type outside this set renders as nothing at all, silently."""
        for drawing in [line([(1, 2), (3, 4)]),
                        line([(1, 2), (3, 4)], closed=True),
                        {'name': 'c', 'primitiveType': 'Polygon', 'polygonMode': 'circle',
                         'radius': 1, 'location': {'lat': 1.0, 'lng': 2.0}},
                        {'name': 't', 'primitiveType': 'TextBox', 'text': 'x',
                         'location': {'lat': 1.0, 'lng': 2.0}}]:
            result = d.convert_drawing(drawing, 'JSH')
            assert result.kind in d.GEOMETRY_TYPES


class TestColours:
    """
    DCS writes 0xRRGGBBAA. The real campaign mission's Common layer is almost
    entirely 0xff0000ff and 0x0000ffff -- opaque red and opaque blue -- which is
    what pins the byte order down.
    """

    @pytest.mark.parametrize('value,expected', [
        ('0xff0000ff', '#ff0000'),      # opaque red, 38 polygons in the mission
        ('0x0000ffff', '#0000ff'),      # opaque blue, 12 polygons
        ('0x0080ffff', '#0080ff'),      # light blue text box
        ('0xff000000', '#ff0000'),      # red at zero alpha: outline-only fill
        ('0xFF0000FF', '#ff0000'),      # case insensitive
        ('0xabcdef', '#abcdef'),        # alpha byte optional
        ('  0xff0000ff  ', '#ff0000'),
    ])
    def test_parses(self, value, expected):
        assert d.parse_dcs_colour(value) == expected

    @pytest.mark.parametrize('value', [
        None, '', 'red', '#ff0000', '0xff00', '0xff0000fff', 'ff0000ff', 0xff0000ff, {},
    ])
    def test_rejects(self, value):
        assert d.parse_dcs_colour(value) is None

    def test_drawing_takes_the_mission_colour(self):
        result = d.convert_drawing(line([(1, 2), (3, 4)], colorString='0x0000ffff'), 'JSH')
        assert result.payload['fields']['color'] == '#0000ff'

    def test_rect_takes_it_too(self):
        result = d.convert_drawing({'name': 'Box', 'primitiveType': 'Polygon',
                                    'polygonMode': 'rect', 'width': 1000, 'height': 500,
                                    'angle': 0, 'colorString': '0xff0000ff',
                                    'location': {'lat': 30.0, 'lng': 32.0}}, 'JSH')
        assert result.payload['fields']['color'] == '#ff0000'

    def test_missing_colour_falls_back_to_the_default(self):
        result = d.convert_drawing(line([(1, 2), (3, 4)]), 'JSH')
        assert result.payload['fields']['color'] == d.FIELD_DEFAULTS['color']

    def test_unparseable_colour_falls_back_rather_than_breaking(self):
        result = d.convert_drawing(line([(1, 2), (3, 4)], colorString='chartreuse'), 'JSH')
        assert result.payload['fields']['color'] == d.FIELD_DEFAULTS['color']

    def test_intel_is_unaffected(self):
        """Markers have no mission colour; reports keep the recon default."""
        payload = d.build_recon_payload('T', 'Curse', 'M', 1.0, 2.0, [], 1, 't')
        assert payload['fields']['color'] == d.FIELD_DEFAULTS['color']


SYSTEMS = {'SA-5': 297100, 'SA-10': 119265, 'SA-11': 51275, 'SA-1': 25000}


class TestThreatSystemMatching:
    """
    The radii are seeded from the rings JSH already drew by hand, so a generated
    ring sits on top of the manual one rather than disagreeing with it.
    """

    @pytest.mark.parametrize('title,system', [
        ('SA-10', 'SA-10'),
        ('SA-10 site', 'SA-10'),
        ('Suspected SA-10', 'SA-10'),
        ('sa-10 near the road', 'SA-10'),
        ('SA10', 'SA-10'),
        ('SA 10', 'SA-10'),
        ('sa10 DESTROYED', 'SA-10'),
        ('two SA-11 launchers', 'SA-11'),
        ('SA-5 battery', 'SA-5'),
    ])
    def test_matches(self, title, system):
        result = d.match_threat_system(title, SYSTEMS)
        assert result is not None and result.system == system
        assert result.radius == SYSTEMS[system]

    def test_longer_designation_wins(self):
        """
        The trap: with SA-1 also configured, SA-10 and SA-11 must not be
        matched as SA-1 and given a 25 km ring instead of 119 km.
        """
        assert d.match_threat_system('SA-10 site', SYSTEMS).system == 'SA-10'
        assert d.match_threat_system('SA-11 site', SYSTEMS).system == 'SA-11'
        assert d.match_threat_system('SA-1 site', SYSTEMS).system == 'SA-1'

    @pytest.mark.parametrize('title', [
        'SA-100', 'SA-1000', 'XSA-10', 'SA-12', 'convoy', '', 'Shilka',
    ])
    def test_non_matches(self, title):
        assert d.match_threat_system(title, SYSTEMS) is None

    @pytest.mark.parametrize('title', [
        'SA-10 Range', 'SA-11 RING', 'SA-5 mez', 'SA-10 WEZ',
    ])
    def test_titles_that_already_describe_a_ring_are_skipped(self, title):
        """Otherwise a hand-drawn 'SA-10 Range' acquires a ring of its own."""
        assert d.match_threat_system(title, SYSTEMS) is None

    def test_zero_or_missing_radius_does_not_match(self):
        assert d.match_threat_system('SA-10', {'SA-10': 0}) is None
        assert d.match_threat_system('SA-10', {'SA-10': 'wide'}) is None

    def test_empty_config_matches_nothing(self):
        assert d.match_threat_system('SA-10 site', {}) is None


class TestFriendlyCoverage:
    """
    A system's value may be a mapping, which is how friendly coverage gets its
    own colour and wording without needing a separate list.
    """

    MIXED = {
        'SA-10': 119265,
        'Patriot': {'radius': 160000, 'colour': '#0068ff', 'label': '{name} Coverage'},
        'Hawk': {'radius': 40000, 'color': '#0068ff'},        # US spelling accepted
    }

    def test_enemy_system_keeps_the_defaults(self):
        ring = d.match_threat_system('SA-10 site', self.MIXED)
        assert ring.colour == d.DEFAULT_RING_COLOUR
        assert ring.title == 'SA-10 Range'

    def test_friendly_system_overrides_colour_and_label(self):
        ring = d.match_threat_system('Patriot battery', self.MIXED)
        assert ring.radius == 160000
        assert ring.colour == '#0068ff'
        assert ring.title == 'Patriot Coverage'

    def test_either_spelling_of_colour(self):
        assert d.match_threat_system('Hawk site', self.MIXED).colour == '#0068ff'

    def test_label_falls_back_when_only_colour_is_given(self):
        assert d.match_threat_system('Hawk site', self.MIXED).title == 'Hawk Range'

    def test_mapping_without_a_radius_does_not_match(self):
        assert d.match_threat_system('Patriot', {'Patriot': {'colour': '#0068ff'}}) is None

    def test_global_defaults_are_overridable(self):
        ring = d.match_threat_system('SA-10', {'SA-10': 1000},
                                     colour='#00ff00', label='{name} WEZ ring')
        assert ring.colour == '#00ff00'
        assert ring.title == 'SA-10 WEZ ring'

    def test_friendly_ring_is_still_written_to_the_visible_coalition(self):
        """
        The override is the colour, never the side. A ring written to the other
        coalition would be invisible to the pilots who need it.
        """
        ring = d.match_threat_system('Patriot', self.MIXED)
        payload = d.build_ring_payload(ring, [30.0, 32.0], 'Mission', side='blue')
        assert payload['fields']['side'] == 'blue'
        assert payload['fields']['color'] == '#0068ff'


class TestRingPayload:
    def test_shape(self):
        ring = d.ThreatRing('SA-10', 119265, d.DEFAULT_RING_COLOUR, d.DEFAULT_RING_LABEL)
        payload = d.build_ring_payload(ring, [30.5, 32.5], 'Curse')
        assert payload['title'] == 'SA-10 Range'
        assert payload['fields']['type'] == 'circle'
        assert payload['fields']['center'] == [30.5, 32.5]
        assert payload['fields']['radius'] == 119265
        assert payload['fields']['side'] == 'blue'
        assert set(payload['fields']) == set(d.FIELD_DEFAULTS)

    def test_custom_label_and_colour(self):
        ring = d.ThreatRing('SA-11', 51275, '#00ff00', '{name} threat')
        payload = d.build_ring_payload(ring, [1.0, 2.0], 'M')
        assert payload['title'] == 'SA-11 threat'
        assert payload['fields']['color'] == '#00ff00'

    @pytest.mark.parametrize('label', ['{name} Range', '{name} Coverage'])
    def test_generated_ring_would_not_match_itself(self, label):
        """Neither wording may produce a ring around a ring."""
        ring = d.ThreatRing('SA-10', 119265, '#ff0000', label)
        payload = d.build_ring_payload(ring, [1.0, 2.0], 'M')
        assert d.match_threat_system(payload['title'], SYSTEMS) is None


class TestImportSummary:
    def test_describe(self):
        summary = d.ImportSummary(imported=12, removed=5, approximated=['a'], skipped=['b', 'c'])
        assert summary.describe() == '12 imported, 5 replaced, 1 approximated, 2 skipped'

    def test_describe_minimal(self):
        assert d.ImportSummary(imported=3).describe() == '3 imported'
