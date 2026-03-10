"""
Unit tests for WeatherService.

Mocks LocationResolver, WeatherClient, and Database to isolate service logic.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

from bluesky_weather_bot.weather.models import (
    CurrentConditions,
    Forecast,
    HistoricalComparison,
    ResolvedLocation,
    WeatherReport,
)
from bluesky_weather_bot.weather.service import WeatherService, _report_to_dict, _dict_to_report


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_loc(name="Denver, CO", lat=39.74, lon=-104.99, tz="America/Denver"):
    return ResolvedLocation(lat=lat, lon=lon, display_name=name, timezone=tz)


def _make_report(loc=None):
    if loc is None:
        loc = _make_loc()
    current = CurrentConditions(
        timestamp=datetime(2025, 3, 9, 10, 0),
        temperature_f=52.0, temperature_c=11.1,
        feels_like_f=48.0, feels_like_c=8.9,
        humidity_pct=45.0, cloud_cover_pct=30.0,
        wind_speed_mph=12.0, wind_speed_kph=19.3,
        wind_direction_deg=225.0,
        wind_gusts_mph=18.0, wind_gusts_kph=29.0,
        precipitation_in=0.0, precipitation_mm=0.0,
        visibility_miles=10.0, visibility_km=16.1,
        surface_pressure_hpa=1015.0,
        weather_description="Partly cloudy",
    )
    return WeatherReport(
        location=loc,
        current=current,
        forecast=Forecast(slots=[]),
        historical=HistoricalComparison(),
        generated_at=datetime(2025, 3, 9, 17, 0),
    )


@pytest.fixture
def db():
    mock_db = MagicMock()
    mock_db.get_cached_report.return_value = None  # cache miss by default
    return mock_db


# ---------------------------------------------------------------------------
# Cache miss path
# ---------------------------------------------------------------------------

class TestCacheMiss:
    def test_calls_client_fetch_on_miss(self, db):
        loc  = _make_loc()
        report = _make_report(loc)

        with (
            patch("bluesky_weather_bot.weather.service.LocationResolver") as MockResolver,
            patch("bluesky_weather_bot.weather.service.WeatherClient") as MockClient,
        ):
            MockResolver.return_value.resolve.return_value = [loc]
            MockClient.return_value.fetch.return_value = report

            svc = WeatherService(db, skip_historical=True)
            results = svc.lookup("Denver, CO")

        assert len(results) == 1
        MockClient.return_value.fetch.assert_called_once_with(
            lat=loc.lat, lon=loc.lon, timezone=loc.timezone, skip_historical=True
        )

    def test_saves_to_cache_on_miss(self, db):
        loc = _make_loc()
        report = _make_report(loc)

        with (
            patch("bluesky_weather_bot.weather.service.LocationResolver") as MockResolver,
            patch("bluesky_weather_bot.weather.service.WeatherClient") as MockClient,
        ):
            MockResolver.return_value.resolve.return_value = [loc]
            MockClient.return_value.fetch.return_value = report

            svc = WeatherService(db, skip_historical=True)
            svc.lookup("Denver, CO")

        db.save_cached_report.assert_called_once()
        kwargs = db.save_cached_report.call_args
        assert kwargs[1]["lat"] == loc.lat or kwargs[0][0] == loc.lat


# ---------------------------------------------------------------------------
# Cache hit path
# ---------------------------------------------------------------------------

class TestCacheHit:
    def test_does_not_call_client_on_hit(self, db):
        loc = _make_loc()
        report = _make_report(loc)
        db.get_cached_report.return_value = _report_to_dict(report)

        with (
            patch("bluesky_weather_bot.weather.service.LocationResolver") as MockResolver,
            patch("bluesky_weather_bot.weather.service.WeatherClient") as MockClient,
        ):
            MockResolver.return_value.resolve.return_value = [loc]

            svc = WeatherService(db, skip_historical=True)
            results = svc.lookup("Denver, CO")

        MockClient.return_value.fetch.assert_not_called()
        assert len(results) == 1
        assert results[0].location.display_name == "Denver, CO"

    def test_cache_roundtrip_preserves_data(self, db):
        loc = _make_loc()
        report = _make_report(loc)
        serialized = _report_to_dict(report)
        recovered = _dict_to_report(serialized)

        assert recovered.current.temperature_f == report.current.temperature_f
        assert recovered.current.weather_description == report.current.weather_description
        assert recovered.location.display_name == report.location.display_name
        assert recovered.generated_at == report.generated_at


# ---------------------------------------------------------------------------
# Multiple locations (ambiguous city)
# ---------------------------------------------------------------------------

class TestMultipleLocations:
    def test_ambiguous_returns_multiple(self, db):
        loc_or = _make_loc("Portland, OR", 45.5, -122.7, "America/Los_Angeles")
        loc_me = _make_loc("Portland, ME", 43.7, -70.3, "America/New_York")

        report_or = _make_report(loc_or)
        report_me = _make_report(loc_me)

        with (
            patch("bluesky_weather_bot.weather.service.LocationResolver") as MockResolver,
            patch("bluesky_weather_bot.weather.service.WeatherClient") as MockClient,
        ):
            MockResolver.return_value.resolve.return_value = [loc_or, loc_me]
            MockClient.return_value.fetch.side_effect = [report_or, report_me]

            svc = WeatherService(db, skip_historical=True)
            results = svc.lookup("Portland")

        assert len(results) == 2
        names = {r.location.display_name for r in results}
        assert "Portland, OR" in names
        assert "Portland, ME" in names


# ---------------------------------------------------------------------------
# Resolution failure
# ---------------------------------------------------------------------------

class TestResolutionFailure:
    def test_raises_value_error_on_bad_location(self, db):
        with (
            patch("bluesky_weather_bot.weather.service.LocationResolver") as MockResolver,
            patch("bluesky_weather_bot.weather.service.WeatherClient"),
        ):
            MockResolver.return_value.resolve.side_effect = ValueError("not found")

            svc = WeatherService(db, skip_historical=True)
            with pytest.raises(ValueError):
                svc.lookup("xyzzy123notaplace")


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_datetime_round_trip(self):
        loc = _make_loc()
        report = _make_report(loc)
        d = _report_to_dict(report)
        # All datetimes should be strings
        assert isinstance(d["current"]["timestamp"], str)
        assert isinstance(d["generated_at"], str)

        recovered = _dict_to_report(d)
        assert isinstance(recovered.current.timestamp, datetime)
        assert isinstance(recovered.generated_at, datetime)

    def test_candidates_dropped_in_serialization(self):
        loc = _make_loc()
        # Simulate candidates circular reference
        loc.candidates = [loc]
        report = _make_report(loc)
        d = _report_to_dict(report)
        assert d["location"]["candidates"] == []
