"""
WeatherService — facade that ties LocationResolver + WeatherClient + Database cache.

Public API:
    svc = WeatherService(db, skip_historical=False)
    reports = svc.lookup("Denver, CO")   # list[WeatherReport]

Cache is stored as JSON in the weather_cache table (1-hour TTL managed by Database).
Serialization avoids the circular reference in ResolvedLocation.candidates by
dropping candidates when writing to the cache.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, date
from typing import Optional

from bluesky_weather_bot.storage.db import Database
from bluesky_weather_bot.weather.client import WeatherClient
from bluesky_weather_bot.weather.models import (
    CurrentConditions,
    DailyForecast,
    DailyForecastSlot,
    DailyHistoricalRecord,
    Forecast,
    HistoricalComparison,
    HourlyForecastSlot,
    ResolvedLocation,
    WeatherReport,
)
from bluesky_weather_bot.weather.resolver import LocationResolver

logger = logging.getLogger(__name__)


class WeatherService:

    def __init__(self, db: Database, skip_historical: bool = False) -> None:
        self._db = db
        self._skip_historical = skip_historical
        self._resolver = LocationResolver()
        self._client = WeatherClient()

    def lookup_by_coords(self, lat: float, lon: float) -> WeatherReport:
        """
        Fetch weather for an exact lat/lon pair.

        Reverse-geocodes to get a display name, then follows the same
        cache → fetch → save path as lookup().  Returns a single WeatherReport
        (coordinates are unambiguous — there's only ever one result).
        """
        loc = self._resolver.resolve_latlon(lat, lon)
        cached = self._db.get_cached_report(loc.lat, loc.lon)
        if cached is not None:
            logger.debug("Cache hit: (%s, %s)", lat, lon)
            report = _dict_to_report(cached)
            report.location.display_name = loc.display_name
            return report

        logger.debug("Cache miss: (%s, %s) — fetching from Open-Meteo", lat, lon)
        report = self._client.fetch(
            lat=lat,
            lon=lon,
            timezone=loc.timezone,
            skip_historical=self._skip_historical,
        )
        report.location = _loc_without_candidates(loc)
        self._db.save_cached_report(
            lat=lat,
            lon=lon,
            display_name=loc.display_name,
            report_json=_report_to_dict(report),
        )
        return report

    def lookup(self, raw_location: str, include_day_history: bool = False) -> list[WeatherReport]:
        """
        Resolve raw_location to one or more coordinates, fetch (or cache-hit)
        a WeatherReport for each, and return the list.

        include_day_history: whether to attach report.this_day_history (a
        ~75-year archive query — by far the slowest part of a lookup, and
        only needed when the caller actually plans to render/send it).

        Raises ValueError if the location cannot be resolved.
        """
        locations = self._resolver.resolve(raw_location)
        results: list[WeatherReport] = []

        for loc in locations:
            cached = self._db.get_cached_report(loc.lat, loc.lon)
            if cached is not None:
                logger.debug("Cache hit: %s", loc.display_name)
                report = _dict_to_report(cached)
                # Refresh display_name and zip_code from resolver in case cache was seeded by coords only
                report.location.display_name = loc.display_name
                report.location.zip_code = loc.zip_code
            else:
                logger.debug("Cache miss: %s — fetching from Open-Meteo", loc.display_name)
                report = self._client.fetch(
                    lat=loc.lat,
                    lon=loc.lon,
                    timezone=loc.timezone,
                    skip_historical=self._skip_historical,
                )
                report.location = _loc_without_candidates(loc)
                self._db.save_cached_report(
                    lat=loc.lat,
                    lon=loc.lon,
                    display_name=loc.display_name,
                    report_json=_report_to_dict(report),
                )

            if include_day_history:
                self._attach_this_day_history(report, loc)
            results.append(report)

        return results

    def _attach_this_day_history(self, report: WeatherReport, loc: ResolvedLocation) -> None:
        """
        Attach 'this day in history' records to report.this_day_history.
        Served from cache when available (fast); otherwise fetched synchronously
        on first request for this location (~2-3s one-time cost, then cached
        for the rest of the year).
        """
        if self._skip_historical:
            return

        today      = date.today()
        month, day = today.month, today.day

        cached = self._db.get_this_day_history(loc.lat, loc.lon, month, day)
        if cached is not None:
            report.this_day_history = _dicts_to_history(cached)
            return

        # Cache miss — fetch synchronously (once per location per year)
        try:
            records = self._client.fetch_this_day_history(
                loc.lat, loc.lon, loc.timezone, month, day
            )
            serialized = [_history_record_to_dict(r) for r in records]
            self._db.save_this_day_history(loc.lat, loc.lon, month, day, serialized)
            report.this_day_history = records
            logger.info(
                "This-day history fetched for %s (%d years)",
                loc.display_name, len(records),
            )
        except Exception:
            logger.exception("This-day history fetch failed for %s", loc.display_name)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _loc_without_candidates(loc: ResolvedLocation) -> ResolvedLocation:
    """Return a copy of loc with candidates=[] to break circular references."""
    return ResolvedLocation(
        lat=loc.lat,
        lon=loc.lon,
        display_name=loc.display_name,
        timezone=loc.timezone,
        zip_code=loc.zip_code,
        input_was_ambiguous=loc.input_was_ambiguous,
        candidates=[],
    )


def _report_to_dict(report: WeatherReport) -> dict:
    """
    Serialize WeatherReport to a JSON-safe dict.

    Handles:
    - circular reference in ResolvedLocation.candidates (dropped to [])
    - datetime objects → isoformat strings
    """
    loc = report.location
    current_d        = dataclasses.asdict(report.current)
    forecast_d       = dataclasses.asdict(report.forecast)
    historical_d     = dataclasses.asdict(report.historical)
    daily_forecast_d = dataclasses.asdict(report.daily_forecast)

    _convert_datetimes(current_d)
    _convert_datetimes(forecast_d)
    _convert_datetimes(historical_d)
    _convert_datetimes(daily_forecast_d)

    return {
        "location": {
            "lat": loc.lat,
            "lon": loc.lon,
            "display_name": loc.display_name,
            "timezone": loc.timezone,
            "zip_code": loc.zip_code,
            "input_was_ambiguous": loc.input_was_ambiguous,
            "candidates": [],
        },
        "current": current_d,
        "forecast": forecast_d,
        "historical": historical_d,
        "daily_forecast": daily_forecast_d,
        "generated_at": report.generated_at.isoformat(),
    }


def _convert_datetimes(obj) -> None:
    """Recursively replace datetime values with isoformat strings (in-place)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, datetime):
                obj[k] = v.isoformat()
            else:
                _convert_datetimes(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, datetime):
                obj[i] = v.isoformat()
            else:
                _convert_datetimes(v)


def _dict_to_report(d: dict) -> WeatherReport:
    """Reconstruct a WeatherReport from its serialized dict form."""
    loc_d = d["location"]
    loc = ResolvedLocation(
        lat=loc_d["lat"],
        lon=loc_d["lon"],
        display_name=loc_d["display_name"],
        timezone=loc_d["timezone"],
        zip_code=loc_d.get("zip_code"),
        input_was_ambiguous=loc_d.get("input_was_ambiguous", False),
        candidates=[],
    )

    c = d["current"]
    current = CurrentConditions(
        timestamp=datetime.fromisoformat(c["timestamp"]),
        temperature_f=c["temperature_f"],
        temperature_c=c["temperature_c"],
        feels_like_f=c["feels_like_f"],
        feels_like_c=c["feels_like_c"],
        humidity_pct=c["humidity_pct"],
        cloud_cover_pct=c["cloud_cover_pct"],
        wind_speed_mph=c["wind_speed_mph"],
        wind_speed_kph=c["wind_speed_kph"],
        wind_direction_deg=c["wind_direction_deg"],
        wind_gusts_mph=c["wind_gusts_mph"],
        wind_gusts_kph=c["wind_gusts_kph"],
        precipitation_in=c["precipitation_in"],
        precipitation_mm=c["precipitation_mm"],
        visibility_miles=c["visibility_miles"],
        visibility_km=c["visibility_km"],
        surface_pressure_hpa=c["surface_pressure_hpa"],
        weather_description=c["weather_description"],
    )

    slots = [
        HourlyForecastSlot(
            hour=datetime.fromisoformat(s["hour"]),
            temperature_f=s["temperature_f"],
            temperature_c=s["temperature_c"],
            precipitation_probability_pct=s["precipitation_probability_pct"],
            precipitation_in=s["precipitation_in"],
            precipitation_mm=s["precipitation_mm"],
            wind_speed_mph=s["wind_speed_mph"],
            wind_speed_kph=s["wind_speed_kph"],
            cloud_cover_pct=s["cloud_cover_pct"],
            weather_description=s["weather_description"],
        )
        for s in d["forecast"]["slots"]
    ]
    forecast = Forecast(slots=slots)

    hist_d = d["historical"]
    historical = HistoricalComparison(
        year_ago=_parse_daily_record(hist_d.get("year_ago")),
        ten_year_avg=_parse_daily_record(hist_d.get("ten_year_avg")),
    )

    df_slots = [
        DailyForecastSlot(
            date=datetime.fromisoformat(s["date"]),
            temp_max_f=s["temp_max_f"],
            temp_max_c=s["temp_max_c"],
            temp_min_f=s["temp_min_f"],
            temp_min_c=s["temp_min_c"],
            precipitation_probability_max_pct=s["precipitation_probability_max_pct"],
            precipitation_in=s["precipitation_in"],
            precipitation_mm=s["precipitation_mm"],
            wind_speed_max_mph=s["wind_speed_max_mph"],
            wind_speed_max_kph=s["wind_speed_max_kph"],
            weather_description=s["weather_description"],
            sunrise=datetime.fromisoformat(s["sunrise"]) if s.get("sunrise") else None,
            sunset=datetime.fromisoformat(s["sunset"]) if s.get("sunset") else None,
        )
        for s in d.get("daily_forecast", {}).get("slots", [])
    ]
    daily_forecast = DailyForecast(slots=df_slots)

    return WeatherReport(
        location=loc,
        current=current,
        forecast=forecast,
        historical=historical,
        daily_forecast=daily_forecast,
        generated_at=datetime.fromisoformat(d["generated_at"]),
    )


def _parse_daily_record(r: Optional[dict]) -> Optional[DailyHistoricalRecord]:
    """Inverse of _history_record_to_dict — deserializes one cached
    "this day" history record read back from the DB."""
    if r is None:
        return None
    return DailyHistoricalRecord(
        date=datetime.fromisoformat(r["date"]),
        temp_max_f=r["temp_max_f"],
        temp_max_c=r["temp_max_c"],
        temp_min_f=r["temp_min_f"],
        temp_min_c=r["temp_min_c"],
        temp_mean_f=r["temp_mean_f"],
        temp_mean_c=r["temp_mean_c"],
        precipitation_in=r["precipitation_in"],
        precipitation_mm=r["precipitation_mm"],
        wind_speed_max_mph=r["wind_speed_max_mph"],
        wind_speed_max_kph=r["wind_speed_max_kph"],
    )


def _history_record_to_dict(r: DailyHistoricalRecord) -> dict:
    """Serializes one DailyHistoricalRecord for the this_day_history_cache
    table's JSON blob column."""
    return {
        "date":               r.date.isoformat(),
        "temp_max_f":         r.temp_max_f,
        "temp_max_c":         r.temp_max_c,
        "temp_min_f":         r.temp_min_f,
        "temp_min_c":         r.temp_min_c,
        "temp_mean_f":        r.temp_mean_f,
        "temp_mean_c":        r.temp_mean_c,
        "precipitation_in":   r.precipitation_in,
        "precipitation_mm":   r.precipitation_mm,
        "wind_speed_max_mph": r.wind_speed_max_mph,
        "wind_speed_max_kph": r.wind_speed_max_kph,
    }


def _dicts_to_history(records: list[dict]) -> list[DailyHistoricalRecord]:
    """Batch form of _parse_daily_record, for a full cached "this day" history list."""
    return [_parse_daily_record(r) for r in records if r is not None]  # type: ignore[misc]
