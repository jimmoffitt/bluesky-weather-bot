"""
Tests for pure helper functions in bot.py (not the ZipWx class itself,
which needs live channel wiring to construct).
"""

from __future__ import annotations

from bot import _find_duplicate_alarm
from bluesky_weather_bot.alarms.models import AlarmRule


def _make_rule(**overrides) -> AlarmRule:
    defaults = dict(
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


class TestFindDuplicateAlarm:
    def test_finds_exact_match(self):
        existing = _make_rule(id=1)
        dup = _find_duplicate_alarm(_make_rule(), [existing])
        assert dup is existing

    def test_location_match_is_case_and_whitespace_insensitive(self):
        existing = _make_rule(id=1, location_raw="  DENVER, co  ")
        dup = _find_duplicate_alarm(_make_rule(location_raw="Denver, CO"), [existing])
        assert dup is existing

    def test_different_threshold_is_not_a_duplicate(self):
        existing = _make_rule(threshold=100.0)
        dup = _find_duplicate_alarm(_make_rule(threshold=90.0), [existing])
        assert dup is None

    def test_different_metric_is_not_a_duplicate(self):
        existing = _make_rule(metric="temp_current")
        dup = _find_duplicate_alarm(_make_rule(metric="wind_speed"), [existing])
        assert dup is None

    def test_different_operator_is_not_a_duplicate(self):
        existing = _make_rule(operator="gte")
        dup = _find_duplicate_alarm(_make_rule(operator="lt"), [existing])
        assert dup is None

    def test_different_location_is_not_a_duplicate(self):
        existing = _make_rule(location_raw="Denver, CO")
        dup = _find_duplicate_alarm(_make_rule(location_raw="Boulder, CO"), [existing])
        assert dup is None

    def test_no_existing_rules_returns_none(self):
        assert _find_duplicate_alarm(_make_rule(), []) is None
