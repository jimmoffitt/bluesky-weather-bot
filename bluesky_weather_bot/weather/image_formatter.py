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
from pathlib import Path
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
# Colour palette
# ---------------------------------------------------------------------------

BG       = "#0d1117"    # overall background
PANEL_L  = "#161b22"    # left panel
SEP      = "#21262d"    # separator lines / progress-bar track
TEXT_PRI = "#e6edf3"    # primary text
TEXT_MUT = "#8b949e"    # muted / secondary text
AMBER    = "#f7a14b"    # temperature, amber highlights
BLUE     = "#6ea8fe"    # blue accent, badge

# Aliases used by forecast chart and historical card
HEADER_BG = PANEL_L
ACCENT    = SEP
TEXT_AMB  = AMBER
TEXT_SKY  = BLUE
TEXT_SEC  = TEXT_MUT

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


_FONTS_DIR = Path(__file__).parent.parent / "fonts"


def _font_syne(size: int) -> ImageFont.ImageFont:
    """Syne Bold — display / values.  Falls back to DejaVu Bold."""
    path = _FONTS_DIR / "Syne-Bold.ttf"
    if path.is_file():
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:
            pass
    return _font(size, bold=True)


def _font_mono(size: int, medium: bool = False) -> ImageFont.ImageFont:
    """DM Mono — labels / units / secondary text.  Falls back to DejaVu."""
    fname = "DMMono-Medium.ttf" if medium else "DMMono-Regular.ttf"
    path = _FONTS_DIR / fname
    if path.is_file():
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:
            pass
    return _font(size)


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
        W, H    = 800, 480
        SPLIT_X = 272          # left / right panel boundary

        img, draw = _new_card(W, H)

        c        = report.current
        loc      = report.location.display_name
        ts       = c.timestamp
        tz       = _tz_abbr(report.location.timezone, ts)
        dow      = ts.strftime("%a").upper()
        mon      = ts.strftime("%b").upper()
        day      = ts.day
        hour12   = ts.hour % 12 or 12
        minute   = ts.strftime("%M")
        ampm     = "AM" if ts.hour < 12 else "PM"
        ts_str   = f"{dow} {mon} {day}  \u00b7  {hour12}:{minute} {ampm} {tz}"
        cardinal = _deg_to_cardinal(c.wind_direction_deg)

        # ── Left panel ───────────────────────────────────────────────────────
        draw.rectangle([(0, 0), (SPLIT_X - 1, H)], fill=_hex_to_rgb(PANEL_L))

        f_loc  = _font_syne(22)
        f_ts   = _font_mono(11)
        f_big  = _font_syne(66)
        f_degc = _font_mono(21)
        f_fl   = _font_mono(12)

        draw.text((18, 28), loc,    font=f_loc, fill=TEXT_PRI)
        draw.text((18, 60), ts_str, font=f_ts,  fill=TEXT_MUT)

        temp_str = f"{c.temperature_f:.0f}\u00b0F"
        tbbox    = draw.textbbox((0, 0), temp_str, font=f_big)
        tw       = tbbox[2] - tbbox[0]
        th       = tbbox[3] - tbbox[1]
        tx       = max(8, (SPLIT_X - tw) // 2)
        ty       = 118
        draw.text((tx, ty), temp_str, font=f_big, fill=AMBER)

        degc_str = f"{c.temperature_c:.0f}\u00b0C"
        _text_centered(draw, ty + th + 10, degc_str, f_degc, TEXT_MUT, SPLIT_X)

        fl_str = f"FEELS LIKE {c.feels_like_f:.0f}\u00b0F"
        _text_centered(draw, ty + th + 40, fl_str, f_fl, TEXT_MUT, SPLIT_X)

        # ── Panel separator ──────────────────────────────────────────────────
        draw.rectangle([(SPLIT_X, 0), (SPLIT_X + 1, H)], fill=_hex_to_rgb(SEP))

        # ── Condition badge (top-right of right panel) ───────────────────────
        f_badge    = _font_mono(11, medium=True)
        badge_text = c.weather_description.upper()[:24]
        bbbox      = draw.textbbox((0, 0), badge_text, font=f_badge)
        bw, bh     = bbbox[2] - bbbox[0], bbbox[3] - bbbox[1]
        bpx, bpy   = 12, 5
        bx2        = W - 18
        bx1        = bx2 - bw - 2 * bpx
        by1        = 18
        by2        = by1 + bh + 2 * bpy
        draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=10,
                                fill=(13, 28, 71), outline=_hex_to_rgb(BLUE), width=1)
        draw.text((bx1 + bpx, by1 + bpy), badge_text, font=f_badge, fill=_hex_to_rgb(BLUE))

        # ── Stat rows (right panel) ──────────────────────────────────────────
        ROW_START = 58
        ROW_H     = (H - ROW_START) // 7   # ≈ 60 px

        f_lbl = _font_mono(11)
        f_val = _font_mono(13, medium=True)

        DOT_X  = SPLIT_X + 22
        LBL_X  = SPLIT_X + 36
        BAR_X1 = SPLIT_X + 6
        BAR_X2 = W - 24
        VAL_X  = W - 18

        stats = [
            ("HUMIDITY",
             f"{c.humidity_pct:.0f}%",
             "bar_blue",  c.humidity_pct),
            ("CLOUD COVER",
             f"{c.cloud_cover_pct:.0f}%",
             "bar_amber", c.cloud_cover_pct),
            ("WIND",
             f"{c.wind_speed_mph:.0f} mph  \u00b7  {c.wind_speed_kph:.0f} km/h  {cardinal}",
             "text", 0.0),
            ("GUSTS",
             f"{c.wind_gusts_mph:.0f} mph  \u00b7  {c.wind_gusts_kph:.0f} km/h",
             "text", 0.0),
            ("PRECIP",
             f"{c.precipitation_in:.2f} in  \u00b7  {c.precipitation_mm:.1f} mm",
             "text", 0.0),
            ("PRESSURE",
             f"{c.surface_pressure_hpa:.0f} hPa",
             "text", 0.0),
            ("VISIBILITY",
             f"{c.visibility_miles:.1f} mi  \u00b7  {c.visibility_km:.1f} km",
             "text", 0.0),
        ]

        for i, (label, value, kind, pct) in enumerate(stats):
            ry  = ROW_START + i * ROW_H
            rym = ry + ROW_H // 2

            if i > 0:
                draw.line([(SPLIT_X + 2, ry), (W, ry)],
                          fill=_hex_to_rgb(SEP), width=1)

            if kind.startswith("bar_"):
                lbl_y    = ry + 12
                bar_y    = ry + ROW_H * 2 // 3
                dot_cy   = lbl_y + 6
                dot_fill = _hex_to_rgb(BLUE) if kind == "bar_blue" else _hex_to_rgb(AMBER)
                draw.ellipse([(DOT_X - 5, dot_cy - 5), (DOT_X + 5, dot_cy + 5)],
                             fill=dot_fill)
                draw.text((LBL_X, lbl_y), label, font=f_lbl, fill=TEXT_MUT)
                bbox = draw.textbbox((0, 0), value, font=f_val)
                vw   = bbox[2] - bbox[0]
                draw.text((VAL_X - vw, lbl_y), value, font=f_val, fill=TEXT_PRI)
                # Progress bar track
                draw.rounded_rectangle(
                    [(BAR_X1, bar_y - 2), (BAR_X2, bar_y + 2)],
                    radius=2, fill=_hex_to_rgb(SEP))
                # Progress bar fill
                fill_w   = int((BAR_X2 - BAR_X1) * min(pct, 100.0) / 100.0)
                bar_fill = _hex_to_rgb(BLUE) if kind == "bar_blue" else _hex_to_rgb(AMBER)
                if fill_w > 4:
                    draw.rounded_rectangle(
                        [(BAR_X1, bar_y - 2), (BAR_X1 + fill_w, bar_y + 2)],
                        radius=2, fill=bar_fill)
            else:
                draw.ellipse([(DOT_X - 5, rym - 5), (DOT_X + 5, rym + 5)],
                             fill=_hex_to_rgb(TEXT_MUT))
                draw.text((LBL_X, rym - 8), label, font=f_lbl, fill=TEXT_MUT)
                bbox = draw.textbbox((0, 0), value, font=f_val)
                vw   = bbox[2] - bbox[0]
                draw.text((VAL_X - vw, rym - 8), value, font=f_val, fill=TEXT_PRI)

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
        temps  = [s.temperature_f for s in slots]
        precip = [s.precipitation_probability_pct for s in slots]
        xs     = range(len(hours))

        fig, (ax_t, ax_p) = plt.subplots(
            2, 1,
            sharex=True,
            figsize=(10, 5),
            dpi=90,
            layout="constrained",
            gridspec_kw={"height_ratios": [2, 1], "hspace": 0.06},
        )
        fig.patch.set_facecolor(BG)

        # --- Top panel: temperature line ---
        ax_t.set_facecolor(HEADER_BG)
        ax_t.plot(xs, temps, color=TEXT_AMB, linewidth=2.5,
                  marker="o", markersize=7, zorder=3)
        for spine in ax_t.spines.values():
            spine.set_edgecolor(ACCENT)
        ax_t.tick_params(axis="y", colors=TEXT_AMB, labelsize=11)
        ax_t.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
        ax_t.set_ylabel("Temp (°F)", color=TEXT_AMB, fontsize=11)
        ax_t.yaxis.label.set_color(TEXT_AMB)
        ax_t.grid(axis="y", color=ACCENT, linestyle="--", linewidth=0.7, alpha=0.5)
        ax_t.set_title(f"6-Hour Forecast  —  {loc}",
                       color=TEXT_PRI, fontsize=13, pad=8)

        # Value labels above each point
        y_pad = (max(temps) - min(temps)) * 0.08 + 0.5 if len(temps) > 1 else 1
        for x, y in zip(xs, temps):
            ax_t.annotate(f"{y:.0f}", (x, y), textcoords="offset points",
                          xytext=(0, 9), ha="center", color=TEXT_AMB, fontsize=10,
                          fontweight="bold")

        # --- Bottom panel: precip probability bars ---
        ax_p.set_facecolor(HEADER_BG)
        ax_p.bar(xs, precip, color=TEXT_SKY, alpha=0.75, width=0.6, zorder=3)
        for spine in ax_p.spines.values():
            spine.set_edgecolor(ACCENT)
        ax_p.tick_params(axis="y", colors=TEXT_SKY, labelsize=10)
        ax_p.tick_params(axis="x", colors=TEXT_PRI, labelsize=11)
        ax_p.set_ylabel("Precip %", color=TEXT_SKY, fontsize=11)
        ax_p.set_ylim(0, 100)
        ax_p.set_xticks(list(xs))
        ax_p.set_xticklabels(hours)
        ax_p.grid(axis="y", color=ACCENT, linestyle="--", linewidth=0.7, alpha=0.5)

        # Value labels inside each bar (skip zero bars)
        for x, p in zip(xs, precip):
            if p >= 5:
                ax_p.text(x, p + 2, f"{p:.0f}%", ha="center", va="bottom",
                          color=TEXT_SKY, fontsize=9, fontweight="bold")

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
