"""
WeatherFormatter: converts a WeatherReport into a list of ≤300-char Bluesky post strings.

Produces a 2- or 3-post thread:
  Post 1 — Current conditions
  Post 2 — 6-hour hourly forecast
  Post 3 — Historical comparison (omitted when unavailable)

All posts are guaranteed to be ≤ MAX_POST_LEN characters. If the forecast
post would exceed the limit, lines are trimmed from the bottom.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from bluesky_weather_bot.weather.models import WeatherReport, DailyHistoricalRecord

MAX_POST_LEN = 300

_CARDINAL = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_cardinal(deg: float) -> str:
    """Converts a wind direction in degrees to a 16-point compass label (N, NNE, NE, ...)."""
    return _CARDINAL[round(deg / 22.5) % 16]


def _hour_label(dt: datetime) -> str:
    """Format hour as '10AM', '12PM', '1PM', etc."""
    h = dt.hour
    if h == 0:
        return "12AM"
    if h < 12:
        return f"{h}AM"
    if h == 12:
        return "12PM"
    return f"{h - 12}PM"


def _weather_emoji(description: str, is_day: bool = True) -> str:
    """
    Maps a WMO weather description string to a matching emoji, by keyword
    substring match (not an exact/enum lookup, since descriptions like
    "Light rain" vs "Rain" vs "Heavy rain" should share one emoji).

    is_day swaps the clear/mostly-clear cases to night variants (crescent
    moon, moon+stars) -- conditions that aren't sky-visibility-based
    (rain, snow, thunder, fog) look the same regardless of time of day,
    so only those two cases branch on it.
    """
    d = description.lower()
    if "clear sky" in d:
        return "\u2600\ufe0f" if is_day else "\U0001f319\u2728"      # sun : moon+sparkle
    if "mainly clear" in d:
        return "\U0001f324" if is_day else "\U0001f319"                # sun-behind-cloud : moon
    if "partly" in d:
        return "\u26c5" if is_day else "\u2601\ufe0f\U0001f319"      # sun-cloud : cloud+moon
    if "overcast" in d:
        return "\u2601\ufe0f"   # cloud
    if "fog" in d:
        return "\U0001f32b"     # fog
    if "drizzle" in d:
        return "\U0001f326"     # sun-behind-rain-cloud
    if "freezing" in d:
        return "\U0001f328"     # cloud-with-snow
    if "snow grains" in d or "snow showers" in d:
        return "\U0001f328"     # cloud-with-snow
    if "snow" in d:
        return "\u2744\ufe0f"   # snowflake
    if "rain" in d or "shower" in d:
        return "\U0001f327"     # cloud-with-rain
    if "thunder" in d:
        return "\u26c8\ufe0f"   # thunder-cloud
    return "\U0001f324" if is_day else "\U0001f319"   # default: sun-behind-cloud : moon


def _tz_abbr(timezone: str, ts: datetime) -> str:
    """Return timezone abbreviation (e.g. 'MST', 'MDT') for the given naive local datetime."""
    try:
        from zoneinfo import ZoneInfo
        aware = ts.replace(tzinfo=ZoneInfo(timezone))
        return aware.strftime("%Z")
    except Exception:
        # Fallback: last component of IANA name
        return timezone.split("/")[-1]


class WeatherFormatter:
    """Stateless formatter. Call format_thread() with a WeatherReport."""

    def format_thread(self, report: WeatherReport, units: str = "imperial") -> list[str]:
        """
        Format a WeatherReport into a list of Bluesky post strings.

        units: "imperial" (°F primary, °C secondary) or
               "metric"   (°C primary, °F secondary).
        Both values are always shown; units controls display order.
        """
        posts = [
            self._post1_current(report, units),
            self._post2_forecast(report, units),
        ]
        post3 = self._post3_historical(report, units)
        if post3:
            posts.append(post3)
        return posts

    # ------------------------------------------------------------------
    # Post 1 — Current conditions
    # ------------------------------------------------------------------

    def _post1_current(self, report: WeatherReport, units: str = "imperial") -> str:
        """Builds the first (always-sent) post: current conditions."""
        c   = report.current
        loc = report.location.display_name
        if report.location.zip_code:
            loc = f"{loc} ({report.location.zip_code})"

        ts    = c.timestamp
        day   = ts.day
        hour  = ts.hour % 12 or 12
        minute = ts.strftime("%M")
        ampm  = "AM" if ts.hour < 12 else "PM"
        wday  = ts.strftime("%a")
        month = ts.strftime("%b")
        tz    = _tz_abbr(report.location.timezone, ts)
        ts_str = f"{wday} {month} {day} {hour}:{minute} {ampm} {tz}"

        cardinal = _deg_to_cardinal(c.wind_direction_deg)
        emoji    = _weather_emoji(c.weather_description, c.is_day)

        if units == "metric":
            temp_str  = f"🌡 {c.temperature_c:.0f}°C ({c.temperature_f:.0f}°F)"
            feels_str = f"Feels {c.feels_like_c:.0f}°C ({c.feels_like_f:.0f}°F)"
            wind_str  = (f"💨 Wind: {c.wind_speed_kph:.0f}km/h ({c.wind_speed_mph:.0f}mph)"
                         f" {cardinal} | Gusts {c.wind_gusts_kph:.0f}km/h ({c.wind_gusts_mph:.0f}mph)")
            precip_str = f"🌧 Precip: {c.precipitation_mm:.1f}mm ({c.precipitation_in:.2f}in)"
        else:
            temp_str  = f"🌡 {c.temperature_f:.0f}°F ({c.temperature_c:.0f}°C)"
            feels_str = f"Feels {c.feels_like_f:.0f}°F ({c.feels_like_c:.0f}°C)"
            wind_str  = (f"💨 Wind: {c.wind_speed_mph:.0f}mph ({c.wind_speed_kph:.0f}km/h)"
                         f" {cardinal} | Gusts {c.wind_gusts_mph:.0f}mph ({c.wind_gusts_kph:.0f}km/h)")
            precip_str = f"🌧 Precip: {c.precipitation_in:.2f}in ({c.precipitation_mm:.1f}mm)"

        lines = [
            f"📍 {loc}",
            f"🕐 As of {ts_str}",
            f"{emoji} {c.weather_description}",
            f"{temp_str} | {feels_str}",
            f"💧 Humidity: {c.humidity_pct:.0f}%",
            wind_str,
            precip_str,
            f"📊 Pressure: {c.surface_pressure_hpa:.0f}hPa",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Post 2 — 6-hour forecast
    # ------------------------------------------------------------------

    def _post2_forecast(self, report: WeatherReport, units: str = "imperial") -> str:
        """Builds the second post: next-6-hours forecast thread reply."""
        loc   = report.location.display_name
        slots = report.forecast.next_n_hours(6)
        lines = [f"⏱ Next 6 Hours — {loc}"]

        for slot in slots:
            if units == "metric":
                temp_part = f"{slot.temperature_c:.0f}°C"
                wind_part = f"{slot.wind_speed_kph:.0f}km/h"
            else:
                temp_part = f"{slot.temperature_f:.0f}°F"
                wind_part = f"{slot.wind_speed_mph:.0f}mph"
            line = (
                f"{_hour_label(slot.hour)}: {temp_part},"
                f" ☁ {slot.cloud_cover_pct:.0f}%,"
                f" 💧 {slot.precipitation_probability_pct:.0f}%,"
                f" 💨 {wind_part}"
            )
            if len("\n".join(lines + [line])) > MAX_POST_LEN:
                break
            lines.append(line)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Post 3 — Historical comparison
    # ------------------------------------------------------------------

    def _post3_historical(self, report: WeatherReport, units: str = "imperial") -> Optional[str]:
        """Builds the third post: year-ago + 10-year-average comparison.
        Returns None (post omitted) if neither value is available, e.g.
        when SKIP_HISTORICAL is set."""
        h = report.historical
        if h.year_ago is None and h.ten_year_avg is None:
            return None

        loc   = report.location.display_name
        lines = [f"📅 Historical — {loc}"]

        def _hist_line(r: DailyHistoricalRecord) -> str:
            """Formats one DailyHistoricalRecord as a hi/lo/precip line."""
            if units == "metric":
                return (
                    f"  Hi {r.temp_max_c:.0f}°C ({r.temp_max_f:.0f}°F)"
                    f" / Lo {r.temp_min_c:.0f}°C ({r.temp_min_f:.0f}°F)"
                    f" | Precip {r.precipitation_mm:.1f}mm"
                )
            return (
                f"  Hi {r.temp_max_f:.0f}°F ({r.temp_max_c:.0f}°C)"
                f" / Lo {r.temp_min_f:.0f}°F ({r.temp_min_c:.0f}°C)"
                f" | Precip {r.precipitation_in:.2f}in"
            )

        if h.year_ago:
            r = h.year_ago
            date_str = f"{r.date.strftime('%b')} {r.date.day}, {r.date.year}"
            lines.append(f"Last year ({date_str}):")
            lines.append(_hist_line(r))

        if h.ten_year_avg:
            r = h.ten_year_avg
            lines.append(f"10-yr avg ({r.date.strftime('%b')} \u00b17d):")
            lines.append(_hist_line(r))

        post = "\n".join(lines)
        if len(post) <= MAX_POST_LEN:
            return post

        # Trim from bottom while over limit (keep header line at minimum)
        while len(lines) > 1:
            lines.pop()
            post = "\n".join(lines)
            if len(post) <= MAX_POST_LEN:
                return post

        return None
