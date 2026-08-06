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
    DailyForecast,
    DailyForecastSlot,
)

# ``report.historical`` (year-ago/10-yr-avg comparison) is unrelated to the
# "On This Day" card below, which is driven by ``report.this_day_history``
# instead — a flat list of one DailyHistoricalRecord per past year.
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


def _make_historical_record(year: int = 2024) -> DailyHistoricalRecord:
    return DailyHistoricalRecord(
        date=datetime(year, 3, 10),
        temp_max_f=60.0, temp_max_c=15.6,
        temp_min_f=38.0, temp_min_c=3.3,
        temp_mean_f=49.0, temp_mean_c=9.4,
        precipitation_in=0.05, precipitation_mm=1.3,
        wind_speed_max_mph=15.0, wind_speed_max_kph=24.1,
    )


def _make_this_day_history(n_years: int = 3) -> list[DailyHistoricalRecord]:
    return [_make_historical_record(2025 - i) for i in range(n_years)]


def _make_report(
    this_day_history: Optional[list[DailyHistoricalRecord]] = None,
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
        historical=HistoricalComparison(),
        this_day_history=this_day_history or [],
        generated_at=datetime(2025, 3, 10, 10, 15),
    )


# ---------------------------------------------------------------------------
# Image count
# ---------------------------------------------------------------------------

class TestImageCount:
    def test_one_image_by_default(self):
        """No include_forecast/include_day: just the current-conditions card."""
        report = _make_report(this_day_history=_make_this_day_history())
        images, alts, caption = WeatherImageFormatter().format_images(report)
        assert len(images) == 1
        assert len(alts) == 1

    def test_two_images_with_forecast_requested(self):
        report = _make_report(this_day_history=[])
        images, alts, caption = WeatherImageFormatter().format_images(
            report, include_forecast=True
        )
        assert len(images) == 2
        assert len(alts) == 2

    def test_three_images_with_forecast_and_day_requested(self):
        report = _make_report(this_day_history=_make_this_day_history())
        images, alts, caption = WeatherImageFormatter().format_images(
            report, include_forecast=True, include_day=True
        )
        assert len(images) == 3
        assert len(alts) == 3

    def test_day_requested_but_no_history_data_stays_at_one_image(self):
        report = _make_report(this_day_history=[])
        images, alts, caption = WeatherImageFormatter().format_images(
            report, include_day=True
        )
        assert len(images) == 1

    def test_alts_match_images(self):
        report = _make_report(this_day_history=_make_this_day_history())
        images, alts, _ = WeatherImageFormatter().format_images(
            report, include_forecast=True, include_day=True
        )
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
        # Narrow + tall (vertical phone proportions) rather than square, and
        # fit tightly to content (header + temp block + 6 stat rows, no
        # sunrise/sunset row since the fixture has no daily_forecast slot) —
        # this pins the exact computed value so a future change to row/header
        # sizing is a deliberate, visible diff.
        assert pil_img.size == (640, 844)

    def test_current_card_desktop_dimensions(self):
        report = _make_report()
        pil_img = PILImage.open(io.BytesIO(
            WeatherImageFormatter()._render_current_card_desktop(report)
        ))
        assert pil_img.size == (1200, 556)

    def test_current_card_gains_a_row_and_height_with_sunrise_data(self):
        report = _make_report()
        report.daily_forecast = DailyForecast(slots=[
            DailyForecastSlot(
                date=datetime(2025, 3, 10),
                temp_max_f=60.0, temp_max_c=15.6,
                temp_min_f=38.0, temp_min_c=3.3,
                precipitation_probability_max_pct=10.0,
                precipitation_in=0.0, precipitation_mm=0.0,
                wind_speed_max_mph=15.0, wind_speed_max_kph=24.1,
                weather_description="Partly cloudy",
                sunrise=datetime(2025, 3, 10, 6, 30),
                sunset=datetime(2025, 3, 10, 18, 45),
            )
        ])
        pil_img = PILImage.open(io.BytesIO(
            WeatherImageFormatter()._render_current_card(report)
        ))
        # One extra 92px row versus the no-sunrise-data case (640, 844).
        assert pil_img.size == (640, 936)

    def test_this_day_card_dimensions(self):
        report = _make_report(this_day_history=_make_this_day_history())
        images, _, _ = WeatherImageFormatter().format_images(
            report, include_forecast=True, include_day=True
        )
        # Third image is the "On This Day" historical chart card
        pil_img = PILImage.open(io.BytesIO(images[2]))
        assert pil_img.size == (760, 538)


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

    def test_caption_mentions_directives_when_neither_requested(self):
        report = _make_report()
        _, _, caption = WeatherImageFormatter().format_images(report)
        assert "/forecast" in caption
        assert "/day" in caption
        assert "second image" not in caption.lower()

    def test_caption_does_not_mention_directives_when_forecast_requested(self):
        report = _make_report()
        _, _, caption = WeatherImageFormatter().format_images(report, include_forecast=True)
        assert "/forecast" not in caption
        assert "image 2" in caption.lower()

    def test_caption_reflects_actual_day_card_even_if_requested_but_unavailable(self):
        # include_day=True but no this_day_history data → day card isn't
        # actually rendered, so the caption shouldn't claim it exists.
        report = _make_report(this_day_history=[])
        _, _, caption = WeatherImageFormatter().format_images(report, include_day=True)
        assert "Add /forecast or /day for more." in caption

    def test_caption_mentions_both_images_when_both_requested(self):
        report = _make_report(this_day_history=_make_this_day_history())
        _, _, caption = WeatherImageFormatter().format_images(
            report, include_forecast=True, include_day=True
        )
        assert "image 2" in caption.lower()
        assert "image 3" in caption.lower()


# ---------------------------------------------------------------------------
# _render_this_day_card edge cases
# ---------------------------------------------------------------------------

class TestThisDayCard:
    def test_empty_bytes_when_no_this_day_history(self):
        formatter = WeatherImageFormatter()
        report = _make_report(this_day_history=[])
        result = formatter._render_this_day_card(report)
        assert result == b""

    def test_returns_png_bytes_when_history_present(self):
        formatter = WeatherImageFormatter()
        report = _make_report(this_day_history=_make_this_day_history())
        result = formatter._render_this_day_card(report)
        assert result[:4] == b"\x89PNG"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_single_slot_forecast_does_not_crash(self):
        report = _make_report(n_slots=1)
        images, alts, caption = WeatherImageFormatter().format_images(
            report, include_forecast=True
        )
        assert len(images) >= 1

    def test_no_slots_forecast_does_not_crash(self):
        report = _make_report(n_slots=0)
        images, alts, caption = WeatherImageFormatter().format_images(
            report, include_forecast=True
        )
        assert len(images) >= 1
