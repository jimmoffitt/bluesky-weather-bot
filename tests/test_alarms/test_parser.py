"""
Tests for parse_alarm_text.

Covers metric/operator/threshold/location detection and the regression case
where a digit location (zip code) was previously mistaken for the threshold.
"""

from __future__ import annotations

import pytest

from bluesky_weather_bot.alarms.parser import parse_alarm_text


class TestMetricDetection:
    def test_current_temp(self):
        rule, err = parse_alarm_text("alert me if temp hits 100", home_location="Denver, CO")
        assert err is None
        assert rule.metric == "temp_current"

    def test_forecast_high_beats_generic_temp(self):
        rule, err = parse_alarm_text(
            "alert me if forecast high hits 100", home_location="Denver, CO"
        )
        assert err is None
        assert rule.metric == "temp_forecast_high"

    def test_forecast_low(self):
        rule, err = parse_alarm_text(
            "notify me when daily low drops below 20", home_location="Denver, CO"
        )
        assert err is None
        assert rule.metric == "temp_forecast_low"

    def test_wind(self):
        rule, err = parse_alarm_text("alert me if wind exceeds 50 mph", home_location="Denver, CO")
        assert err is None
        assert rule.metric == "wind_speed"

    def test_precip(self):
        rule, err = parse_alarm_text(
            "alert me if rain chance over 80%", home_location="Denver, CO"
        )
        assert err is None
        assert rule.metric == "precip_prob"

    def test_unknown_metric_errors(self):
        rule, err = parse_alarm_text("alert me if the sky looks nice", home_location="Denver, CO")
        assert rule is None
        assert "couldn't identify" in err.lower()


class TestOperatorDetection:
    def test_hits_defaults_gte(self):
        rule, _ = parse_alarm_text("alert me if temp hits 100", home_location="Denver, CO")
        assert rule.operator == "gte"

    def test_above_is_gte(self):
        rule, _ = parse_alarm_text("alert me if temp goes above 100", home_location="Denver, CO")
        assert rule.operator == "gte"

    def test_drops_below_is_lt(self):
        rule, _ = parse_alarm_text("alert me if temp drops below 20", home_location="Denver, CO")
        assert rule.operator == "lt"

    def test_at_most_is_lte(self):
        rule, _ = parse_alarm_text("alert me if wind is at most 10 mph", home_location="Denver, CO")
        assert rule.operator == "lte"


class TestThresholdAndUnits:
    def test_plain_number_uses_fallback_units(self):
        rule, _ = parse_alarm_text(
            "alert me if temp hits 100", home_location="Denver, CO", user_units="metric"
        )
        assert rule.threshold == 100.0
        assert rule.units == "metric"

    def test_explicit_fahrenheit_overrides_fallback(self):
        rule, _ = parse_alarm_text(
            "alert me if temp hits 100 degrees F", home_location="Denver, CO", user_units="metric"
        )
        assert rule.units == "imperial"

    def test_explicit_celsius_overrides_fallback(self):
        rule, _ = parse_alarm_text(
            "alert me if temp hits 38 degrees C",
            home_location="Denver, CO",
            user_units="imperial",
        )
        assert rule.units == "metric"

    def test_missing_threshold_errors(self):
        rule, err = parse_alarm_text("alert me if temp is high", home_location="Denver, CO")
        assert rule is None
        assert "threshold" in err.lower()

    @pytest.mark.xfail(
        reason="Known gap: a unit glued directly to the number ('100F', '38C') "
        "breaks the \\b word boundary _NUMBER_RE relies on, so no other number "
        "in the text means the whole message is rejected as 'no threshold'.",
        strict=True,
    )
    def test_unit_glued_to_number_still_parses(self):
        rule, err = parse_alarm_text("alert me if temp hits 100F", home_location="Denver, CO")
        assert err is None
        assert rule.threshold == 100.0


class TestLocationExtraction:
    def test_explicit_city_location(self):
        rule, _ = parse_alarm_text(
            "alert me if temp in Denver, CO drops below 20", home_location="Boulder, CO"
        )
        assert rule.location_raw == "Denver, CO"
        assert rule.threshold == 20.0

    def test_falls_back_to_home_location(self):
        rule, _ = parse_alarm_text("alert me if temp hits 100", home_location="Boulder, CO")
        assert rule.location_raw == "Boulder, CO"

    def test_no_location_and_no_home_errors(self):
        rule, err = parse_alarm_text("alert me if temp hits 100", home_location=None)
        assert rule is None
        assert "location" in err.lower()

    def test_zip_code_location_does_not_corrupt_threshold(self):
        """Regression: a zip-code location used to be picked up by the
        threshold regex (first digit run in the text), producing a bogus
        five-digit threshold and silently discarding the explicit location."""
        rule, err = parse_alarm_text(
            "alert me if temp in 80501 drops below 20", home_location="Boulder, CO"
        )
        assert err is None
        assert rule.location_raw == "80501"
        assert rule.threshold == 20.0

    def test_zip_code_location_with_threshold_after(self):
        rule, err = parse_alarm_text(
            "alert me if wind in 90210 exceeds 50 mph", home_location="Boulder, CO"
        )
        assert err is None
        assert rule.location_raw == "90210"
        assert rule.threshold == 50.0


class TestCooldown:
    def test_forecast_metrics_get_daily_cooldown(self):
        rule, _ = parse_alarm_text(
            "alert me if forecast high hits 100", home_location="Denver, CO"
        )
        assert rule.cooldown_hours == 24.0

    def test_current_metrics_get_default_cooldown(self):
        rule, _ = parse_alarm_text("alert me if temp hits 100", home_location="Denver, CO")
        assert rule.cooldown_hours == 4.0
