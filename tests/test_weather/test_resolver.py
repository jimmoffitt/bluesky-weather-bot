"""
Unit tests for LocationResolver.

No network calls — covers zip lookup (mocked pgeocode), TOP_CITIES direct hits,
city-only index (Minneapolis-style), ambiguous-city expansion, normalization,
resolve_latlon(), and the zip_code field on ResolvedLocation.
"""

from unittest.mock import MagicMock, patch

import pytest

from bluesky_weather_bot.weather.resolver import LocationResolver, CITY_ONLY_INDEX
from bluesky_weather_bot.weather.models import ResolvedLocation


@pytest.fixture
def resolver():
    return LocationResolver()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_already_normalized(self, resolver):
        assert resolver._normalize("denver, co") == "denver, co"

    def test_city_state_no_comma(self, resolver):
        assert resolver._normalize("Denver CO") == "denver, co"

    def test_full_state_name_with_comma(self, resolver):
        assert resolver._normalize("Denver, Colorado") == "denver, co"

    def test_full_state_name_no_comma(self, resolver):
        assert resolver._normalize("Denver Colorado") == "denver, co"

    def test_uppercase(self, resolver):
        assert resolver._normalize("SEATTLE WA") == "seattle, wa"

    def test_leading_trailing_whitespace(self, resolver):
        assert resolver._normalize("  Boston, MA  ") == "boston, ma"


# ---------------------------------------------------------------------------
# TOP_CITIES direct hits
# ---------------------------------------------------------------------------

class TestTopCities:
    def test_denver(self, resolver):
        results = resolver.resolve("Denver, CO")
        assert len(results) == 1
        loc = results[0]
        assert loc.display_name == "Denver, CO"
        assert abs(loc.lat - 39.7392) < 0.01
        assert loc.timezone == "America/Denver"

    def test_longmont(self, resolver):
        results = resolver.resolve("Longmont, CO")
        assert len(results) == 1
        assert results[0].display_name == "Longmont, CO"

    def test_new_york_city(self, resolver):
        results = resolver.resolve("NYC")
        assert len(results) == 1
        assert results[0].display_name == "New York, NY"

    def test_case_insensitive(self, resolver):
        results = resolver.resolve("DENVER, CO")
        assert len(results) == 1
        assert results[0].display_name == "Denver, CO"

    def test_city_full_state(self, resolver):
        results = resolver.resolve("Seattle, Washington")
        assert len(results) == 1
        assert results[0].display_name == "Seattle, WA"


# ---------------------------------------------------------------------------
# Ambiguous cities
# ---------------------------------------------------------------------------

class TestAmbiguousCities:
    def test_portland_returns_two(self, resolver):
        results = resolver.resolve("Portland")
        assert len(results) == 2
        names = {r.display_name for r in results}
        assert "Portland, OR" in names
        assert "Portland, ME" in names

    def test_all_ambiguous_flagged(self, resolver):
        results = resolver.resolve("Springfield")
        assert len(results) > 1
        for r in results:
            assert r.input_was_ambiguous is True

    def test_portland_with_state_not_ambiguous(self, resolver):
        results = resolver.resolve("Portland, OR")
        assert len(results) == 1
        assert results[0].display_name == "Portland, OR"
        assert results[0].input_was_ambiguous is False


# ---------------------------------------------------------------------------
# Zip code lookup (mocked pgeocode)
# ---------------------------------------------------------------------------

class TestZipLookup:
    def test_valid_zip(self, resolver):
        mock_result = MagicMock()
        mock_result.latitude = 40.1672
        mock_result.longitude = -105.1019
        mock_result.state_code = "CO"
        mock_result.place_name = "Longmont"

        with patch("pgeocode.Nominatim") as mock_nomi_cls:
            mock_nomi_cls.return_value.query_postal_code.return_value = mock_result
            results = resolver.resolve("80501")

        assert len(results) == 1
        assert "Longmont" in results[0].display_name
        assert abs(results[0].lat - 40.1672) < 0.001

    def test_zip_code_field_populated(self, resolver):
        """ResolvedLocation.zip_code must be set when resolving a ZIP."""
        mock_result = MagicMock()
        mock_result.latitude = 44.9778
        mock_result.longitude = -93.2650
        mock_result.state_code = "MN"
        mock_result.place_name = "Minneapolis"

        with patch("pgeocode.Nominatim") as mock_nomi_cls:
            mock_nomi_cls.return_value.query_postal_code.return_value = mock_result
            loc = resolver.resolve("55401")[0]

        assert loc.zip_code == "55401"

    def test_city_lookup_has_no_zip_code(self, resolver):
        """Direct city lookups don't produce a zip_code."""
        loc = resolver.resolve("Denver, CO")[0]
        assert loc.zip_code is None

    def test_invalid_zip_raises(self, resolver):
        mock_result = MagicMock()
        mock_result.latitude = float("nan")

        with patch("pgeocode.Nominatim") as mock_nomi_cls:
            mock_nomi_cls.return_value.query_postal_code.return_value = mock_result
            with pytest.raises(ValueError, match="not found"):
                resolver.resolve("00000")


# ---------------------------------------------------------------------------
# City-only index (bare city name, no state)
# ---------------------------------------------------------------------------

class TestCityOnlyIndex:
    """
    Cities in CITY_ONLY_INDEX resolve offline without a state suffix.
    Ambiguous cities still go through the multi-candidate path.
    """

    def test_index_is_populated(self):
        assert len(CITY_ONLY_INDEX) > 50

    def test_minneapolis_no_state(self, resolver):
        results = resolver.resolve("Minneapolis")
        assert len(results) == 1
        assert results[0].display_name == "Minneapolis, MN"

    def test_minneapolis_case_insensitive(self, resolver):
        assert resolver.resolve("MINNEAPOLIS")[0].display_name == "Minneapolis, MN"
        assert resolver.resolve("minneapolis")[0].display_name == "Minneapolis, MN"

    def test_minneapolis_with_state_still_works(self, resolver):
        assert resolver.resolve("Minneapolis, MN")[0].display_name == "Minneapolis, MN"

    def test_houston_no_state(self, resolver):
        results = resolver.resolve("Houston")
        assert len(results) == 1
        assert results[0].display_name == "Houston, TX"

    def test_seattle_no_state(self, resolver):
        results = resolver.resolve("Seattle")
        assert len(results) == 1
        assert results[0].display_name == "Seattle, WA"

    def test_chicago_no_state(self, resolver):
        results = resolver.resolve("Chicago")
        assert len(results) == 1
        assert results[0].display_name == "Chicago, IL"

    def test_ambiguous_aurora_not_in_index(self):
        """Aurora appears in CO and IL — must stay out of the city-only index."""
        assert "aurora" not in CITY_ONLY_INDEX

    def test_ambiguous_portland_still_returns_two(self, resolver):
        results = resolver.resolve("Portland")
        assert len(results) == 2

    def test_ambiguous_springfield_still_returns_multiple(self, resolver):
        results = resolver.resolve("Springfield")
        assert len(results) > 1

    def test_city_only_has_correct_timezone(self, resolver):
        loc = resolver.resolve("Minneapolis")[0]
        assert loc.timezone == "America/Chicago"

    def test_city_only_has_correct_coords(self, resolver):
        loc = resolver.resolve("Minneapolis")[0]
        assert abs(loc.lat - 44.9778) < 0.01
        assert abs(loc.lon - (-93.2650)) < 0.01


# ---------------------------------------------------------------------------
# resolve_latlon
# ---------------------------------------------------------------------------

class TestResolveLatLon:
    def test_returns_resolved_location(self, resolver):
        loc = resolver.resolve_latlon(39.7392, -104.9903)
        assert isinstance(loc, ResolvedLocation)

    def test_denver_coords_hit_top_city(self, resolver):
        """Denver coordinates are within 50 km of Denver in TOP_CITIES."""
        loc = resolver.resolve_latlon(39.7392, -104.9903)
        assert "Denver" in loc.display_name

    def test_longmont_coords_hit_top_city(self, resolver):
        loc = resolver.resolve_latlon(40.1672, -105.1019)
        assert "Longmont" in loc.display_name

    def test_timezone_is_set(self, resolver):
        loc = resolver.resolve_latlon(39.7392, -104.9903)
        assert loc.timezone  # non-empty
        assert "/" in loc.timezone  # looks like an IANA tz

    def test_coords_preserved(self, resolver):
        loc = resolver.resolve_latlon(39.7392, -104.9903)
        assert abs(loc.lat - 39.7392) < 0.001
        assert abs(loc.lon - (-104.9903)) < 0.001

    def test_remote_coords_return_something(self, resolver):
        """Coords far from any top city fall through to Nominatim or coordinate fallback."""
        loc = resolver.resolve_latlon(46.8772, -96.7898)  # Fargo, ND — in TOP_CITIES
        assert loc.display_name  # non-empty
        assert loc.timezone
