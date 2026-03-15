"""
Tests for WeatherImageFormatter.

Skipped automatically when Pillow or matplotlib are not installed.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import pytest

PIL = pytest.importorskip("PIL")
mpl = pytest.importorskip("matplotlib.pyplot")

from PIL import Image as PILImage

from bluesky_weather_bot.weather.models import (
    WeatherReport,
    ResolvedLocation,
    CurrentConditions,
    HourlyForecastSlot,
    Forecast,
    HistoricalComparison,
    DailyHistoricalRecord,
)
from bluesky_weather_bot.weather.image_formatter import WeatherImageFormatter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_current(temp_f: float = 55.0) -> CurrentConditions:
    return CurrentConditions(
        timestamp=datetime(2025, 3, 10, 10, 15),
        temperature_f=temp_f,
        temperature_c=(temp_f - 32) * 5 / 9,
        feels_like_f=temp_f - 3,
        feels_like_c=(temp_f - 3 - 32) * 5 / 9,
        humidity_pct=45.0,
        cloud_cover_pct=30.0,
        wind_speed_mph=12.0,
        wind_speed_kph=19.3,
        wind_direction_deg=225.0,
        wind_gusts_mph=18.0,
        wind_gusts_kph=29.0,
        precipitation_in=0.0,
        precipitation_mm=0.0,
        visibility_miles=10.0,
        visibility_km=16.1,
        surface_pressure_hpa=1015.0,
        weather_description="Partly cloudy",
    )


def _make_slot(hour: int, temp_f: float = 55.0, precip_pct: float = 10.0) -> HourlyForecastSlot:
    return HourlyForecastSlot(
        hour=datetime(2025, 3, 10, hour),
        temperature_f=temp_f,
        temperature_c=(temp_f - 32) * 5 / 9,
        precipitation_probability_pct=precip_pct,
        precipitation_in=0.0,
        precipitation_mm=0.0,
        wind_speed_mph=10.0,
        wind_speed_kph=16.0,
        cloud_cover_pct=20.0,
        weather_description="Partly cloudy",
    )


def _make_historical() -> HistoricalComparison:
    rec = DailyHistoricalRecord(
        date=datetime(2024, 3, 10),
        temp_max_f=60.0, temp_max_c=15.6,
        temp_min_f=38.0, temp_min_c=3.3,
        temp_mean_f=49.0, temp_mean_c=9.4,
        precipitation_in=0.05, precipitation_mm=1.3,
        wind_speed_max_mph=15.0, wind_speed_max_kph=24.1,
    )
    return HistoricalComparison(year_ago=rec, ten_year_avg=None)


def _make_report(
    historical: Optional[HistoricalComparison] = None,
    n_slots: int = 6,
) -> WeatherReport:
    loc = ResolvedLocation(
        lat=39.95, lon=-105.09,
        display_name="Longmont, CO",
        timezone="America/Denver",
    )
    forecast = Forecast(slots=[_make_slot(10 + i) for i in range(n_slots)])
    return WeatherReport(
        location=loc,
        current=_make_current(),
        forecast=forecast,
        historical=historical or HistoricalComparison(),
        generated_at=datetime(2025, 3, 10, 10, 15),
    )


# ---------------------------------------------------------------------------
# Image count
# ---------------------------------------------------------------------------

class TestImageCount:
    def test_two_images_when_no_historical(self):
        report = _make_report(historical=HistoricalComparison())
        images, alts, caption = WeatherImageFormatter().format_images(report)
        assert len(images) == 2
        assert len(alts) == 2

    def test_three_images_when_historical_present(self):
        report = _make_report(historical=_make_historical())
        images, alts, caption = WeatherImageFormatter().format_images(report)
        assert len(images) == 3
        assert len(alts) == 3

    def test_alts_match_images(self):
        report = _make_report(historical=_make_historical())
        images, alts, _ = WeatherImageFormatter().format_images(report)
        assert len(alts) == len(images)


# ---------------------------------------------------------------------------
# PNG validity
# ---------------------------------------------------------------------------

class TestPngValidity:
    def test_images_are_valid_png(self):
        report = _make_report()
        images, _, _ = WeatherImageFormatter().format_images(report)
        for img_bytes in images:
            assert img_bytes[:4] == b"\x89PNG", "Image does not start with PNG magic bytes"

    def test_current_card_dimensions(self):
        report = _make_report()
        images, _, _ = WeatherImageFormatter().format_images(report)
        pil_img = PILImage.open(io.BytesIO(images[0]))
        assert pil_img.size == (900, 900)

    def test_historical_card_dimensions(self):
        report = _make_report(historical=_make_historical())
        images, _, _ = WeatherImageFormatter().format_images(report)
        # Third image is the historical card
        pil_img = PILImage.open(io.BytesIO(images[2]))
        assert pil_img.size == (800, 400)


# ---------------------------------------------------------------------------
# Caption
# ---------------------------------------------------------------------------

class TestCaption:
    def test_caption_within_300_chars(self):
        report = _make_report()
        _, _, caption = WeatherImageFormatter().format_images(report)
        assert len(caption) <= 300

    def test_caption_contains_location(self):
        report = _make_report()
        _, _, caption = WeatherImageFormatter().format_images(report)
        assert "Longmont" in caption

    def test_caption_contains_temperature(self):
        report = _make_report()
        _, _, caption = WeatherImageFormatter().format_images(report)
        assert "F" in caption

    def test_caption_contains_hashtag(self):
        report = _make_report()
        _, _, caption = WeatherImageFormatter().format_images(report)
        assert "#" in caption


# ---------------------------------------------------------------------------
# _render_historical_card edge cases
# ---------------------------------------------------------------------------

class TestHistoricalCard:
    def test_none_when_no_historical_data(self):
        formatter = WeatherImageFormatter()
        report = _make_report(historical=HistoricalComparison())
        result = formatter._render_historical_card(report)
        assert result is None

    def test_returns_bytes_when_year_ago_present(self):
        formatter = WeatherImageFormatter()
        report = _make_report(historical=_make_historical())
        result = formatter._render_historical_card(report)
        assert result is not None
        assert result[:4] == b"\x89PNG"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_single_slot_forecast_does_not_crash(self):
        report = _make_report(n_slots=1)
        images, alts, caption = WeatherImageFormatter().format_images(report)
        assert len(images) >= 1

    def test_no_slots_forecast_does_not_crash(self):
        report = _make_report(n_slots=0)
        images, alts, caption = WeatherImageFormatter().format_images(report)
        assert len(images) >= 1
