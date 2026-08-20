"""
Open-Meteo Historical Weather Archive client.

Uses the archive endpoint (archive-api.open-meteo.com/v1/archive), which:
  - Is free, no API key required
  - Covers 1940-01-01 through approximately yesterday
  - Accepts arbitrary start_date / end_date ranges
  - Returns daily variables (no hourly in this client)

Typical usage:
    client = ArchiveClient()
    records = client.fetch_daily(
        lat=40.17, lon=-105.10,
        start_date="2015-01-01",
        end_date="2024-12-31",
        location_key="40.17:-105.10",
        display_name="Longmont, CO",
        zip_code="80501",
    )
    # records is a list[dict] ready for ClimateDatabase.insert_daily_records()
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
EARLIEST_YEAR = 1940   # ERA5 reanalysis coverage start — uniform globally, not location-dependent

# (Open-Meteo API parameter name, weather_daily column name) for every daily
# field we store. Single source of truth: DAILY_VARS, the API request, the
# response parser, and ClimateDatabase's schema/insert are all built from
# this list, so adding a field is a one-line change here rather than four
# hand-kept lists that can drift out of sync.
#
# Excludes visibility_mean/max/min and uv_index_max/uv_index_clear_sky_max —
# valid parameter names the API accepts, but verified (live, across 1960 /
# 2000 / 2020 / 2025 samples) to always return null on the daily archive
# endpoint. Not worth a schema column that's permanently empty.
#
# Units: requested in fahrenheit/mph/inch (see fetch_daily); confirmed via
# the API's own daily_units response, not assumed. growing_degree_days is
# the one exception — Open-Meteo returns it in fixed Celsius-based units
# ("GGDc") regardless of temperature_unit.
DAILY_FIELD_MAP: list[tuple[str, str]] = [
    ("weather_code",                        "weather_code"),
    ("temperature_2m_max",                  "temp_max_f"),
    ("temperature_2m_min",                  "temp_min_f"),
    ("temperature_2m_mean",                 "temp_mean_f"),
    ("apparent_temperature_max",            "apparent_temp_max_f"),
    ("apparent_temperature_min",            "apparent_temp_min_f"),
    ("apparent_temperature_mean",           "apparent_temp_mean_f"),
    ("precipitation_sum",                   "precipitation_in"),
    ("rain_sum",                            "rain_in"),
    ("snowfall_sum",                        "snowfall_in"),
    ("precipitation_hours",                 "precipitation_hours"),
    ("sunrise",                             "sunrise"),
    ("sunset",                              "sunset"),
    ("sunshine_duration",                   "sunshine_duration_sec"),
    ("daylight_duration",                   "daylight_duration_sec"),
    ("wind_speed_10m_max",                  "wind_speed_max_mph"),
    ("wind_gusts_10m_max",                  "wind_gusts_max_mph"),
    ("wind_direction_10m_dominant",         "wind_direction_dominant_deg"),
    ("shortwave_radiation_sum",             "shortwave_radiation_mj"),
    ("et0_fao_evapotranspiration",          "et0_evapotranspiration_in"),
    ("cloud_cover_mean",                    "cloud_cover_mean_pct"),
    ("cloud_cover_max",                     "cloud_cover_max_pct"),
    ("cloud_cover_min",                     "cloud_cover_min_pct"),
    ("dew_point_2m_mean",                   "dew_point_mean_f"),
    ("dew_point_2m_max",                    "dew_point_max_f"),
    ("dew_point_2m_min",                    "dew_point_min_f"),
    ("relative_humidity_2m_mean",           "humidity_mean_pct"),
    ("relative_humidity_2m_max",            "humidity_max_pct"),
    ("relative_humidity_2m_min",            "humidity_min_pct"),
    ("pressure_msl_mean",                   "pressure_msl_mean_hpa"),
    ("pressure_msl_max",                    "pressure_msl_max_hpa"),
    ("pressure_msl_min",                    "pressure_msl_min_hpa"),
    ("surface_pressure_mean",               "surface_pressure_mean_hpa"),
    ("surface_pressure_max",                "surface_pressure_max_hpa"),
    ("surface_pressure_min",                "surface_pressure_min_hpa"),
    ("wind_speed_10m_mean",                 "wind_speed_mean_mph"),
    ("wind_gusts_10m_mean",                 "wind_gusts_mean_mph"),
    ("soil_moisture_0_to_7cm_mean",         "soil_moisture_0_7cm"),
    ("soil_moisture_7_to_28cm_mean",        "soil_moisture_7_28cm"),
    ("soil_moisture_28_to_100cm_mean",      "soil_moisture_28_100cm"),
    ("soil_moisture_100_to_255cm_mean",     "soil_moisture_100_255cm"),
    ("soil_temperature_0_to_7cm_mean",      "soil_temp_0_7cm_f"),
    ("soil_temperature_7_to_28cm_mean",     "soil_temp_7_28cm_f"),
    ("soil_temperature_28_to_100cm_mean",   "soil_temp_28_100cm_f"),
    ("soil_temperature_100_to_255cm_mean",  "soil_temp_100_255cm_f"),
    ("wet_bulb_temperature_2m_mean",        "wet_bulb_temp_mean_f"),
    ("growing_degree_days_base_0_limit_50", "growing_degree_days"),
]

# Fields whose value should be stored as-is (int/text), not coerced to float
_NON_FLOAT_FIELDS = {"weather_code", "sunrise", "sunset"}

DAILY_VARS = ",".join(api_name for api_name, _ in DAILY_FIELD_MAP)

# Open-Meteo free tier allows ~10 000 req/day; this keeps us polite
DEFAULT_RETRY_WAIT_SEC = 2
MAX_RETRIES = 3


class ArchiveClient:
    """Fetches multi-year daily weather records from Open-Meteo archive API."""

    def __init__(self) -> None:
        # Incremented once per real HTTP request in _get() (including
        # retries — each is a real request against the quota). Lets
        # fetch_daily_years()'s max_calls stop a backfill before it
        # exceeds Open-Meteo's daily call quota.
        self.call_count = 0

    def fetch_daily(
        self,
        lat: float,
        lon: float,
        start_date: str,          # YYYY-MM-DD
        end_date: str,            # YYYY-MM-DD
        location_key: str,
        display_name: Optional[str] = None,
        zip_code: Optional[str] = None,
        timezone: str = "auto",
    ) -> list[dict]:
        """
        Fetch daily historical records for a location and date range.

        Returns a list of dicts ready for ClimateDatabase.insert_daily_records().
        Fields: location_key, zip_code, display_name, date,
                temp_max_f, temp_min_f, temp_mean_f,
                precipitation_in, snowfall_in, wind_speed_max_mph,
                weather_code, source, fetched_at.
        """
        data = self._get({
            "latitude":          lat,
            "longitude":         lon,
            "start_date":        start_date,
            "end_date":          end_date,
            "daily":             DAILY_VARS,
            "temperature_unit":  "fahrenheit",
            "wind_speed_unit":   "mph",
            "precipitation_unit": "inch",
            "timezone":          timezone,
        })

        return self._parse(
            data,
            location_key=location_key,
            display_name=display_name,
            zip_code=zip_code,
        )

    def fetch_daily_years(
        self,
        lat: float,
        lon: float,
        years: Optional[int] = None,
        location_key: str = "",
        display_name: Optional[str] = None,
        zip_code: Optional[str] = None,
        timezone: str = "auto",
        skip_years: Optional[set[int]] = None,
        max_calls: Optional[int] = None,
    ) -> list[dict]:
        """
        Convenience wrapper: fetch years years through yesterday, or the
        full archive back to EARLIEST_YEAR (1940) if years is None.

        Splits into annual chunks to stay within Open-Meteo's recommended
        request size and to allow progress logging.

        skip_years omits years entirely (no API call at all) — used to
        resume a backfill without re-fetching years already in the DB.
        max_calls stops fetching (returning whatever was collected so far,
        not an error) once self.call_count would reach it, so a
        multi-location backfill can respect a daily API-call quota and
        stop cleanly mid-location rather than mid-year-request.
        """
        today      = date.today()
        yesterday  = today - timedelta(days=1)
        start_year = EARLIEST_YEAR if years is None else today.year - years
        skip_years = skip_years or set()

        all_records: list[dict] = []

        for year in range(start_year, today.year + 1):
            if year in skip_years:
                continue
            if max_calls is not None and self.call_count >= max_calls:
                logger.info(
                    "Call budget (%d) reached — stopping mid-backfill for %s at year %d",
                    max_calls, display_name or location_key, year,
                )
                break

            chunk_start = date(year, 1, 1)
            chunk_end   = date(year, 12, 31)
            if chunk_end > yesterday:
                chunk_end = yesterday
            if chunk_start > yesterday:
                break

            logger.info(
                "Fetching %s  %s → %s  (%s)",
                display_name or location_key,
                chunk_start, chunk_end,
                f"{len(all_records)} rows so far",
            )
            records = self.fetch_daily(
                lat=lat, lon=lon,
                start_date=chunk_start.isoformat(),
                end_date=chunk_end.isoformat(),
                location_key=location_key,
                display_name=display_name,
                zip_code=zip_code,
                timezone=timezone,
            )
            all_records.extend(records)

            # Be polite between annual requests
            if year < today.year:
                time.sleep(0.2)

        return all_records

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, params: dict, attempt: int = 0) -> dict:
        """GET with exponential-backoff retry on rate limiting (HTTP 429)
        and transient network errors, up to MAX_RETRIES attempts."""
        url = ARCHIVE_URL + "?" + urllib.parse.urlencode(params)
        logger.debug("Archive API: %s", url)
        self.call_count += 1
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                wait = DEFAULT_RETRY_WAIT_SEC * (2 ** attempt)
                logger.warning("Rate limited; retrying in %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                return self._get(params, attempt + 1)
            raise
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES:
                wait = DEFAULT_RETRY_WAIT_SEC * (2 ** attempt)
                logger.warning("Network error (%s); retrying in %ds", e.reason, wait)
                time.sleep(wait)
                return self._get(params, attempt + 1)
            raise

    def _parse(
        self,
        data: dict,
        location_key: str,
        display_name: Optional[str],
        zip_code: Optional[str],
    ) -> list[dict]:
        """Flattens one archive API response into a list of per-day record
        dicts, ready for bulk insert into the climate DB. Every field in
        DAILY_FIELD_MAP is pulled out generically — see that constant for
        the API-name-to-column mapping."""
        daily = data.get("daily", {})
        times = daily.get("time", [])

        # One array lookup per field up front, not per row — avoids 47
        # dict.get() calls inside the per-day loop below.
        field_series = {
            db_col: daily.get(api_name, [])
            for api_name, db_col in DAILY_FIELD_MAP
        }

        fetched_at = datetime.utcnow().isoformat()
        records: list[dict] = []

        for i, t in enumerate(times):
            record = {
                "location_key": location_key,
                "zip_code":     zip_code,
                "display_name": display_name,
                "date":         t,
                "source":       "open-meteo-archive",
                "fetched_at":   fetched_at,
            }
            for db_col, series in field_series.items():
                v = _at(series, i)
                record[db_col] = v if db_col in _NON_FLOAT_FIELDS else _f(v)
            records.append(record)

        return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _f(v) -> Optional[float]:
    """Return float or None (preserve None rather than defaulting to 0.0)."""
    return float(v) if v is not None else None


def _at(lst: list, i: int):
    """Safe indexed access — returns None instead of raising past the end
    of a shorter-than-expected API response array."""
    return lst[i] if i < len(lst) else None
