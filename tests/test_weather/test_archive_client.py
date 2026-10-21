"""
Unit tests for ArchiveClient's request cost model and budget pacing.
No network: urlopen and time.sleep are patched.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from bluesky_weather_bot.weather import archive_client as ac


class TestRequestWeight:
    # The two worked examples from open-meteo.com/en/pricing
    def test_pricing_page_examples(self):
        assert ac.request_weight(15, 14) == pytest.approx(1.5)
        assert ac.request_weight(15, 28) == pytest.approx(3.0)

    def test_small_requests_floor_at_one_call(self):
        assert ac.request_weight(5, 1) == 1.0

    def test_old_47_var_year_was_about_122_calls(self):
        """Regression (2026-10-21): this is why the 10k/day quota ran out
        after ~80 backfill requests regardless of pacing."""
        assert ac.request_weight(47, 365) == pytest.approx(122.5, abs=0.1)

    def test_core_10_var_year_is_about_26_calls(self):
        assert ac.request_weight(10, 365) == pytest.approx(26.07, abs=0.01)


class TestRequestedFields:
    def test_at_most_ten_variables(self):
        """The 11th variable raises the cost of every request."""
        assert len(ac.DAILY_VARS.split(",")) <= 10

    def test_requested_fields_exist_in_schema(self):
        schema = {api for api, _ in ac.DAILY_FIELD_MAP}
        assert set(ac.REQUESTED_DAILY_FIELDS) <= schema

    @pytest.mark.parametrize("api_name", [
        "temperature_2m_max", "temperature_2m_min", "relative_humidity_2m_mean",
    ])
    def test_user_requested_fields_present(self, api_name):
        assert api_name in ac.REQUESTED_DAILY_FIELDS

    def test_unrequested_columns_parse_as_none(self):
        data = {"daily": {"time": ["2026-10-01"], "temperature_2m_max": [70.0]}}
        rows = ac.ArchiveClient()._parse(data, "k", None, None)
        assert rows[0]["temp_max_f"] == 70.0
        assert rows[0]["soil_moisture_0_7cm"] is None


def _fake_response():
    return io.BytesIO(json.dumps({"daily": {"time": []}}).encode())


class TestBudgetPacing:
    def test_second_request_waits_its_share_of_daily_budget(self):
        client = ac.ArchiveClient()
        params = {"daily": ac.DAILY_VARS, "start_date": "2001-01-01", "end_date": "2001-12-31"}
        with patch.object(ac.urllib.request, "urlopen", side_effect=lambda *a, **k: _fake_response()), \
             patch.object(ac.time, "sleep") as sleep, \
             patch.object(ac.time, "time", return_value=1000.0):
            client._get(params)
            client._get(params)

        expected = 86_400 * ac.request_weight(10, 365) / ac.DAILY_CALL_BUDGET
        assert sleep.call_args.args[0] == pytest.approx(expected)
        assert expected > ac.MIN_REQUEST_INTERVAL_SEC  # budget, not the floor, governs
        assert client.weighted_calls == pytest.approx(2 * ac.request_weight(10, 365))

    def test_budget_leaves_headroom_for_live_bot(self):
        assert ac.DAILY_CALL_BUDGET < ac.FREE_TIER_DAILY_CALLS
