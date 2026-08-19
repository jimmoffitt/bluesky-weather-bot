"""
Open-Meteo API client.

Fetches current conditions, 6-hour hourly forecast, and optional historical
daily data for a given lat/lon. No API key required; uses only stdlib
(urllib.request + json) to avoid extra dependencies.

When skip_historical=False, two API calls are made:
  1. current + hourly (6 slots)
  2. daily (past_days=380) for historical comparison
This avoids the massive hourly payload that would result from combining
past_days=380 with the hourly variable in one call.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from typing import Optional

from bluesky_weather_bot.weather.models import (
    WeatherReport,
    CurrentConditions,
    HourlyForecastSlot,
    Forecast,
    DailyForecastSlot,
    DailyForecast,
    DailyHistoricalRecord,
    HistoricalComparison,
    ResolvedLocation,
    wmo_description,
)

logger = logging.getLogger(__name__)

BASE_URL    = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "cloud_cover,wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
    "precipitation,visibility,surface_pressure,weather_code"
)

HOURLY_VARS = (
    "temperature_2m,precipitation_probability,precipitation,"
    "wind_speed_10m,cloud_cover,weather_code,relative_humidity_2m"
)

DAILY_VARS = (
    "temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
    "precipitation_sum,wind_speed_10m_max"
)

DAILY_FORECAST_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_sum,precipitation_probability_max,"
    "wind_speed_10m_max,sunrise,sunset"
)


class WeatherClient:
    """Fetches weather data from Open-Meteo and returns a WeatherReport."""

    def fetch(
        self,
        lat: float,
        lon: float,
        timezone: str,
        skip_historical: bool = False,
    ) -> WeatherReport:
        """
        Fetch weather for the given coordinates.

        Returns a WeatherReport with a placeholder location (display_name='').
        The caller (WeatherService) fills in the real location after fetch.
        """
        data = self._fetch_current_and_hourly(lat, lon, timezone)

        # Rename daily forecast key before potentially overwriting with archive daily data
        data["daily_forecast"] = data.pop("daily", {})

        if not skip_historical:
            daily_data = self._fetch_daily(lat, lon, timezone)
            data["daily"] = daily_data.get("daily", {})

        location = ResolvedLocation(
            lat=lat, lon=lon, display_name="", timezone=timezone
        )
        return WeatherReport(
            location=location,
            current=self._parse_current(data),
            forecast=self._parse_forecast(data),
            historical=self._parse_historical(data, skip_historical),
            daily_forecast=self._parse_daily_forecast(data),
        )

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def _fetch_current_and_hourly(self, lat: float, lon: float, timezone: str) -> dict:
        """One API call covers current conditions + the 12-hour and 7-day
        forecasts — Open-Meteo returns all three from the same endpoint."""
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": CURRENT_VARS,
            "hourly": HOURLY_VARS,
            "daily": DAILY_FORECAST_VARS,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": timezone,
            "forecast_hours": 12,
            "forecast_days": 7,
        }
        return self._get(params)

    def _fetch_daily(self, lat: float, lon: float, timezone: str) -> dict:
        """Fetches the year-ago-to-now daily archive, used to build the
        year-ago and 10-year-average historical comparison."""
        from datetime import date, timedelta
        end_date   = (date.today() - timedelta(days=1)).isoformat()
        start_date = (date.today() - timedelta(days=380)).isoformat()
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date,
            "end_date": end_date,
            "daily": DAILY_VARS,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": timezone,
        }
        return self._get_archive(params)

    def _get(self, params: dict) -> dict:
        """GET against the current-conditions/forecast endpoint."""
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        logger.debug("Open-Meteo: %s", url)
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read())

    def _get_archive(self, params: dict) -> dict:
        """GET against the historical-archive endpoint (separate API from current/forecast)."""
        url = ARCHIVE_URL + "?" + urllib.parse.urlencode(params)
        logger.debug("Open-Meteo archive: %s", url)
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read())

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_current(self, data: dict) -> CurrentConditions:
        """Parses the "current" block of the API response into CurrentConditions."""
        c = data["current"]
        temp_f   = _f(c.get("temperature_2m"))
        feels_f  = _f(c.get("apparent_temperature"))
        wind_mph  = _f(c.get("wind_speed_10m"))
        gusts_mph = _f(c.get("wind_gusts_10m"))
        precip_in = _f(c.get("precipitation"))
        vis_m     = _f(c.get("visibility"))

        return CurrentConditions(
            timestamp=datetime.fromisoformat(c["time"]),
            temperature_f=temp_f,
            temperature_c=_f_to_c(temp_f),
            feels_like_f=feels_f,
            feels_like_c=_f_to_c(feels_f),
            humidity_pct=_f(c.get("relative_humidity_2m")),
            cloud_cover_pct=_f(c.get("cloud_cover")),
            wind_speed_mph=wind_mph,
            wind_speed_kph=_mph_to_kph(wind_mph),
            wind_direction_deg=_f(c.get("wind_direction_10m", 0)),
            wind_gusts_mph=gusts_mph,
            wind_gusts_kph=_mph_to_kph(gusts_mph),
            precipitation_in=precip_in,
            precipitation_mm=_in_to_mm(precip_in),
            visibility_miles=_m_to_miles(vis_m),
            visibility_km=round(vis_m / 1000.0, 1) if vis_m else 0.0,
            surface_pressure_hpa=_f(c.get("surface_pressure")),
            weather_description=wmo_description(int(c.get("weather_code", 0))),
        )

    def _parse_forecast(self, data: dict) -> Forecast:
        """Parses the "hourly" block into a full Forecast (all returned
        slots — truncation to "next N hours" happens at render time)."""
        h = data.get("hourly", {})
        times        = h.get("time", [])
        temps        = h.get("temperature_2m", [])
        precip_probs = h.get("precipitation_probability", [])
        precips      = h.get("precipitation", [])
        winds        = h.get("wind_speed_10m", [])
        covers       = h.get("cloud_cover", [])
        codes        = h.get("weather_code", [])
        humidities   = h.get("relative_humidity_2m", [])

        slots = []
        for i, t in enumerate(times):
            temp_f    = _f(_at(temps, i))
            precip_in = _f(_at(precips, i))
            wind_mph  = _f(_at(winds, i))
            slots.append(HourlyForecastSlot(
                hour=datetime.fromisoformat(t),
                temperature_f=temp_f,
                temperature_c=_f_to_c(temp_f),
                precipitation_probability_pct=_f(_at(precip_probs, i)),
                precipitation_in=precip_in,
                precipitation_mm=_in_to_mm(precip_in),
                wind_speed_mph=wind_mph,
                wind_speed_kph=_mph_to_kph(wind_mph),
                cloud_cover_pct=_f(_at(covers, i)),
                weather_description=wmo_description(int(_at(codes, i) or 0)),
                humidity_pct=_f(_at(humidities, i)),
            ))
        return Forecast(slots=slots)

    def _parse_historical(self, data: dict, skip_historical: bool) -> HistoricalComparison:
        """
        Builds the year-ago-exact-date record and the ±7-day 10-year
        average from the daily archive block. Returns an empty
        HistoricalComparison if skip_historical is set (SKIP_HISTORICAL env
        var, for faster local development) or the archive call was skipped.
        """
        if skip_historical or "daily" not in data:
            return HistoricalComparison()

        daily    = data["daily"]
        times    = daily.get("time", [])
        max_t    = daily.get("temperature_2m_max", [])
        min_t    = daily.get("temperature_2m_min", [])
        mean_t   = daily.get("temperature_2m_mean", [])
        precips  = daily.get("precipitation_sum", [])
        winds    = daily.get("wind_speed_10m_max", [])

        today           = date.today()
        year_ago_target = today - timedelta(days=365)
        today_doy       = today.timetuple().tm_yday

        year_ago_record: Optional[DailyHistoricalRecord] = None
        avg_bucket: list[DailyHistoricalRecord] = []

        for i, t in enumerate(times):
            d = date.fromisoformat(t)
            if d > today:
                continue  # skip future dates

            max_f    = _f(_at(max_t, i))
            min_f    = _f(_at(min_t, i))
            mean_f   = _f(_at(mean_t, i))
            prec_in  = _f(_at(precips, i))
            wind_mph = _f(_at(winds, i))

            rec = DailyHistoricalRecord(
                date=datetime.combine(d, datetime.min.time()),
                temp_max_f=max_f,
                temp_max_c=_f_to_c(max_f),
                temp_min_f=min_f,
                temp_min_c=_f_to_c(min_f),
                temp_mean_f=mean_f,
                temp_mean_c=_f_to_c(mean_f),
                precipitation_in=prec_in,
                precipitation_mm=_in_to_mm(prec_in),
                wind_speed_max_mph=wind_mph,
                wind_speed_max_kph=_mph_to_kph(wind_mph),
            )

            if d == year_ago_target:
                year_ago_record = rec

            # ±7 day-of-year band (wraps around year boundary)
            record_doy = d.timetuple().tm_yday
            diff = abs(record_doy - today_doy)
            diff = min(diff, 365 - diff)
            if diff <= 7:
                avg_bucket.append(rec)

        ten_year_avg: Optional[DailyHistoricalRecord] = None
        if avg_bucket:
            n = len(avg_bucket)
            avg_max_f  = sum(r.temp_max_f for r in avg_bucket) / n
            avg_min_f  = sum(r.temp_min_f for r in avg_bucket) / n
            avg_mean_f = sum(r.temp_mean_f for r in avg_bucket) / n
            avg_prec   = sum(r.precipitation_in for r in avg_bucket) / n
            avg_wind   = sum(r.wind_speed_max_mph for r in avg_bucket) / n

            ten_year_avg = DailyHistoricalRecord(
                date=datetime.combine(today, datetime.min.time()),
                temp_max_f=avg_max_f,
                temp_max_c=_f_to_c(avg_max_f),
                temp_min_f=avg_min_f,
                temp_min_c=_f_to_c(avg_min_f),
                temp_mean_f=avg_mean_f,
                temp_mean_c=_f_to_c(avg_mean_f),
                precipitation_in=avg_prec,
                precipitation_mm=_in_to_mm(avg_prec),
                wind_speed_max_mph=avg_wind,
                wind_speed_max_kph=_mph_to_kph(avg_wind),
            )

        return HistoricalComparison(year_ago=year_ago_record, ten_year_avg=ten_year_avg)

    def _parse_daily_forecast(self, data: dict) -> DailyForecast:
        """Parses the 7-day daily forecast block (distinct from
        _parse_historical, which covers the past, not the future)."""
        df = data.get("daily_forecast", {})
        times    = df.get("time", [])
        codes    = df.get("weather_code", [])
        max_t    = df.get("temperature_2m_max", [])
        min_t    = df.get("temperature_2m_min", [])
        precips  = df.get("precipitation_sum", [])
        prec_pct = df.get("precipitation_probability_max", [])
        winds    = df.get("wind_speed_10m_max", [])
        sunrises = df.get("sunrise", [])
        sunsets  = df.get("sunset", [])

        slots = []
        for i, t in enumerate(times):
            max_f    = _f(_at(max_t, i))
            min_f    = _f(_at(min_t, i))
            prec_in  = _f(_at(precips, i))
            wind_mph = _f(_at(winds, i))
            sr_raw   = _at(sunrises, i)
            ss_raw   = _at(sunsets, i)
            slots.append(DailyForecastSlot(
                date=datetime.fromisoformat(t),
                temp_max_f=max_f,
                temp_max_c=_f_to_c(max_f),
                temp_min_f=min_f,
                temp_min_c=_f_to_c(min_f),
                precipitation_probability_max_pct=_f(_at(prec_pct, i)),
                precipitation_in=prec_in,
                precipitation_mm=_in_to_mm(prec_in),
                wind_speed_max_mph=wind_mph,
                wind_speed_max_kph=_mph_to_kph(wind_mph),
                weather_description=wmo_description(int(_at(codes, i) or 0)),
                sunrise=datetime.fromisoformat(sr_raw) if sr_raw else None,
                sunset=datetime.fromisoformat(ss_raw) if ss_raw else None,
            ))
        return DailyForecast(slots=slots)

    # ------------------------------------------------------------------
    # "This day in history" — ERA5 fetch across all available years
    # ------------------------------------------------------------------

    def fetch_this_day_history(
        self,
        lat: float,
        lon: float,
        timezone: str,
        month: int,
        day: int,
        start_year: int = 1950,
    ) -> list[DailyHistoricalRecord]:
        """
        Fetch ERA5 daily data from start_year to last year, then filter to
        entries matching (month, day). Returns one DailyHistoricalRecord per
        year that has data for that date.

        One API call returns the full date range; client-side filtering keeps
        only matching MM-DD rows (~75 data points for 75 years).
        """
        today = date.today()
        start = date(start_year, month, day)
        # end = yesterday to ensure complete daily data
        end   = date(today.year - 1, 12, 31)
        if end < start:
            return []

        params = {
            "latitude":           lat,
            "longitude":          lon,
            "start_date":         start.isoformat(),
            "end_date":           end.isoformat(),
            "daily":              "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
            "temperature_unit":   "fahrenheit",
            "wind_speed_unit":    "mph",
            "precipitation_unit": "inch",
            "timezone":           timezone,
        }
        try:
            data   = self._get_archive(params)
        except Exception as e:
            logger.warning("fetch_this_day_history failed: %s", e)
            return []

        daily   = data.get("daily", {})
        times   = daily.get("time", [])
        max_t   = daily.get("temperature_2m_max", [])
        min_t   = daily.get("temperature_2m_min", [])
        precips = daily.get("precipitation_sum", [])
        winds   = daily.get("wind_speed_10m_max", [])

        records: list[DailyHistoricalRecord] = []
        for i, t in enumerate(times):
            d = date.fromisoformat(t)
            if d.month != month or d.day != day:
                continue
            max_f    = _f(_at(max_t, i))
            min_f    = _f(_at(min_t, i))
            prec_in  = _f(_at(precips, i))
            wind_mph = _f(_at(winds, i))
            records.append(DailyHistoricalRecord(
                date=datetime.combine(d, datetime.min.time()),
                temp_max_f=max_f,
                temp_max_c=_f_to_c(max_f),
                temp_min_f=min_f,
                temp_min_c=_f_to_c(min_f),
                temp_mean_f=(max_f + min_f) / 2,
                temp_mean_c=_f_to_c((max_f + min_f) / 2),
                precipitation_in=prec_in,
                precipitation_mm=_in_to_mm(prec_in),
                wind_speed_max_mph=wind_mph,
                wind_speed_max_kph=_mph_to_kph(wind_mph),
            ))
        return records


# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------

def _f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


def _mph_to_kph(mph: float) -> float:
    return round(mph * 1.60934, 1)


def _in_to_mm(inches: float) -> float:
    return round(inches * 25.4, 1)


def _m_to_miles(m: float) -> float:
    return round(m / 1609.34, 1)


def _f(v) -> float:
    """Safely cast to float; return 0.0 for None/null."""
    return float(v) if v is not None else 0.0


def _at(lst: list, i: int):
    """Safe list access; returns None if out of bounds."""
    return lst[i] if i < len(lst) else None
