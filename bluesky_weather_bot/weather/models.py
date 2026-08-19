"""
Weather data models.

All dataclasses are plain Python — no ORM coupling — so they serialize
freely to/from SQLite cache, JSON, or across module boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ResolvedLocation:
    """Result of resolving a raw location string to coordinates + timezone."""
    lat: float
    lon: float
    display_name: str           # e.g. "Denver, CO"
    timezone: str               # IANA tz string, e.g. "America/Denver"
    zip_code: Optional[str] = None          # 5-digit US ZIP if known
    input_was_ambiguous: bool = False
    candidates: list["ResolvedLocation"] = field(default_factory=list)


@dataclass
class CurrentConditions:
    """Right-now conditions for one location, dual-unit throughout (°F/°C, mph/kph, in/mm)."""
    timestamp: datetime
    temperature_f: float
    temperature_c: float
    feels_like_f: float
    feels_like_c: float
    humidity_pct: float
    cloud_cover_pct: float
    wind_speed_mph: float
    wind_speed_kph: float
    wind_direction_deg: float
    wind_gusts_mph: float
    wind_gusts_kph: float
    precipitation_in: float
    precipitation_mm: float
    visibility_miles: float
    visibility_km: float
    surface_pressure_hpa: float
    weather_description: str


@dataclass
class HourlyForecastSlot:
    """One hour of forecast data — see Forecast.next_n_hours() for the usual entry point."""
    hour: datetime
    temperature_f: float
    temperature_c: float
    precipitation_probability_pct: float
    precipitation_in: float
    precipitation_mm: float
    wind_speed_mph: float
    wind_speed_kph: float
    cloud_cover_pct: float
    weather_description: str
    humidity_pct: float = 0.0


@dataclass
class Forecast:
    """Full hourly forecast (as many slots as the API returned)."""
    slots: list[HourlyForecastSlot] = field(default_factory=list)

    def next_n_hours(self, n: int = 6) -> list[HourlyForecastSlot]:
        """Truncates to the first n slots — the formatter's default view
        shows only the next few hours, not the whole forecast."""
        return self.slots[:n]


@dataclass
class DailyForecastSlot:
    """One calendar day of forecast data."""
    date: datetime
    temp_max_f: float
    temp_max_c: float
    temp_min_f: float
    temp_min_c: float
    precipitation_probability_max_pct: float
    precipitation_in: float
    precipitation_mm: float
    wind_speed_max_mph: float
    wind_speed_max_kph: float
    weather_description: str
    sunrise: Optional[datetime] = None
    sunset: Optional[datetime] = None


@dataclass
class DailyForecast:
    """7-day daily forecast."""
    slots: list[DailyForecastSlot] = field(default_factory=list)


@dataclass
class DailyHistoricalRecord:
    """One day's actuals from the ERA5 archive — used for both the
    year-ago comparison and the 10-year average (see HistoricalComparison)."""
    date: datetime
    temp_max_f: float
    temp_max_c: float
    temp_min_f: float
    temp_min_c: float
    temp_mean_f: float
    temp_mean_c: float
    precipitation_in: float
    precipitation_mm: float
    wind_speed_max_mph: float
    wind_speed_max_kph: float


@dataclass
class HistoricalComparison:
    """Year-ago actuals + 10-year climatological average for today's date."""
    year_ago: Optional[DailyHistoricalRecord] = None
    ten_year_avg: Optional[DailyHistoricalRecord] = None


@dataclass
class WeatherReport:
    """Complete weather report for one location. Ready for formatting."""
    location: ResolvedLocation
    current: CurrentConditions
    forecast: Forecast
    historical: HistoricalComparison
    daily_forecast: DailyForecast = field(default_factory=DailyForecast)
    this_day_history: list[DailyHistoricalRecord] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# WMO Weather Code → human-readable description
# Reference: https://open-meteo.com/en/docs#weathervariables
# ---------------------------------------------------------------------------

WMO_DESCRIPTIONS: dict[int, str] = {
    0:  "Clear sky",
    1:  "Mainly clear",
    2:  "Partly cloudy",
    3:  "Overcast",
    45: "Foggy",
    48: "Icy fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm w/ hail",
    99: "Thunderstorm w/ heavy hail",
}


def wmo_description(code: int) -> str:
    """Maps an Open-Meteo WMO weather code to a human-readable string,
    falling back to a labeled placeholder for any code not in the table."""
    return WMO_DESCRIPTIONS.get(code, f"Unknown ({code})")
