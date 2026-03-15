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

BG       = "#141b26"   # --card  (card / right panel bg)
PANEL_L  = "#162032"   # --card2 (left temp panel)
HDR_BG   = "#111827"   # header strip
SEP      = "#1c2534"   # --divider (row separator)
BORDER   = "#202c3e"   # --border  (panel edge lines)
TEXT_PRI = "#e2eaf7"   # --text
TEXT_MUT = "#6b7fa3"   # --muted
AMBER    = "#f7a14b"   # --accent2 (temperature)
BLUE     = "#6ea8fe"   # --accent  (badge, bars)

# Aliases used by forecast chart and historical card
HEADER_BG = HDR_BG
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


def _draw_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
               icon_type: str, color: tuple) -> None:
    """Draw a small icon centered at (cx, cy) fitting within radius r."""
    if icon_type == "drop":
        # Teardrop: triangle pointing up + ellipse for the round bottom
        draw.polygon([(cx, cy - r), (cx - r, cy), (cx + r, cy)], fill=color)
        draw.ellipse([(cx - r, cy - r // 3), (cx + r, cy + r)], fill=color)

    elif icon_type == "cloud":
        # Three bumps on top + rectangle for flat base
        bump_r = max(3, int(r * 0.55))
        for ox, oy in ((-int(r * 0.5), 0), (0, -int(r * 0.35)), (int(r * 0.5), 0)):
            draw.ellipse([(cx + ox - bump_r, cy + oy - bump_r),
                          (cx + ox + bump_r, cy + oy + bump_r)], fill=color)
        draw.rectangle([(cx - r, cy), (cx + r, cy + int(r * 0.6))], fill=color)

    elif icon_type == "wind":
        # Three horizontal lines of decreasing length
        for j in range(3):
            y_l = cy - r + j * (r * 2 // 3 + 1)
            x1  = cx + r - j * (r // 2)
            draw.line([(cx - r, y_l), (x1, y_l)], fill=color, width=2)

    elif icon_type == "arrow_up":
        # Upward arrow: triangle head + rectangular stem
        stem = max(1, r // 3)
        draw.polygon([(cx, cy - r), (cx - r, cy + r // 4), (cx + r, cy + r // 4)],
                     fill=color)
        draw.rectangle([(cx - stem, cy + r // 4), (cx + stem, cy + r)], fill=color)

    elif icon_type == "rain":
        # Three angled rain lines
        spacing = r * 2 // 3
        for j in range(3):
            x0 = cx - r + j * spacing
            draw.line([(x0, cy - r // 2), (x0 - r // 3, cy + r // 2)],
                      fill=color, width=2)

    elif icon_type == "gauge":
        # Pressure gauge: semicircle arc + needle + pivot dot
        draw.arc([(cx - r, cy - r // 2), (cx + r, cy + r + r // 2)],
                 start=180, end=0, fill=color, width=2)
        draw.line([(cx, cy + r // 4), (cx + r * 2 // 3, cy - r // 3)],
                  fill=color, width=2)
        draw.ellipse([(cx - 2, cy + r // 4 - 2), (cx + 2, cy + r // 4 + 2)],
                     fill=color)

    elif icon_type == "eye":
        # Eye outline (ellipse) + filled pupil
        draw.ellipse([(cx - r, cy - r // 2), (cx + r, cy + r // 2)],
                     outline=color, width=2)
        pr = max(2, r // 3)
        draw.ellipse([(cx - pr, cy - pr), (cx + pr, cy + pr)], fill=color)


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
        self, report: WeatherReport, units: str = "imperial"
    ) -> tuple[list[bytes], list[str], str]:
        images: list[bytes] = []
        alts:   list[str]   = []

        card1 = self._render_current_card(report, units)
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

    def _render_current_card(self, report: WeatherReport, units: str = "imperial") -> bytes:
        # Portrait layout — optimised for phone viewing
        W      = 900
        H      = 900
        H_HDR  = 76    # header strip — single row: location · timestamp · badge
        TEMP_H = 170   # snug: just enough for temp row + FEELS LIKE

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

        # ── Header — badge calculated first so timestamp can anchor to it ─────
        draw.rectangle([(0, 0), (W, H_HDR)], fill=_hex_to_rgb(HDR_BG))
        draw.line([(0, H_HDR), (W, H_HDR)], fill=_hex_to_rgb(BORDER), width=1)

        f_loc   = _font_syne(28)
        f_ts    = _font_mono(24)
        f_badge = _font_syne(19)

        badge_text = c.weather_description[:24]
        bbbox      = draw.textbbox((0, 0), badge_text, font=f_badge)
        bw, bh     = bbbox[2] - bbbox[0], bbbox[3] - bbbox[1]
        bpx, bpy   = 12, 5
        bx2        = W - 16
        bx1        = bx2 - bw - 2 * bpx
        by1        = (H_HDR - bh - 2 * bpy) // 2
        by2        = by1 + bh + 2 * bpy
        draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=100,
                                fill=(24, 36, 56), outline=_hex_to_rgb(BLUE), width=1)
        draw.text((bx1 + bpx, by1 + bpy), badge_text,
                  font=f_badge, fill=_hex_to_rgb(BLUE))

        # Location left; timestamp right-anchored to badge — they can never overlap
        loc_b  = draw.textbbox((0, 0), loc,    font=f_loc)
        ts_b   = draw.textbbox((0, 0), ts_str, font=f_ts)
        loc_h  = loc_b[3] - loc_b[1]
        ts_h   = ts_b[3]  - ts_b[1]
        row_h  = max(loc_h, ts_h)
        text_y = (H_HDR - row_h) // 2

        draw.text((20, text_y), loc, font=f_loc, fill=TEXT_PRI)
        ts_x = bx1 - (ts_b[2] - ts_b[0]) - 16
        if ts_x > 20 + (loc_b[2] - loc_b[0]) + 8:   # draw only if it fits
            draw.text((ts_x, text_y + (row_h - ts_h) // 2), ts_str,
                      font=f_ts, fill=TEXT_MUT)

        # ── Temperature block — °F and °C on the same row ─────────────────────
        draw.rectangle([(0, H_HDR), (W, H_HDR + TEMP_H)], fill=_hex_to_rgb(PANEL_L))
        draw.line([(0, H_HDR + TEMP_H), (W, H_HDR + TEMP_H)],
                  fill=_hex_to_rgb(BORDER), width=1)

        f_num    = _font_syne(104)
        f_unit   = _font_syne(47)
        f_tc_num = _font_syne(73)
        f_tc_u   = _font_syne(47)

        metric = (units == "metric")
        pri_num  = f"{c.temperature_c:.0f}" if metric else f"{c.temperature_f:.0f}"
        pri_unit = "\u00b0C"                if metric else "\u00b0F"
        sec_num  = f"{c.temperature_f:.0f}" if metric else f"{c.temperature_c:.0f}"
        sec_unit = "\u00b0F"                if metric else "\u00b0C"
        feels_val = f"{c.feels_like_c:.0f}" if metric else f"{c.feels_like_f:.0f}"
        feels_u   = "\u00b0C"               if metric else "\u00b0F"

        nb  = draw.textbbox((0, 0), pri_num,  font=f_num)
        ub  = draw.textbbox((0, 0), pri_unit, font=f_unit)
        cb  = draw.textbbox((0, 0), sec_num,  font=f_tc_num)
        tub = draw.textbbox((0, 0), sec_unit, font=f_tc_u)

        nw    = nb[2] - nb[0];  n_bot = nb[3]
        uw    = ub[2] - ub[0]
        cw    = cb[2] - cb[0]

        # Vertical: centre the temperature row + FEELS LIKE block within TEMP_H
        feels_h   = 18 + 8          # font size + margin
        content_h = n_bot + 10 + feels_h
        ty        = H_HDR + max(8, (TEMP_H - content_h) // 2)

        GAP_TC  = 32
        total_w = nw + 4 + uw + GAP_TC + cw + (tub[2] - tub[0])
        tx      = (W - total_w) // 2

        # All four elements bottom-aligned to shared baseline
        baseline = ty + n_bot
        draw.text((tx, ty), pri_num, font=f_num, fill=AMBER)
        draw.text((tx + nw + 4,                    baseline - ub[3]),  pri_unit, font=f_unit,   fill=AMBER)
        draw.text((tx + nw + 4 + uw + GAP_TC,      baseline - cb[3]),  sec_num,  font=f_tc_num, fill=TEXT_MUT)
        draw.text((tx + nw + 4 + uw + GAP_TC + cw, baseline - tub[3]), sec_unit, font=f_tc_u,  fill=TEXT_MUT)

        # "FEELS LIKE"
        _text_centered(draw, baseline + 10,
                       f"FEELS LIKE  {feels_val}{feels_u}",
                       _font_mono(18), TEXT_MUT, W)

        # ── Stats rows — single row per metric ────────────────────────────────
        STATS_Y0 = H_HDR + TEMP_H
        ROW_H    = (H - STATS_Y0) // 6

        f_lbl = _font_mono(32)
        f_pri = _font_mono(37, medium=True)
        f_sec = _font_mono(29)

        ICON_X = 30
        LBL_X  = 80
        VAL_X  = W - 16

        if metric:
            wind_pri  = f"{c.wind_speed_kph:.0f} km/h {cardinal}"
            wind_sec  = f"  \u00b7  {c.wind_speed_mph:.0f} mph"
            gusts_pri = f"{c.wind_gusts_kph:.0f} km/h"
            gusts_sec = f"  \u00b7  {c.wind_gusts_mph:.0f} mph"
            prec_pri  = f"{c.precipitation_mm:.1f} mm"
            prec_sec  = f"  \u00b7  {c.precipitation_in:.2f} in"
        else:
            wind_pri  = f"{c.wind_speed_mph:.0f} mph {cardinal}"
            wind_sec  = f"  \u00b7  {c.wind_speed_kph:.0f} km/h"
            gusts_pri = f"{c.wind_gusts_mph:.0f} mph"
            gusts_sec = f"  \u00b7  {c.wind_gusts_kph:.0f} km/h"
            prec_pri  = f"{c.precipitation_in:.2f} in"
            prec_sec  = f"  \u00b7  {c.precipitation_mm:.1f} mm"

        rows = [
            ("HUMIDITY",    "blue",  "drop",     c.humidity_pct,
             f"{c.humidity_pct:.0f} %",    None),
            ("CLOUD COVER", "amber", "cloud",    c.cloud_cover_pct,
             f"{c.cloud_cover_pct:.0f} %", None),
            ("WIND",        "muted", "wind",     None, wind_pri,  wind_sec),
            ("GUSTS",       "muted", "arrow_up", None, gusts_pri, gusts_sec),
            ("PRECIP",      "muted", "rain",     None, prec_pri,  prec_sec),
            ("PRESSURE",    "muted", "gauge",    None,
             f"{c.surface_pressure_hpa:.0f} hPa", None),
            # TODO: visibility values from Open-Meteo look suspect (e.g. 183.5 mi
            # in overcast conditions). Commenting out until the raw API values
            # can be audited and confirmed reliable.
            # ("VISIBILITY",  "muted", "eye",      None,
            #  f"{c.visibility_miles:.1f} mi",
            #  f"  \u00b7  {c.visibility_km:.1f} km"),
        ]

        for i, (label, icon_color, icon_type, bar_pct, pri_val, sec_val) in enumerate(rows):
            ry  = STATS_Y0 + i * ROW_H
            rym = ry + ROW_H // 2   # vertical centre of row

            if i > 0:
                draw.line([(0, ry), (W, ry)], fill=_hex_to_rgb(SEP), width=1)

            # Icon box — centred on row midpoint
            ic_bg  = ((18, 42, 80)  if icon_color == "blue"  else
                      (55, 36, 14)  if icon_color == "amber" else
                      (28, 40, 62))
            ic_clr = (_hex_to_rgb(BLUE)  if icon_color == "blue"  else
                      _hex_to_rgb(AMBER) if icon_color == "amber" else
                      _hex_to_rgb(TEXT_MUT))
            draw.rounded_rectangle(
                [(ICON_X - 23, rym - 23), (ICON_X + 23, rym + 23)],
                radius=7, fill=ic_bg)
            _draw_icon(draw, ICON_X, rym, 13, icon_type, ic_clr)

            # Label — vertically centred, muted colour for hierarchy
            lb = draw.textbbox((0, 0), label, font=f_lbl)
            lh = lb[3] - lb[1]
            lw = lb[2] - lb[0]
            draw.text((LBL_X, rym - lh // 2), label, font=f_lbl, fill=TEXT_MUT)

            # Value(s) — vertically centred, right-aligned
            if bar_pct is not None:
                vb     = draw.textbbox((0, 0), pri_val, font=f_pri)
                pw, ph = vb[2] - vb[0], vb[3] - vb[1]
                bar_x1 = LBL_X + lw + 16
                bar_x2 = VAL_X - pw - 12
                bar_fill = _hex_to_rgb(BLUE if icon_color == "blue" else AMBER)
                if bar_x2 > bar_x1 + 4:
                    draw.rounded_rectangle(
                        [(bar_x1, rym - 3), (bar_x2, rym + 3)],
                        radius=3, fill=_hex_to_rgb(SEP))
                    fill_w = int((bar_x2 - bar_x1) * min(bar_pct, 100.0) / 100.0)
                    if fill_w > 4:
                        draw.rounded_rectangle(
                            [(bar_x1, rym - 3), (bar_x1 + fill_w, rym + 3)],
                            radius=3, fill=bar_fill)
                draw.text((VAL_X - pw, rym - ph // 2), pri_val, font=f_pri, fill=TEXT_PRI)
            elif sec_val:
                bp     = draw.textbbox((0, 0), pri_val, font=f_pri)
                bs     = draw.textbbox((0, 0), sec_val, font=f_sec)
                pw, ph = bp[2] - bp[0], bp[3] - bp[1]
                sw, sh = bs[2] - bs[0], bs[3] - bs[1]
                x_sec  = VAL_X - sw
                x_pri  = x_sec - pw
                draw.text((x_pri, rym - ph // 2), pri_val, font=f_pri, fill=TEXT_PRI)
                draw.text((x_sec, rym - sh // 2), sec_val, font=f_sec, fill=TEXT_MUT)
            else:
                vb     = draw.textbbox((0, 0), pri_val, font=f_pri)
                pw, ph = vb[2] - vb[0], vb[3] - vb[1]
                draw.text((VAL_X - pw, rym - ph // 2), pri_val, font=f_pri, fill=TEXT_PRI)

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
        rh     = [s.humidity_pct for s in slots]
        winds  = [s.wind_speed_mph for s in slots]
        xs     = range(len(hours))

        GREEN  = "#34d399"   # RH line
        PURPLE = "#a78bfa"   # wind line

        fig, (ax_t, ax_p, ax_rh, ax_w) = plt.subplots(
            4, 1,
            sharex=True,
            figsize=(10, 7),
            dpi=90,
            layout="constrained",
            gridspec_kw={"height_ratios": [2, 1, 1, 1]},
        )
        fig.patch.set_facecolor(BG)

        def _style_ax(ax, y_color):
            ax.set_facecolor(HEADER_BG)
            for spine in ax.spines.values():
                spine.set_edgecolor(ACCENT)
            ax.tick_params(axis="y", colors=y_color, labelsize=10)
            ax.grid(axis="y", color=ACCENT, linestyle="--", linewidth=0.7, alpha=0.5)

        # --- Panel 1: Temperature ---
        _style_ax(ax_t, TEXT_AMB)
        ax_t.plot(xs, temps, color=TEXT_AMB, linewidth=2.5,
                  marker="o", markersize=7, zorder=3)
        ax_t.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
        ax_t.set_ylabel("Temp (°F)", color=TEXT_AMB, fontsize=10)
        ax_t.set_title(f"6-Hour Forecast  —  {loc}", color=TEXT_PRI, fontsize=13, pad=8)
        for x, y in zip(xs, temps):
            ax_t.annotate(f"{y:.0f}", (x, y), textcoords="offset points",
                          xytext=(0, 9), ha="center", color=TEXT_AMB, fontsize=10,
                          fontweight="bold")

        # --- Panel 2: Precip probability ---
        _style_ax(ax_p, TEXT_SKY)
        ax_p.bar(xs, precip, color=TEXT_SKY, alpha=0.75, width=0.6, zorder=3)
        ax_p.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
        ax_p.set_ylabel("Precip %", color=TEXT_SKY, fontsize=10)
        ax_p.set_ylim(0, 100)
        for x, p in zip(xs, precip):
            if p >= 5:
                ax_p.text(x, p + 2, f"{p:.0f}%", ha="center", va="bottom",
                          color=TEXT_SKY, fontsize=9, fontweight="bold")

        # --- Panel 3: Relative Humidity ---
        _style_ax(ax_rh, GREEN)
        ax_rh.plot(xs, rh, color=GREEN, linewidth=2, marker="s", markersize=5, zorder=3)
        ax_rh.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
        ax_rh.set_ylabel("RH %", color=GREEN, fontsize=10)
        ax_rh.set_ylim(0, 100)

        # --- Panel 4: Wind speed ---
        _style_ax(ax_w, PURPLE)
        ax_w.plot(xs, winds, color=PURPLE, linewidth=2, marker="^", markersize=5, zorder=3)
        ax_w.tick_params(axis="x", colors=TEXT_PRI, labelsize=11)
        ax_w.set_ylabel("Wind\n(mph)", color=PURPLE, fontsize=10)
        ax_w.set_xticks(list(xs))
        ax_w.set_xticklabels(hours)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ------------------------------------------------------------------
    # Caption and alt texts
    # ------------------------------------------------------------------

    def _caption(self, report: WeatherReport) -> str:
        import re
        c   = report.current
        loc = report.location.display_name
        cardinal = _deg_to_cardinal(c.wind_direction_deg)
        tags = ""
        m = re.search(r',\s*([A-Z]{2})\s*$', loc)
        if m:
            tags = f" #{m.group(1)}Wx"
        text = (
            f"{loc}: {c.weather_description}, "
            f"{c.temperature_f:.0f}F ({c.temperature_c:.0f}C), "
            f"humidity {c.humidity_pct:.0f}%, "
            f"wind {c.wind_speed_mph:.0f}mph {cardinal}.{tags}"
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
