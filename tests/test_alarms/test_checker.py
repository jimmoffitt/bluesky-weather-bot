"""
Tests for AlarmChecker: condition evaluation and fire/cooldown behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from bluesky_weather_bot.alarms.checker import AlarmChecker, _evaluate_condition
from bluesky_weather_bot.alarms.models import AlarmRule
from bluesky_weather_bot.channels.notify.base import NotificationResult
from bluesky_weather_bot.weather.models import (
    CurrentConditions,
    DailyForecast,
    DailyForecastSlot,
    Forecast,
    HistoricalComparison,
    HourlyForecastSlot,
    ResolvedLocation,
    WeatherReport,
)


def _make_report(
    temp_f: float = 70.0,
    wind_mph: float = 5.0,
    daily_highs_f: tuple[float, ...] = (80.0,),
    daily_lows_f: tuple[float, ...] = (40.0,),
    precip_pcts: tuple[float, ...] = (10.0,),
) -> WeatherReport:
    loc = ResolvedLocation(
        lat=39.7, lon=-104.99, display_name="Denver, CO", timezone="America/Denver"
    )
    current = CurrentConditions(
        timestamp=datetime(2026, 1, 1, 12, 0),
        temperature_f=temp_f,
        temperature_c=(temp_f - 32) * 5 / 9,
        feels_like_f=temp_f,
        feels_like_c=(temp_f - 32) * 5 / 9,
        humidity_pct=30.0,
        cloud_cover_pct=10.0,
        wind_speed_mph=wind_mph,
        wind_speed_kph=wind_mph * 1.60934,
        wind_direction_deg=180.0,
        wind_gusts_mph=wind_mph,
        wind_gusts_kph=wind_mph * 1.60934,
        precipitation_in=0.0,
        precipitation_mm=0.0,
        visibility_miles=10.0,
        visibility_km=16.1,
        surface_pressure_hpa=1013.0,
        weather_description="Clear",
    )
    forecast = Forecast(slots=[
        HourlyForecastSlot(
            hour=datetime(2026, 1, 1, h),
            temperature_f=temp_f,
            temperature_c=(temp_f - 32) * 5 / 9,
            precipitation_probability_pct=pct,
            precipitation_in=0.0,
            precipitation_mm=0.0,
            wind_speed_mph=wind_mph,
            wind_speed_kph=wind_mph * 1.60934,
            cloud_cover_pct=10.0,
            weather_description="Clear",
        )
        for h, pct in enumerate(precip_pcts)
    ])
    daily = DailyForecast(slots=[
        DailyForecastSlot(
            date=datetime(2026, 1, 1 + i),
            temp_max_f=hi,
            temp_max_c=(hi - 32) * 5 / 9,
            temp_min_f=lo,
            temp_min_c=(lo - 32) * 5 / 9,
            precipitation_probability_max_pct=10.0,
            precipitation_in=0.0,
            precipitation_mm=0.0,
            wind_speed_max_mph=wind_mph,
            wind_speed_max_kph=wind_mph * 1.60934,
            weather_description="Clear",
        )
        for i, (hi, lo) in enumerate(zip(daily_highs_f, daily_lows_f))
    ])
    return WeatherReport(
        location=loc,
        current=current,
        forecast=forecast,
        historical=HistoricalComparison(),
        daily_forecast=daily,
    )


def _make_rule(**overrides) -> AlarmRule:
    defaults = dict(
        id=1,
        user_did="did:plc:alice",
        user_handle="alice.bsky.social",
        location_raw="Denver, CO",
        metric="temp_current",
        operator="gte",
        threshold=100.0,
        units="imperial",
    )
    defaults.update(overrides)
    return AlarmRule(**defaults)


class TestEvaluateCondition:
    def test_temp_current_gte_met(self):
        met, value = _evaluate_condition(_make_rule(operator="gte", threshold=90.0), _make_report(temp_f=95.0))
        assert met is True
        assert value == 95.0

    def test_temp_current_gte_not_met(self):
        met, _ = _evaluate_condition(_make_rule(operator="gte", threshold=90.0), _make_report(temp_f=80.0))
        assert met is False

    def test_temp_current_lt(self):
        met, value = _evaluate_condition(
            _make_rule(metric="temp_current", operator="lt", threshold=20.0),
            _make_report(temp_f=10.0),
        )
        assert met is True
        assert value == 10.0

    def test_temp_forecast_high_uses_max_across_slots(self):
        rule = _make_rule(metric="temp_forecast_high", operator="gte", threshold=95.0)
        report = _make_report(daily_highs_f=(80.0, 99.0, 85.0), daily_lows_f=(40.0, 50.0, 45.0))
        met, value = _evaluate_condition(rule, report)
        assert met is True
        assert value == 99.0

    def test_temp_forecast_low_uses_min_across_slots(self):
        rule = _make_rule(metric="temp_forecast_low", operator="lte", threshold=15.0)
        report = _make_report(daily_highs_f=(80.0, 90.0), daily_lows_f=(20.0, 10.0))
        met, value = _evaluate_condition(rule, report)
        assert met is True
        assert value == 10.0

    def test_precip_prob_uses_max_across_slots(self):
        rule = _make_rule(metric="precip_prob", operator="gt", threshold=75.0)
        report = _make_report(precip_pcts=(10.0, 90.0, 30.0))
        met, value = _evaluate_condition(rule, report)
        assert met is True
        assert value == 90.0

    def test_wind_speed_metric(self):
        rule = _make_rule(metric="wind_speed", operator="gte", threshold=20.0)
        met, value = _evaluate_condition(rule, _make_report(wind_mph=25.0))
        assert met is True
        assert value == 25.0

    def test_metric_units_metric_uses_celsius(self):
        rule = _make_rule(metric="temp_current", operator="gte", threshold=30.0, units="metric")
        report = _make_report(temp_f=95.0)  # ~35C
        met, value = _evaluate_condition(rule, report)
        assert met is True
        assert value == pytest.approx((95.0 - 32) * 5 / 9)

    def test_empty_daily_forecast_not_met(self):
        rule = _make_rule(metric="temp_forecast_high", operator="gte", threshold=1.0)
        report = _make_report()
        report.daily_forecast.slots = []
        met, _ = _evaluate_condition(rule, report)
        assert met is False


class TestEvaluateAndMaybeFire:
    def _checker(self):
        db = MagicMock()
        weather = MagicMock()
        dm = MagicMock()
        dm.send.return_value = NotificationResult(success=True, channel="bluesky_dm")
        return AlarmChecker(db=db, weather_service=weather, dm_channel=dm), db, dm

    def test_fires_and_records_when_condition_met(self):
        checker, db, dm = self._checker()
        rule = _make_rule(operator="gte", threshold=90.0)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        db.update_alarm_checked.assert_called_once_with(rule.id)
        dm.send.assert_called_once()
        db.update_alarm_fired.assert_called_once_with(rule.id)

    def test_does_not_fire_when_condition_not_met(self):
        checker, db, dm = self._checker()
        rule = _make_rule(operator="gte", threshold=90.0)
        report = _make_report(temp_f=50.0)

        checker._evaluate_and_maybe_fire(rule, report)

        dm.send.assert_not_called()
        db.update_alarm_fired.assert_not_called()

    def test_respects_cooldown(self):
        checker, db, dm = self._checker()
        recent = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        rule = _make_rule(operator="gte", threshold=90.0, cooldown_hours=4.0, last_fired_at=recent)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        dm.send.assert_not_called()
        db.update_alarm_fired.assert_not_called()

    def test_fires_again_after_cooldown_expires(self):
        checker, db, dm = self._checker()
        old = (datetime.utcnow() - timedelta(hours=5)).isoformat()
        rule = _make_rule(operator="gte", threshold=90.0, cooldown_hours=4.0, last_fired_at=old)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        dm.send.assert_called_once()
        db.update_alarm_fired.assert_called_once_with(rule.id)

    def test_does_not_record_fire_when_dm_send_fails(self):
        checker, db, dm = self._checker()
        dm.send.return_value = NotificationResult(success=False, channel="bluesky_dm", error="boom")
        rule = _make_rule(operator="gte", threshold=90.0)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        dm.send.assert_called_once()
        db.update_alarm_fired.assert_not_called()


class TestPublicFiring:
    def _checker_with_post(self):
        db = MagicMock()
        weather = MagicMock()
        dm = MagicMock()
        post = MagicMock()
        post.send.return_value = NotificationResult(success=True, channel="bluesky_post")
        checker = AlarmChecker(db=db, weather_service=weather, dm_channel=dm, post_channel=post)
        return checker, db, dm, post

    def test_public_rule_posts_publicly_instead_of_dm(self):
        checker, db, dm, post = self._checker_with_post()
        rule = _make_rule(operator="gte", threshold=90.0, is_public=True)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        post.send.assert_called_once()
        dm.send.assert_not_called()
        db.update_alarm_fired.assert_called_once_with(rule.id)

        payload = post.send.call_args.args[0]
        assert payload.target_channel == "bluesky_post"
        # First element of post_thread is a client_utils.TextBuilder carrying
        # the @mention facet, not a plain string.
        text_builder = payload.post_thread[0]
        assert rule.user_handle in text_builder.build_text()
        assert "Denver, CO" in text_builder.build_text()

    def test_private_rule_still_uses_dm_when_post_channel_configured(self):
        checker, db, dm, post = self._checker_with_post()
        rule = _make_rule(operator="gte", threshold=90.0, is_public=False)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        dm.send.assert_called_once()
        post.send.assert_not_called()

    def test_public_rule_without_post_channel_skips_and_does_not_crash(self):
        # No post_channel wired up (e.g. bluesky_post notify channel not
        # registered) — should log and skip, not raise or fall back to DM.
        db = MagicMock()
        weather = MagicMock()
        dm = MagicMock()
        checker = AlarmChecker(db=db, weather_service=weather, dm_channel=dm, post_channel=None)
        rule = _make_rule(operator="gte", threshold=90.0, is_public=True)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        dm.send.assert_not_called()
        db.update_alarm_fired.assert_not_called()

    def test_does_not_record_fire_when_public_post_fails(self):
        checker, db, dm, post = self._checker_with_post()
        post.send.return_value = NotificationResult(success=False, channel="bluesky_post", error="boom")
        rule = _make_rule(operator="gte", threshold=90.0, is_public=True)
        report = _make_report(temp_f=95.0)

        checker._evaluate_and_maybe_fire(rule, report)

        post.send.assert_called_once()
        db.update_alarm_fired.assert_not_called()
