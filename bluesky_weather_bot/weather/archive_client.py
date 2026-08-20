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
from typing import Callable, Optional

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

# Open-Meteo free tier allows ~10 000 req/day and ~600/min — figures
# that turned out to be far more optimistic than what's actually
# survivable in practice. Observed live, in order:
#   - 0.2s/0.5s pacing: every one of the first 4 locations failed outright
#   - flat 1.5s pacing: still hit a sustained throttle episode bad enough
#     to exhaust all 5 retries (5+10+20+40+80s = 155s) on a single year,
#     twice, within the very first location
# A fixed interval can't win this — too fast and it gets throttled, too
# slow and it's needlessly conservative during calm periods. So pacing is
# adaptive: MIN_REQUEST_INTERVAL_SEC is the starting point, but any 429
# grows the *baseline* interval (not just that one retry's backoff), and
# a run of clean successes relaxes it back down. See ArchiveClient's
# _current_interval.
MIN_REQUEST_INTERVAL_SEC = 1.5
MAX_REQUEST_INTERVAL_SEC = 20.0
INTERVAL_BACKOFF_MULTIPLIER = 1.8   # applied to the baseline on every 429
INTERVAL_RELAX_AFTER = 8            # consecutive successes before easing off
INTERVAL_RELAX_MULTIPLIER = 0.85
DEFAULT_RETRY_WAIT_SEC = 5
MAX_RETRIES = 5

# Even MAX_REQUEST_INTERVAL_SEC (20s, ~3/min — far under any published
# limit) turned out insufficient once: observed live, three consecutive
# *years* each burned all 5 retries (155s) and still failed, back to
# back, after ~25 minutes of sustained activity. That's a qualitatively
# different signal than an individual 429 — evidence of a longer-duration
# server-side penalty that per-request backoff alone can't out-wait. So a
# full retry exhaustion (not just a single 429) triggers its own,
# separately-escalating cooldown before the next request is even
# attempted, on top of the per-request interval above.
EXHAUSTION_COOLDOWN_BASE_SEC = 60.0
MAX_EXHAUSTION_COOLDOWN_SEC = 600.0


class ArchiveClient:
    """Fetches multi-year daily weather records from Open-Meteo archive API."""

    def __init__(self) -> None:
        # Incremented once per real HTTP request in _get() (including
        # retries — each is a real request against the quota). Lets
        # fetch_daily_years()'s max_calls stop a backfill before it
        # exceeds Open-Meteo's daily call quota.
        self.call_count = 0
        # Rate-limiting is centralized here (not scattered across every
        # caller's own sleep calls) so nothing can accidentally bypass it.
        self._last_request_at: Optional[float] = None
        # Current baseline gap between requests — grows on 429s, relaxes
        # after sustained success. See the module comment above.
        self._current_interval = MIN_REQUEST_INTERVAL_SEC
        self._consecutive_successes = 0
        # How many requests in a row have fully exhausted MAX_RETRIES —
        # resets on any success, escalates the exhaustion cooldown below.
        self._consecutive_exhaustions = 0

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
        newest_first: bool = True,
        on_year_complete: Optional[Callable[[list[dict]], None]] = None,
    ) -> list[dict]:
        """
        Convenience wrapper: fetch years years through yesterday, or the
        full archive back to EARLIEST_YEAR (1940) if years is None.

        Splits into annual chunks to stay within Open-Meteo's recommended
        request size and to allow progress logging. newest_first (default)
        fetches most-recent-year-first, so a multi-day backfill that gets
        cut off by max_calls has the most relevant/recent decades done
        first rather than sitting on 1940s data while recent years wait.

        skip_years omits years entirely (no API call at all) — used to
        resume a backfill without re-fetching years already in the DB.
        max_calls stops fetching (returning whatever was collected so far,
        not an error) once self.call_count would reach it, so a
        multi-location backfill can respect a daily API-call quota and
        stop cleanly mid-location rather than mid-year-request.
        on_year_complete, if given, is called with each year's records as
        soon as they're fetched (not batched to the end) — lets the
        caller persist progress incrementally, so a location that's
        taking a long time (e.g. due to adaptive backoff after repeated
        429s) doesn't lose everything already fetched if the process is
        killed or crashes before the whole location finishes.
        """
        today      = date.today()
        yesterday  = today - timedelta(days=1)
        start_year = EARLIEST_YEAR if years is None else today.year - years
        skip_years = skip_years or set()

        year_order = (
            range(today.year, start_year - 1, -1) if newest_first
            else range(start_year, today.year + 1)
        )

        all_records: list[dict] = []

        for year in year_order:
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
                # No data yet for this year (only possible for today.year,
                # e.g. on Jan 1st before any of the new year has passed).
                # continue, not break: in newest_first order this is the
                # *first* iteration, and a break would wrongly skip every
                # earlier year too.
                continue

            logger.info(
                "Fetching %s  %s → %s  (%s)",
                display_name or location_key,
                chunk_start, chunk_end,
                f"{len(all_records)} rows so far",
            )
            try:
                records = self.fetch_daily(
                    lat=lat, lon=lon,
                    start_date=chunk_start.isoformat(),
                    end_date=chunk_end.isoformat(),
                    location_key=location_key,
                    display_name=display_name,
                    zip_code=zip_code,
                    timezone=timezone,
                )
            except Exception as exc:
                # One year failing (e.g. retries exhausted on a stubborn
                # 429) shouldn't sacrifice every other year already
                # collected in all_records — log and move on. The failed
                # year stays out of the DB, so it's simply not in
                # get_years_present() next run and gets retried naturally.
                logger.warning(
                    "Failed to fetch %s year %d, skipping it this run: %s",
                    display_name or location_key, year, exc,
                )
                continue
            all_records.extend(records)
            if on_year_complete is not None:
                on_year_complete(records)

        return all_records

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, params: dict, attempt: int = 0) -> dict:
        """GET with exponential-backoff retry on rate limiting (HTTP 429)
        and transient network errors, up to MAX_RETRIES attempts. Every
        real request — including retries — waits for the current adaptive
        interval (self._current_interval) since the last one first."""
        if self._last_request_at is not None:
            wait = self._current_interval - (time.time() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)

        url = ARCHIVE_URL + "?" + urllib.parse.urlencode(params)
        logger.debug("Archive API: %s", url)
        self.call_count += 1
        self._last_request_at = time.time()
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                result = json.loads(resp.read())
            self._on_request_success()
            return result
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self._on_rate_limited()
                if attempt < MAX_RETRIES:
                    wait = DEFAULT_RETRY_WAIT_SEC * (2 ** attempt)
                    logger.warning("Rate limited; retrying in %ds (attempt %d)", wait, attempt + 1)
                    time.sleep(wait)
                    return self._get(params, attempt + 1)
                self._on_exhausted()
            raise
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES:
                wait = DEFAULT_RETRY_WAIT_SEC * (2 ** attempt)
                logger.warning("Network error (%s); retrying in %ds", e.reason, wait)
                time.sleep(wait)
                return self._get(params, attempt + 1)
            raise

    def _on_request_success(self) -> None:
        """After enough consecutive clean requests, ease the baseline
        interval back down toward MIN_REQUEST_INTERVAL_SEC — a throttling
        episode shouldn't leave every later request needlessly slow."""
        self._consecutive_successes += 1
        self._consecutive_exhaustions = 0
        if (self._consecutive_successes >= INTERVAL_RELAX_AFTER
                and self._current_interval > MIN_REQUEST_INTERVAL_SEC):
            self._current_interval = max(
                MIN_REQUEST_INTERVAL_SEC,
                self._current_interval * INTERVAL_RELAX_MULTIPLIER,
            )
            self._consecutive_successes = 0
            logger.debug("Pacing relaxed to %.1fs", self._current_interval)

    def _on_rate_limited(self) -> None:
        """A 429 means the *current baseline pace* is too fast, not just
        that this one request was unlucky — grow it for every subsequent
        request too, not only this retry's own backoff wait."""
        self._consecutive_successes = 0
        old = self._current_interval
        self._current_interval = min(
            MAX_REQUEST_INTERVAL_SEC,
            self._current_interval * INTERVAL_BACKOFF_MULTIPLIER,
        )
        if self._current_interval > old:
            logger.info("Pacing increased to %.1fs after rate limiting", self._current_interval)

    def _on_exhausted(self) -> None:
        """
        Called when a request is about to give up after MAX_RETRIES
        straight 429s — a stronger signal than any single 429 that
        per-request backoff isn't enough on its own. Sleeps a separate,
        escalating cooldown (independent of self._current_interval)
        before returning control to the caller, who will then either
        skip this year/request or try the next one — either way, the
        next attempt gets real breathing room instead of hitting the
        same wall immediately.
        """
        wait = min(
            EXHAUSTION_COOLDOWN_BASE_SEC * (2 ** self._consecutive_exhaustions),
            MAX_EXHAUSTION_COOLDOWN_SEC,
        )
        self._consecutive_exhaustions += 1
        logger.warning(
            "All %d retries exhausted — cooling down %.0fs before the next request",
            MAX_RETRIES, wait,
        )
        time.sleep(wait)

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
