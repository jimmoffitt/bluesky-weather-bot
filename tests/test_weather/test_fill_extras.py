"""
Tests for filling non-core climate variables after the fact:
ClimateDatabase's field_fills helpers and backfill_climate.py --fill-extras.
No network: the archive client is faked.
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

from bluesky_weather_bot.storage.climate_db import ClimateDatabase
from bluesky_weather_bot.weather import archive_client as ac

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_climate.py"
_spec = importlib.util.spec_from_file_location("backfill_climate", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

KEY = "39.74:-104.99"
LOC = {"location_key": KEY, "lat": 39.74, "lon": -104.99,
       "display_name": "Denver, CO", "zip_code": None, "timezone": "America/Denver"}


def _row(day: str, **extra) -> dict:
    row = {"location_key": KEY, "zip_code": None, "display_name": "Denver, CO",
           "date": day, "source": "test", "fetched_at": "t"}
    for _, col in ac.DAILY_FIELD_MAP:
        row[col] = None
    row["temp_max_f"] = 70.0
    row.update(extra)
    return row


@pytest.fixture
def cdb(tmp_path):
    with ClimateDatabase(tmp_path / "climate.db") as c:
        yield c


class FakeClient:
    """Returns one value per requested field for every day in the range."""

    def __init__(self, value=1.5, fail=False):
        self.calls: list[dict] = []
        self.value = value
        self.fail = fail
        self.call_count = 0
        self.weighted_calls = 0.0

    def fetch_daily(self, *, start_date, end_date, fields, **kw):
        self.calls.append({"start": start_date, "end": end_date, "fields": fields})
        self.call_count += 1
        if self.fail:
            raise RuntimeError("HTTP Error 429")
        d, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        out = []
        while d <= end:
            r = {"location_key": KEY, "date": d.isoformat()}
            for _, col in ac.DAILY_FIELD_MAP:
                r[col] = None
            for f in fields:
                r[ac.API_TO_COLUMN[f]] = self.value
            out.append(r)
            d += timedelta(days=1)
        return out


class TestUnfilledYears:
    def test_null_column_needs_filling(self, cdb):
        cdb.insert_daily_records([_row("2001-03-01"), _row("2002-03-01")])
        assert cdb.get_unfilled_years(KEY, ["dew_point_mean_f"]) == {
            2001: {"dew_point_mean_f"}, 2002: {"dew_point_mean_f"},
        }

    def test_rows_that_already_have_values_never_need_filling(self, cdb):
        """The 4 cities fetched with all 47 variables have no fill records."""
        cdb.insert_daily_records([_row("2001-03-01", dew_point_mean_f=40.0)])
        assert cdb.get_unfilled_years(KEY, ["dew_point_mean_f"]) == {}

    def test_recorded_fill_covers_genuine_nulls(self, cdb):
        cdb.insert_daily_records([_row("2001-03-01")])
        cdb.record_field_fill(KEY, 2001, ["dew_point_mean_f"], "2001-12-31")
        assert cdb.get_unfilled_years(KEY, ["dew_point_mean_f"]) == {}

    def test_rows_added_after_a_fill_need_filling_again(self, cdb):
        """The current year keeps growing after it's been filled once."""
        cdb.insert_daily_records([_row("2026-03-01")])
        cdb.record_field_fill(KEY, 2026, ["dew_point_mean_f"], "2026-03-01")
        cdb.insert_daily_records([_row("2026-04-01")])
        assert cdb.get_unfilled_years(KEY, ["dew_point_mean_f"]) == {2026: {"dew_point_mean_f"}}

    def test_unknown_column_rejected(self, cdb):
        with pytest.raises(ValueError):
            cdb.get_unfilled_years(KEY, ["dew_point_mean_f; DROP TABLE weather_daily"])


class TestUpdateDailyFields:
    def test_updates_only_named_columns_on_existing_rows(self, cdb):
        cdb.insert_daily_records([_row("2001-03-01")])
        n = cdb.update_daily_fields(
            [{"location_key": KEY, "date": "2001-03-01", "dew_point_mean_f": 40.0, "temp_max_f": None},
             {"location_key": KEY, "date": "2001-03-02", "dew_point_mean_f": 41.0}],
            ["dew_point_mean_f"],
        )
        assert n == 1  # the 03-02 record has no row, so nothing is inserted
        row = cdb.get_daily_records(KEY, "2001-01-01", "2001-12-31")
        assert len(row) == 1
        assert row[0]["dew_point_mean_f"] == 40.0
        assert row[0]["temp_max_f"] == 70.0  # core value untouched


class TestPackFields:
    def test_packs_into_batches_of_ten(self):
        assert [len(b) for b in ac.pack_fields(list("abcdefghijklmnopqrstuvwxy"))] == [10, 10, 5]

    def test_all_extras_take_four_requests(self):
        all_extras = [f for g in ac.EXTRA_FIELD_GROUPS.values() for f in g]
        assert len(all_extras) == 37
        assert len(ac.pack_fields(all_extras)) == 4


class TestBackfillFillExtras:
    def test_fills_records_and_skips_on_rerun(self, cdb):
        cdb.insert_daily_records([_row("2001-03-01"), _row("2001-03-02")])
        client = FakeClient()
        backfill.backfill_fill_extras(cdb, client, [LOC], ["wind"], dry_run=False, max_calls=None)

        assert len(client.calls) == 1
        assert client.calls[0]["fields"] == ac.EXTRA_FIELD_GROUPS["wind"]
        rows = cdb.get_daily_records(KEY, "2001-01-01", "2001-12-31")
        assert all(r["wind_speed_mean_mph"] == 1.5 for r in rows)
        assert all(r["temp_max_f"] == 70.0 for r in rows)

        backfill.backfill_fill_extras(cdb, client, [LOC], ["wind"], dry_run=False, max_calls=None)
        assert len(client.calls) == 1  # nothing left to fill

    def test_groups_are_packed_together_across_groups(self, cdb):
        cdb.insert_daily_records([_row("2001-03-01")])
        client = FakeClient()
        backfill.backfill_fill_extras(cdb, client, [LOC], ["wind", "agriculture"], dry_run=False, max_calls=None)
        assert len(client.calls) == 1  # 3 + 3 vars fit in one request
        assert len(client.calls[0]["fields"]) == 6

    def test_genuine_api_nulls_are_not_rerequested(self, cdb):
        cdb.insert_daily_records([_row("2001-03-01")])
        client = FakeClient(value=None)
        backfill.backfill_fill_extras(cdb, client, [LOC], ["soil"], dry_run=False, max_calls=None)
        backfill.backfill_fill_extras(cdb, client, [LOC], ["soil"], dry_run=False, max_calls=None)
        assert len(client.calls) == 1

    def test_failed_request_is_retried_next_run(self, cdb):
        cdb.insert_daily_records([_row("2001-03-01")])
        backfill.backfill_fill_extras(cdb, FakeClient(fail=True), [LOC], ["wind"], dry_run=False, max_calls=None)
        assert cdb.get_unfilled_years(KEY, ["wind_speed_mean_mph"]) == {2001: {"wind_speed_mean_mph"}}

    def test_dry_run_makes_no_requests(self, cdb, capsys):
        cdb.insert_daily_records([_row("2001-03-01")])
        client = FakeClient()
        backfill.backfill_fill_extras(cdb, client, [LOC], list(ac.EXTRA_FIELD_GROUPS), dry_run=True, max_calls=None)
        assert client.calls == []
        out = capsys.readouterr().out
        assert "Would make 4 request(s)" in out
        assert "weighted calls" in out

    def test_newest_year_first(self, cdb):
        cdb.insert_daily_records([_row("1999-03-01"), _row("2003-03-01"), _row("2001-03-01")])
        client = FakeClient()
        backfill.backfill_fill_extras(cdb, client, [LOC], ["wind"], dry_run=False, max_calls=None)
        assert [c["start"][:4] for c in client.calls] == ["2003", "2001", "1999"]


class TestLock:
    def test_second_backfill_is_refused(self, tmp_path):
        db_path = tmp_path / "climate.db"
        held = backfill.acquire_backfill_lock(db_path)
        try:
            import subprocess, sys
            code = (
                "import importlib.util, sys; from pathlib import Path\n"
                f"spec = importlib.util.spec_from_file_location('b', {str(_SCRIPT)!r})\n"
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                f"m.acquire_backfill_lock(Path({str(db_path)!r}))\n"
            )
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            assert result.returncode == 2
            assert "already running" in result.stderr
        finally:
            held.close()
