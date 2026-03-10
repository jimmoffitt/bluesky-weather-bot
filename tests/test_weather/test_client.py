"""
Integration tests for WeatherClient and the full location→posts pipeline.

Run with:
    pytest tests/test_weather/test_client.py -m integration -v

Or include in the full suite:
    pytest -m integration
"""

import pytest

from bluesky_weather_bot.weather.client import WeatherClient, _f_to_c, _mph_to_kph, _in_to_mm
from bluesky_weather_bot.weather.formatter import WeatherFormatter
from bluesky_weather_bot.weather.models import WeatherReport
from bluesky_weather_bot.weather.service import WeatherService
from bluesky_weather_bot.storage.db import Database


# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------

class TestConversions:
    def test_freezing_point(self):
        assert _f_to_c(32.0) == 0.0

    def test_boiling_point(self):
        assert abs(_f_to_c(212.0) - 100.0) < 0.01

    def test_body_temp(self):
        assert abs(_f_to_c(98.6) - 37.0) < 0.1

    def test_mph_to_kph(self):
        assert abs(_mph_to_kph(60.0) - 96.56) < 0.1

    def test_in_to_mm(self):
        assert abs(_in_to_mm(1.0) - 25.4) < 0.01

    def test_zero_values(self):
        assert _f_to_c(32.0) == 0.0
        assert _mph_to_kph(0.0) == 0.0
        assert _in_to_mm(0.0) == 0.0


# ---------------------------------------------------------------------------
# Integration — live API (marked to allow skipping in CI)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestWeatherClientLive:
    """
    These tests call the real Open-Meteo API. They require network access.
    Run with: pytest -m integration
    """

    @pytest.fixture
    def client(self):
        return WeatherClient()

    # Longmont, CO coordinates
    LAT = 40.1672
    LON = -105.1019
    TZ  = "America/Denver"

    def test_fetch_returns_weather_report(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        assert isinstance(report, WeatherReport)

    def test_current_conditions_populated(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        c = report.current
        # Temperature should be in a plausible range for any month in Colorado
        assert -60.0 < c.temperature_f < 130.0
        assert -60.0 < c.temperature_c < 60.0

    def test_dual_units_consistent(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        c = report.current
        # °C should match °F conversion within rounding
        expected_c = round((c.temperature_f - 32) * 5 / 9, 1)
        assert abs(c.temperature_c - expected_c) <= 0.2

        expected_kph = round(c.wind_speed_mph * 1.60934, 1)
        assert abs(c.wind_speed_kph - expected_kph) <= 0.5

    def test_forecast_has_slots(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        assert len(report.forecast.slots) > 0

    def test_forecast_slots_are_hourly(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        slots = report.forecast.slots
        if len(slots) >= 2:
            delta = slots[1].hour - slots[0].hour
            assert delta.total_seconds() == 3600

    def test_skip_historical_true_omits_historical(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        assert report.historical.year_ago is None
        assert report.historical.ten_year_avg is None

    def test_skip_historical_false_includes_historical(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=False)
        # We might not always get year_ago (data gaps) but ten_year_avg from
        # the ±7 day window should almost always be present
        assert report.historical.ten_year_avg is not None

    def test_visibility_non_negative(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        assert report.current.visibility_miles >= 0.0
        assert report.current.visibility_km >= 0.0

    def test_location_coords_preserved(self, client):
        report = client.fetch(self.LAT, self.LON, self.TZ, skip_historical=True)
        assert abs(report.location.lat - self.LAT) < 0.001
        assert abs(report.location.lon - self.LON) < 0.001


# ---------------------------------------------------------------------------
# Integration — full pipeline: location string → WeatherReport → posts
# ---------------------------------------------------------------------------

def _run_pipeline(raw_location: str) -> list[str]:
    """Helper: resolve raw_location, fetch weather, format into posts."""
    with Database(":memory:") as db:
        svc = WeatherService(db, skip_historical=True)
        reports = svc.lookup(raw_location)
    fmt = WeatherFormatter()
    all_posts = []
    for report in reports:
        all_posts.extend(fmt.format_thread(report))
    return all_posts


@pytest.mark.integration
class TestPipelineZipCode:
    """ZIP code → resolved location → weather posts."""

    def test_zip_resolves_and_returns_posts(self):
        posts = _run_pipeline("55401")   # downtown Minneapolis ZIP
        assert len(posts) >= 2           # current + forecast at minimum

    def test_zip_post1_has_location(self):
        posts = _run_pipeline("55401")
        assert "📍" in posts[0]

    def test_zip_post1_has_temperature(self):
        posts = _run_pipeline("55401")
        assert "°F" in posts[0]

    def test_zip_post2_is_forecast(self):
        posts = _run_pipeline("55401")
        assert "6 Hours" in posts[1]

    def test_zip_all_posts_within_char_limit(self):
        posts = _run_pipeline("55401")
        for i, post in enumerate(posts):
            assert len(post) <= 300, f"Post {i} is {len(post)} chars:\n{post}"


@pytest.mark.integration
class TestPipelineMinneapolis:
    """'Minneapolis' (bare city, no state) → resolved offline → weather posts."""

    def test_minneapolis_resolves_without_network(self):
        """Resolution must succeed using CITY_ONLY_INDEX, not Nominatim."""
        from bluesky_weather_bot.weather.resolver import LocationResolver
        from unittest.mock import patch

        resolver = LocationResolver()
        with patch("geopy.geocoders.Nominatim") as mock_nom:
            results = resolver.resolve("Minneapolis")
            mock_nom.assert_not_called()   # Nominatim never touched

        assert results[0].display_name == "Minneapolis, MN"

    def test_minneapolis_returns_posts(self):
        posts = _run_pipeline("Minneapolis")
        assert len(posts) >= 2

    def test_minneapolis_display_name_in_post(self):
        posts = _run_pipeline("Minneapolis")
        assert "Minneapolis" in posts[0]

    def test_minneapolis_post1_within_char_limit(self):
        posts = _run_pipeline("Minneapolis")
        assert len(posts[0]) <= 300

    def test_minneapolis_forecast_post_present(self):
        posts = _run_pipeline("Minneapolis")
        assert any("6 Hours" in p for p in posts)
