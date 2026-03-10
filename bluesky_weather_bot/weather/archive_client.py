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

DAILY_VARS = (
    "temperature_2m_max,"
    "temperature_2m_min,"
    "temperature_2m_mean,"
    "precipitation_sum,"
    "snowfall_sum,"
    "wind_speed_10m_max,"
    "weather_code"
)

# Open-Meteo free tier allows ~10 000 req/day; this keeps us polite
DEFAULT_RETRY_WAIT_SEC = 2
MAX_RETRIES = 3


class ArchiveClient:
    """Fetches multi-year daily weather records from Open-Meteo archive API."""

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
        years: int = 10,
        location_key: str = "",
        display_name: Optional[str] = None,
        zip_code: Optional[str] = None,
        timezone: str = "auto",
    ) -> list[dict]:
        """
        Convenience wrapper: fetch the last N years through yesterday.

        Splits into annual chunks to stay within Open-Meteo's recommended
        request size and to allow progress logging.
        """
        today      = date.today()
        yesterday  = today - timedelta(days=1)
        start_year = today.year - years

        all_records: list[dict] = []

        for year in range(start_year, today.year + 1):
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
        url = ARCHIVE_URL + "?" + urllib.parse.urlencode(params)
        logger.debug("Archive API: %s", url)
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
        daily    = data.get("daily", {})
        times    = daily.get("time", [])
        max_t    = daily.get("temperature_2m_max", [])
        min_t    = daily.get("temperature_2m_min", [])
        mean_t   = daily.get("temperature_2m_mean", [])
        precip   = daily.get("precipitation_sum", [])
        snow     = daily.get("snowfall_sum", [])
        wind     = daily.get("wind_speed_10m_max", [])
        codes    = daily.get("weather_code", [])

        fetched_at = datetime.utcnow().isoformat()
        records: list[dict] = []

        for i, t in enumerate(times):
            records.append({
                "location_key":      location_key,
                "zip_code":          zip_code,
                "display_name":      display_name,
                "date":              t,
                "temp_max_f":        _f(_at(max_t, i)),
                "temp_min_f":        _f(_at(min_t, i)),
                "temp_mean_f":       _f(_at(mean_t, i)),
                "precipitation_in":  _f(_at(precip, i)),
                "snowfall_in":       _f(_at(snow, i)),
                "wind_speed_max_mph": _f(_at(wind, i)),
                "weather_code":      _at(codes, i),
                "source":            "open-meteo-archive",
                "fetched_at":        fetched_at,
            })

        return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _f(v) -> Optional[float]:
    """Return float or None (preserve None rather than defaulting to 0.0)."""
    return float(v) if v is not None else None


def _at(lst: list, i: int):
    return lst[i] if i < len(lst) else None
