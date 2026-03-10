"""
WeatherImageFormatter

Converts a WeatherReport into up to 3 PNG images suitable for a single
Bluesky post with embedded image attachments.

Returns:
    (images: list[bytes], alts: list[str], caption: str)

Images produced:
  1. Current conditions card  — always present (800 × 500 px)
  2. Forecast chart           — always present (matplotlib dual-axis)
  3. Historical comparison card — only when historical data is available (800 × 400 px)

Requirements:
    pip install Pillow matplotlib

Designed for headless operation (Raspberry Pi, CI).  Uses the Agg matplotlib
backend and DejaVu fonts (available on most Linux systems and macOS).

No emoji — plain ASCII labels only (emoji fonts not reliably available on Pi).
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from typing import Optional

# Matplotlib MUST be configured before pyplot is imported
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from PIL import Image, ImageDraw, ImageFont

from bluesky_weather_bot.weather.models import (
    WeatherReport,
    HistoricalComparison,
    DailyHistoricalRecord,
)

# ---------------------------------------------------------------------------
# Colour palette (dark theme)
# ---------------------------------------------------------------------------

BG          = "#1a1a2e"    # image background
HEADER_BG   = "#16213e"    # header / axis background
ACCENT      = "#0f3460"    # accent line / grid
TEXT_PRI    = "#e0e0e0"    # primary text
TEXT_AMB    = "#f5a623"    # amber — temperature values
TEXT_SKY    = "#7ec8e3"    # sky-blue — secondary values / precip
TEXT_SEC    = "#8899aa"    # secondary / footer text

# ---------------------------------------------------------------------------
# Cardinal directions (shared with formatter.py)
# ---------------------------------------------------------------------------

_CARDINAL = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_cardinal(deg: float) -> str:
    return _CARDINAL[round(deg / 22.5) % 16]


def _hour_label(dt: datetime) -> str:
    h = dt.hour
    if h == 0:
        return "12AM"
    if h < 12:
        return f"{h}AM"
    if h == 12:
        return "12PM"
    return f"{h - 12}PM"


# ---------------------------------------------------------------------------
# Font loader
# ---------------------------------------------------------------------------

_FONT_SEARCH_DIRS = [
    "/usr/share/fonts/truetype/dejavu",          # Debian/Ubuntu/Pi
    "/usr/share/fonts/dejavu",                   # some Fedora layouts
    "/usr/share/fonts/TTF",                      # Arch
    "/Library/Fonts",                            # macOS
    "/System/Library/Fonts",                     # macOS system
]


def _find_font(name: str) -> Optional[str]:
    for d in _FONT_SEARCH_DIRS:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    fname = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = _find_font(fname)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _text_centered(draw: ImageDraw.ImageDraw, y: int, text: str,
                   font: ImageFont.ImageFont, fill: str, width: int) -> int:
    """Draw text horizontally centred in [0, width]. Returns text height."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (width - tw) // 2
    draw.text((x, y), text, font=font, fill=fill)
    return bbox[3] - bbox[1]


def _draw_hline(draw: ImageDraw.ImageDraw, y: int, width: int, fill: str,
                margin: int = 40) -> None:
    draw.line([(margin, y), (width - margin, y)], fill=fill, width=2)


def _new_card(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (w, h), _hex_to_rgb(BG))
    draw = ImageDraw.Draw(img)
    return img, draw


def _header_bar(draw: ImageDraw.ImageDraw, width: int, h: int,
                line1: str, line2: str) -> int:
    """Draw a header bar. Returns the y position after the bar."""
    draw.rectangle([(0, 0), (width, h)], fill=_hex_to_rgb(HEADER_BG))
    f_big   = _font(24, bold=True)
    f_small = _font(16)
    _text_centered(draw, 14, line1, f_big,   TEXT_PRI, width)
    _text_centered(draw, 46, line2, f_small, TEXT_SEC, width)
    # Accent line
    draw.rectangle([(0, h), (width, h + 3)], fill=_hex_to_rgb(ACCENT))
    return h + 4


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Timezone abbreviation (same as formatter.py)
# ---------------------------------------------------------------------------

def _tz_abbr(timezone: str, ts: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        aware = ts.replace(tzinfo=ZoneInfo(timezone))
        return aware.strftime("%Z")
    except Exception:
        return timezone.split("/")[-1]


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class WeatherImageFormatter:
    """
    Generates up to 3 PNG images from a WeatherReport.

    Usage:
        formatter = WeatherImageFormatter()
        images, alts, caption = formatter.format_images(report)
        # images: list of raw PNG bytes; alts: matching alt strings; caption: ≤300 chars
    """

    def format_images(
        self, report: WeatherReport
    ) -> tuple[list[bytes], list[str], str]:
        images: list[bytes] = []
        alts:   list[str]   = []

        card1 = self._render_current_card(report)
        images.append(card1)
        alts.append(self._alt_current(report))

        chart = self._render_forecast_chart(report)
        images.append(chart)
        alts.append(self._alt_forecast(report))

        card3 = self._render_historical_card(report)
        if card3 is not None:
            images.append(card3)
            alts.append(self._alt_historical(report))

        caption = self._caption(report)
        return images, alts, caption

    # ------------------------------------------------------------------
    # Card 1 — Current conditions  (800 × 500 px)
    # ------------------------------------------------------------------

    def _render_current_card(self, report: WeatherReport) -> bytes:
        W, H = 800, 380
        img, draw = _new_card(W, H)

        c   = report.current
        loc = report.location.display_name
        ts  = c.timestamp
        tz  = _tz_abbr(report.location.timezone, ts)
        day    = ts.day
        hour12 = ts.hour % 12 or 12
        ampm   = "AM" if ts.hour < 12 else "PM"
        ts_str = ts.strftime(f"%a %b {day}  {hour12}:{ts.strftime('%M')} {ampm} {tz}")
        cardinal = _deg_to_cardinal(c.wind_direction_deg)

        # Compact header bar
        HEADER_H = 56
        draw.rectangle([(0, 0), (W, HEADER_H)], fill=_hex_to_rgb(HEADER_BG))
        _text_centered(draw, 9, f"{loc}  |  {c.weather_description}",
                       _font(22, bold=True), TEXT_PRI, W)
        _text_centered(draw, 34, ts_str, _font(14), TEXT_SEC, W)
        draw.rectangle([(0, HEADER_H), (W, HEADER_H + 2)], fill=_hex_to_rgb(ACCENT))

        y   = HEADER_H + 6   # 64
        PAD = 40

        # Big temperature — no divider lines below
        draw.text((PAD, y),
                  f"{c.temperature_f:.0f}F / {c.temperature_c:.0f}C",
                  font=_font(50, bold=True), fill=TEXT_AMB)
        draw.text((PAD, y + 58),
                  f"Feels like {c.feels_like_f:.0f}F ({c.feels_like_c:.0f}C)",
                  font=_font(17), fill=TEXT_PRI)

        y += 88   # 152

        # 2-column metric grid (no dividers)
        MID   = W // 2
        COLS  = [PAD, MID + 10]
        ROW_H = 44
        f_lbl = _font(13)
        f_val = _font(20, bold=True)

        def _stat(col: int, y0: int, label: str, value: str) -> None:
            draw.text((COLS[col], y0),      label, font=f_lbl, fill=TEXT_SKY)
            draw.text((COLS[col], y0 + 16), value, font=f_val, fill=TEXT_PRI)

        _stat(0, y, "HUMIDITY",    f"{c.humidity_pct:.0f}%")
        _stat(1, y, "CLOUD COVER", f"{c.cloud_cover_pct:.0f}%")
        y += ROW_H

        _stat(0, y, "WIND",
              f"{c.wind_speed_mph:.0f}mph ({c.wind_speed_kph:.0f}km/h) {cardinal}")
        _stat(1, y, "GUSTS",
              f"{c.wind_gusts_mph:.0f}mph ({c.wind_gusts_kph:.0f}km/h)")
        y += ROW_H

        _stat(0, y, "PRECIP",   f"{c.precipitation_in:.2f}in ({c.precipitation_mm:.1f}mm)")
        _stat(1, y, "PRESSURE", f"{c.surface_pressure_hpa:.0f} hPa")
        y += ROW_H

        _stat(0, y, "VISIBILITY", f"{c.visibility_miles:.1f}mi ({c.visibility_km:.1f}km)")

        # Footer
        f_foot = _font(12)
        footer = "ZipWx Bot  |  Data: Open-Meteo"
        bbox   = draw.textbbox((0, 0), footer, font=f_foot)
        fw     = bbox[2] - bbox[0]
        draw.text((W - fw - 20, H - 20), footer, font=f_foot, fill=TEXT_SEC)

        return _to_png(img)

    # ------------------------------------------------------------------
    # Card 3 — Historical comparison  (800 × 400 px)
    # ------------------------------------------------------------------

    def _render_historical_card(self, report: WeatherReport) -> Optional[bytes]:
        h = report.historical
        if h.year_ago is None and h.ten_year_avg is None:
            return None

        W, H = 800, 400
        img, draw = _new_card(W, H)
        loc = report.location.display_name

        y = _header_bar(draw, W, 70, f"Historical Comparison  —  {loc}", "")
        y += 20
        pad = 60
        f_label = _font(13)
        f_val   = _font(20, bold=True)
        f_sub   = _font(15)

        def _draw_block(rec: DailyHistoricalRecord, header: str, y0: int) -> int:
            draw.text((pad, y0), header, font=_font(15, bold=True), fill=TEXT_SKY)
            y0 += 24
            draw.text((pad, y0),
                      f"Hi {rec.temp_max_f:.0f}F ({rec.temp_max_c:.0f}C)"
                      f"  /  Lo {rec.temp_min_f:.0f}F ({rec.temp_min_c:.0f}C)",
                      font=f_val, fill=TEXT_AMB)
            y0 += 28
            draw.text((pad, y0),
                      f"Precip {rec.precipitation_in:.2f}in  |  "
                      f"Max wind {rec.wind_speed_max_mph:.0f}mph",
                      font=f_sub, fill=TEXT_PRI)
            return y0 + 40

        if h.year_ago:
            d = h.year_ago.date
            date_str = f"{d.strftime('%b')} {d.day}, {d.year}"
            y = _draw_block(h.year_ago, f"Last year  ({date_str})", y)
            _draw_hline(draw, y, W, ACCENT)
            y += 16

        if h.ten_year_avg:
            d = h.ten_year_avg.date
            date_str = f"{d.strftime('%b')} {d.day}"
            y = _draw_block(h.ten_year_avg, f"10-year avg  ({date_str} +/-7 days)", y)

        # Footer
        f_foot = _font(12)
        footer = "ZipWx Bot  |  Data: Open-Meteo"
        bbox   = draw.textbbox((0, 0), footer, font=f_foot)
        fw     = bbox[2] - bbox[0]
        draw.text((W - fw - 20, H - 22), footer, font=f_foot, fill=TEXT_SEC)

        return _to_png(img)

    # ------------------------------------------------------------------
    # Chart — Forecast (matplotlib dual-axis)
    # ------------------------------------------------------------------

    def _render_forecast_chart(self, report: WeatherReport) -> bytes:
        slots = report.forecast.next_n_hours(6)
        loc   = report.location.display_name

        hours  = [_hour_label(s.hour) for s in slots]
        temps  = [s.temperature_f      for s in slots]
        precip = [s.precipitation_probability_pct for s in slots]

        fig, ax1 = plt.subplots(figsize=(10, 4), dpi=90)
        fig.patch.set_facecolor(BG)
        ax1.set_facecolor(HEADER_BG)

        # Left axis — temperature
        line1, = ax1.plot(hours, temps, color=TEXT_AMB,
                          linewidth=2, marker="o", markersize=6)
        ax1.set_ylabel("Temperature (°F)", color=TEXT_AMB, fontsize=11)
        ax1.tick_params(axis="y", colors=TEXT_AMB)
        ax1.tick_params(axis="x", colors=TEXT_PRI)
        for spine in ax1.spines.values():
            spine.set_edgecolor(ACCENT)

        # Value labels above each temp point
        for x, y in zip(hours, temps):
            ax1.annotate(f"{y:.0f}", (x, y), textcoords="offset points",
                         xytext=(0, 8), ha="center", color=TEXT_AMB, fontsize=9)

        # Right axis — precip probability
        ax2 = ax1.twinx()
        ax2.set_facecolor(HEADER_BG)
        line2, = ax2.plot(hours, precip, color=TEXT_SKY, linewidth=2,
                          linestyle="--", marker="s", markersize=5)
        ax2.set_ylabel("Precip Probability (%)", color=TEXT_SKY, fontsize=11)
        ax2.tick_params(axis="y", colors=TEXT_SKY)
        ax2.set_ylim(0, 100)
        for spine in ax2.spines.values():
            spine.set_edgecolor(ACCENT)

        # Grid on primary axis
        ax1.grid(color=ACCENT, linestyle="--", linewidth=0.7, alpha=0.5)

        # Title and tick styling
        ax1.set_title(f"6-Hour Forecast  —  {loc}", color=TEXT_PRI, fontsize=13, pad=10)
        ax1.xaxis.label.set_color(TEXT_PRI)
        fig.tight_layout(pad=1.5)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ------------------------------------------------------------------
    # Caption and alt texts
    # ------------------------------------------------------------------

    def _caption(self, report: WeatherReport) -> str:
        c   = report.current
        loc = report.location.display_name
        cardinal = _deg_to_cardinal(c.wind_direction_deg)
        text = (
            f"{loc}: {c.weather_description}, "
            f"{c.temperature_f:.0f}F ({c.temperature_c:.0f}C), "
            f"humidity {c.humidity_pct:.0f}%, "
            f"wind {c.wind_speed_mph:.0f}mph {cardinal}. #WxBot"
        )
        return text[:300]

    def _alt_current(self, report: WeatherReport) -> str:
        c = report.current
        return (
            f"Current conditions for {report.location.display_name}: "
            f"{c.temperature_f:.0f}F, {c.weather_description}"
        )

    def _alt_forecast(self, report: WeatherReport) -> str:
        return f"6-hour temperature and precipitation forecast for {report.location.display_name}"

    def _alt_historical(self, report: WeatherReport) -> str:
        return f"Historical weather comparison for {report.location.display_name}"
