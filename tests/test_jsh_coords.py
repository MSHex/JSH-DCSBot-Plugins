"""
Tests for jsh_coords' parsing and formatting.

Run from the repository root: python -m pytest -q tests/test_jsh_coords.py

The reference values come from the DCS coordinate box itself -- one point shown
in all four formats at once -- so these assertions check agreement with the
game's own display, not merely self-consistency.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins' / 'jsh_coords'))

import coords  # noqa: E402

# The same point, as DCS showed it.
DMS = 'N 49°26\'01.48" E 8°13\'06.09"'
DDM = "N 49°26.024' E 8°13.101'"
DD = '49.433744, 8.218358'


class TestAgreementWithDCS:
    """All three of DCS's lat/long forms must describe one point."""

    def test_dms_matches_dd(self):
        point = coords.parse_latlon(DMS)
        assert coords.to_dd(point) == DD

    def test_dms_matches_ddm(self):
        point = coords.parse_latlon(DMS)
        assert coords.to_ddm(point) == 'N 49°26.024\'  E 8°13.101\''

    def test_dd_matches_dms_to_the_precision_dd_carries(self):
        """
        Converting the other way loses the last hundredth of a second, and that
        is arithmetic rather than a bug. DCS prints all four formats from one
        full-precision value: 49.433744 is itself truncated, and it works back
        out to 01.4784", which truncates to 01.47 -- 30 cm from the 01.48 the
        game showed. Round-tripping DD cannot recover a digit DD never had.
        """
        point = coords.parse_latlon(DD)
        assert coords.to_dms(point) == 'N 49°26\'01.47"  E 8°13\'06.08"'
        assert coords.to_dms(point, 0) == 'N 49°26\'01"  E 8°13\'06"'

    def test_ddm_agrees_to_its_own_precision(self):
        # DDM pins the point only to the nearest thousandth of a minute, about
        # 1.8 m, so DMS from it lands inside the right second but not on 01.48.
        point = coords.parse_latlon(DDM)
        assert coords.to_ddm(point) == 'N 49°26.024\'  E 8°13.101\''
        assert coords.to_dms(point).startswith('N 49°26\'01')


class TestTruncation:
    """DCS cuts rather than rounds: 13.1015' displays as 13.101'."""

    def test_cuts_not_rounds(self):
        assert coords._truncate(13.1015, 3) == 13.101
        assert coords._truncate(1.4799, 2) == 1.47

    def test_float_error_does_not_eat_a_digit(self):
        # 1.48 arrives as 1.4799999999 after a degrees round trip.
        assert coords._truncate(1.48, 2) == 1.48

    @pytest.mark.parametrize('text', [DMS, DDM, DD])
    def test_round_trip_is_stable(self, text):
        """Formatting, re-parsing and re-formatting must not drift."""
        point = coords.parse_latlon(text)
        once = coords.to_dms(point)
        twice = coords.to_dms(coords.parse_latlon(once))
        assert once == twice


class TestHemispheres:
    def test_west_survives(self):
        """A trailing W used to be swallowed by the latitude and flip to E."""
        point = coords.parse_latlon('N 00°00\'00.01" W 179°59\'59.99"')
        assert point.lon < 0
        assert coords.to_dms(point) == 'N 00°00\'00.01"  W 179°59\'59.99"'

    def test_south(self):
        point = coords.parse_latlon('S 33°52\'07.68" E 151°12\'33.48"')
        assert coords.to_dd(point) == '-33.868800, 151.209300'

    def test_trailing_letters(self):
        point = coords.parse_latlon('49.433744 N, 8.218358 E')
        assert coords.to_dd(point) == DD

    def test_halves_in_either_order(self):
        point = coords.parse_latlon('8°13\'06.09"E 49°26\'01.48"N')
        assert coords.to_dd(point) == DD

    def test_negative_decimal_without_letters(self):
        point = coords.parse_latlon('-33.8688, 151.2093')
        assert point.lat == pytest.approx(-33.8688)
        assert point.lon == pytest.approx(151.2093)


class TestLooseInput:
    @pytest.mark.parametrize('text', [
        'N49 26 01.48 E8 13 06.09',
        'N 49 26 01.48 E 8 13 06.09',
        'N 49:26:01.48 E 8:13:06.09',
    ])
    def test_separators(self, text):
        assert coords.to_dd(coords.parse_latlon(text)) == DD

    def test_degrees_and_minutes_only(self):
        point = coords.parse_latlon('N 49°26\' E 8°13\'')
        assert coords.to_ddm(point) == 'N 49°26.000\'  E 8°13.000\''


class TestCompact:
    """
    The run-together form, and the degree-digit padding different aircraft use.

    Every string here is the same point as DMS/DDM/DD above. Splitting from the
    right is what makes the padding a non-issue.
    """

    @pytest.mark.parametrize('text', [
        'N 492601.48 E 81306.09',      # as DCS's own box numbers it
        'N 492601.48 E 081306.09',     # longitude padded to two degree digits
        'N 492601.48 E 0081306.09',    # padded to three -- Hornet, Viper, A-10C
        'N492601.48 E0081306.09',      # no spaces anywhere
        '492601.48N 081306.09E',       # hemisphere letters trailing
        'N 0492601.48 E 0081306.09',   # latitude padded as well
    ])
    def test_every_padding_is_the_same_point(self, text):
        assert coords.to_dd(coords.parse_latlon(text)) == DD

    def test_splits_from_the_right(self):
        assert coords._split_compact('81306.09') == (8, 13, pytest.approx(6.09))
        assert coords._split_compact('081306.09') == (8, 13, pytest.approx(6.09))
        assert coords._split_compact('0081306.09') == (8, 13, pytest.approx(6.09))

    def test_four_digits_is_decimal_minutes(self):
        """4926.024 has no seconds -- the trailing pair is decimal minutes."""
        assert coords._split_compact('4926.024') == (49, pytest.approx(26.024), 0)
        point = coords.parse_latlon('N 4926.024 E 813.101')
        assert coords.to_ddm(point) == 'N 49°26.024\'  E 8°13.101\''

    def test_padded_decimal_minutes(self):
        point = coords.parse_latlon('N 4926.024 E 00813.101')
        assert coords.to_ddm(point) == 'N 49°26.024\'  E 8°13.101\''

    def test_no_fraction(self):
        point = coords.parse_latlon('N 492601 E 081306')
        assert coords.to_dms(point, 0) == 'N 49°26\'01"  E 8°13\'06"'

    def test_hemisphere_letter_is_required(self):
        """
        Two bare run-together numbers are just two numbers. Guessing at them
        would hijack anything else someone might paste.
        """
        assert coords.parse_latlon('492601.48 81306.09') is None

    @pytest.mark.parametrize('text', [
        '49.433744, 8.218358',
        '-33.8688, 151.2093',
    ])
    def test_decimal_degrees_still_wins(self, text):
        """The compact rule must not swallow ordinary decimal degrees."""
        assert coords.parse_latlon(text) is not None


class TestMGRS:
    @pytest.mark.parametrize('text,expected', [
        ('32U MV 12345 67890', '32UMV1234567890'),
        ('32UMV1234567890', '32UMV1234567890'),
        ('32u mv 1234 5678', '32UMV12345678'),
        ('4Q FJ 12 34', '04QFJ1234'),
    ])
    def test_normalises(self, text, expected):
        assert coords.parse_mgrs(text) == expected

    def test_odd_digits_rejected(self):
        with pytest.raises(coords.CoordError):
            coords.parse_mgrs('32U MV 1234 567')

    @pytest.mark.parametrize('text', ['32I MV 1234 5678', '32O MV 1234 5678'])
    def test_excluded_bands(self, text):
        """I and O are not MGRS latitude bands."""
        assert coords.parse_mgrs(text) is None


class TestMetric:
    def test_hyphen_is_a_separator(self):
        """X-00506931 is positive 506931; the hyphen is punctuation."""
        assert coords.parse_metric('X-00506931 Z-00890072') == (506931.0, 890072.0)

    def test_two_hyphens_is_negative(self):
        assert coords.parse_metric('X--00506931 Z-00890072') == (-506931.0, 890072.0)

    def test_round_trip(self):
        assert coords.to_metric(506931, 890072) == 'X-00506931 Z-00890072'
        assert coords.to_metric(-506931, 890072) == 'X--00506931 Z-00890072'

    def test_bare_numbers(self):
        assert coords.parse_metric('X 506931 Z 890072') == (506931.0, 890072.0)


class TestRejection:
    @pytest.mark.parametrize('text', ['hello', '', 'N 49', 'X 1', '?'])
    def test_nothing_matches_rubbish(self, text):
        assert coords.parse_mgrs(text) is None
        assert coords.parse_metric(text) is None
        assert coords.parse_latlon(text) is None

    @pytest.mark.parametrize('text', ['99.5, 8.2', '49.4, 200.1'])
    def test_out_of_range_is_caught_by_validate(self, text):
        with pytest.raises(coords.CoordError):
            coords.validate(coords.parse_latlon(text))


class TestAltitude:
    def test_metres_and_feet(self):
        assert coords.format_altitude(100) == '100 m / 328 feet'
        assert coords.format_altitude(0) == '0 m / 0 feet'
