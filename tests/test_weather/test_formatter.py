"""
Unit tests for WeatherFormatter.

Uses a synthetic WeatherReport — no network calls.
"""

from datetime import datetime

import pytest

from bluesky_weather_bot.weather.formatter import WeatherFormatter, MAX_POST_LEN
from bluesky_weather_bot.weather.models import (
    CurrentConditions,
    DailyHistoricalRecord,
    Forecast,
    HistoricalComparison,
    HourlyForecastSlot,
    ResolvedLocation,
    WeatherReport,
)


def _make_report(
    location_name: str = "Denver, CO",
    timezone: str = "America/Denver",
    include_historical: bool = True,
) -> WeatherReport:
    loc = ResolvedLocation(
        lat=39.7392, lon=-104.9903,
        display_name=location_name,
        timezone=timezone,
    )
    current = CurrentConditions(
        timestamp=datetime(2025, 3, 9, 10, 15),
        temperature_f=52.0,
        temperature_c=11.1,
        feels_like_f=48.0,
        feels_like_c=8.9,
        humidity_pct=45.0,
        cloud_cover_pct=30.0,
        wind_speed_mph=12.0,
        wind_speed_kph=19.3,
        wind_direction_deg=225.0,   # SW
        wind_gusts_mph=18.0,
        wind_gusts_kph=29.0,
        precipitation_in=0.0,
        precipitation_mm=0.0,
        visibility_miles=10.0,
        visibility_km=16.1,
        surface_pressure_hpa=1015.0,
        weather_description="Partly cloudy",
    )
    slots = [
        HourlyForecastSlot(
            hour=datetime(2025, 3, 9, 10 + i, 0),
            temperature_f=52.0 + i,
            temperature_c=round((52.0 + i - 32) * 5 / 9, 1),
            precipitation_probability_pct=max(0, 10 - 2 * i),
            precipitation_in=0.0,
            precipitation_mm=0.0,
            wind_speed_mph=max(1, 13 - i),
            wind_speed_kph=round(max(1, 13 - i) * 1.609, 1),
            cloud_cover_pct=max(0, 40 - 5 * i),
            weather_description="Partly cloudy",
        )
        for i in range(6)
    ]
    forecast = Forecast(slots=slots)

    if include_historical:
        year_ago = DailyHistoricalRecord(
            date=datetime(2024, 3, 9),
            temp_max_f=61.0, temp_max_c=16.1,
            temp_min_f=38.0, temp_min_c=3.3,
            temp_mean_f=49.5, temp_mean_c=9.7,
            precipitation_in=0.0, precipitation_mm=0.0,
            wind_speed_max_mph=15.0, wind_speed_max_kph=24.1,
        )
        ten_yr = DailyHistoricalRecord(
            date=datetime(2025, 3, 9),
            temp_max_f=55.0, temp_max_c=12.8,
            temp_min_f=28.0, temp_min_c=-2.2,
            temp_mean_f=41.5, temp_mean_c=5.3,
            precipitation_in=0.05, precipitation_mm=1.27,
            wind_speed_max_mph=12.0, wind_speed_max_kph=19.3,
        )
        historical = HistoricalComparison(year_ago=year_ago, ten_year_avg=ten_yr)
    else:
        historical = HistoricalComparison()

    return WeatherReport(
        location=loc,
        current=current,
        forecast=forecast,
        historical=historical,
        generated_at=datetime(2025, 3, 9, 17, 15),
    )


@pytest.fixture
def formatter():
    return WeatherFormatter()


@pytest.fixture
def report():
    return _make_report()


# ---------------------------------------------------------------------------
# format_thread
# ---------------------------------------------------------------------------

class TestFormatThread:
    def test_returns_three_posts_with_historical(self, formatter, report):
        posts = formatter.format_thread(report)
        assert len(posts) == 3

    def test_returns_two_posts_without_historical(self, formatter):
        report = _make_report(include_historical=False)
        posts = formatter.format_thread(report)
        assert len(posts) == 2

    def test_all_posts_within_limit(self, formatter, report):
        for post in formatter.format_thread(report):
            assert len(post) <= MAX_POST_LEN, f"Post too long ({len(post)} chars):\n{post}"


# ---------------------------------------------------------------------------
# Post 1 — current conditions
# ---------------------------------------------------------------------------

class TestPost1:
    def test_contains_location(self, formatter, report):
        post = formatter._post1_current(report)
        assert "Denver, CO" in post

    def test_contains_temperature(self, formatter, report):
        post = formatter._post1_current(report)
        assert "52°F" in post
        assert "11°C" in post

    def test_contains_feels_like(self, formatter, report):
        post = formatter._post1_current(report)
        assert "48°F" in post

    def test_contains_humidity(self, formatter, report):
        post = formatter._post1_current(report)
        assert "45%" in post

    def test_contains_wind_cardinal(self, formatter, report):
        post = formatter._post1_current(report)
        assert "SW" in post   # 225° → SW

    def test_contains_pressure(self, formatter, report):
        post = formatter._post1_current(report)
        assert "1015hPa" in post

    def test_contains_pressure(self, formatter, report):
        post = formatter._post1_current(report)
        assert "hPa" in post

    def test_within_limit(self, formatter, report):
        post = formatter._post1_current(report)
        assert len(post) <= MAX_POST_LEN


# ---------------------------------------------------------------------------
# Post 2 — forecast
# ---------------------------------------------------------------------------

class TestPost2:
    def test_contains_header(self, formatter, report):
        post = formatter._post2_forecast(report)
        assert "Denver, CO" in post
        assert "6 Hours" in post

    def test_contains_hour_labels(self, formatter, report):
        post = formatter._post2_forecast(report)
        # slots start at hour 10
        assert "10AM" in post

    def test_within_limit(self, formatter, report):
        post = formatter._post2_forecast(report)
        assert len(post) <= MAX_POST_LEN

    def test_truncation_long_location(self, formatter):
        """Very long location name should still produce a ≤300-char post."""
        report = _make_report(location_name="A" * 80)
        post = formatter._post2_forecast(report)
        assert len(post) <= MAX_POST_LEN


# ---------------------------------------------------------------------------
# Post 3 — historical
# ---------------------------------------------------------------------------

class TestPost3:
    def test_contains_location(self, formatter, report):
        post = formatter._post3_historical(report)
        assert post is not None
        assert "Denver, CO" in post

    def test_contains_year_ago(self, formatter, report):
        post = formatter._post3_historical(report)
        assert "2024" in post
        assert "61°F" in post

    def test_contains_ten_year_avg(self, formatter, report):
        post = formatter._post3_historical(report)
        assert "10-yr avg" in post
        assert "55°F" in post

    def test_returns_none_when_no_historical(self, formatter):
        report = _make_report(include_historical=False)
        assert formatter._post3_historical(report) is None

    def test_within_limit(self, formatter, report):
        post = formatter._post3_historical(report)
        assert post is not None
        assert len(post) <= MAX_POST_LEN
