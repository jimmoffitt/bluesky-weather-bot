"""
WeatherImageFormatter

Converts a WeatherReport into 2 PNG images suitable for a single
Bluesky post with embedded image attachments.

Returns:
    (images: list[bytes], alts: list[str], caption: str)

Images produced:
  1. Current conditions card  — always present (900 × 900 px)
  2. Forecast card            — 12-hr hourly + 7-day daily + historical (900 px wide)

Requirements:
    pip install Pillow

Designed for headless operation (Raspberry Pi, CI).  Uses DejaVu fonts
(available on most Linux systems and macOS).

No emoji — plain ASCII labels only (emoji fonts not reliably available on Pi).
"""

from __future__ import annotations

import io
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

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

    elif icon_type in ("sunrise", "sunset"):
        import math
        # Horizon line centred vertically
        draw.line([(cx - r, cy), (cx + r, cy)], fill=color, width=2)
        # Sun circle above horizon
        sr   = max(3, int(r * 0.42))
        scy  = cy - sr - 4
        draw.ellipse([(cx - sr, scy - sr), (cx + sr, scy + sr)], outline=color, width=2)
        # Three rays from top half of circle
        for deg in (-45, 0, 45):
            rad    = math.radians(deg - 90)
            inner  = sr + 3
            outer  = sr + 7
            draw.line(
                [(cx + int(inner * math.cos(rad)), scy + int(inner * math.sin(rad))),
                 (cx + int(outer * math.cos(rad)), scy + int(outer * math.sin(rad)))],
                fill=color, width=2,
            )
        # Arrow below horizon indicating rise (up) or set (down)
        ah = max(4, r // 3)
        ay = cy + 4
        if icon_type == "sunrise":
            draw.polygon([(cx, ay), (cx - ah, ay + ah), (cx + ah, ay + ah)], fill=color)
        else:
            draw.polygon([(cx, ay + ah), (cx - ah, ay), (cx + ah, ay)], fill=color)


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

        card2 = self._render_forecast_card(report)
        images.append(card2)
        alts.append(self._alt_forecast_card(report))

        caption = self._caption(report)
        return images, alts, caption

    # ------------------------------------------------------------------
    # Shared header — location + timestamp (+ optional badge)
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_location_header(
        draw: ImageDraw.ImageDraw,
        W: int,
        loc: str,
        ts_str: str,
        badge_text: str = "",
    ) -> int:
        """
        Draw a single-line location + timestamp header bar consistent across all cards.
        Returns H_HDR (y position of the first content pixel below the header).
        """
        H_HDR  = 76
        f_loc   = _font_syne(24)
        f_ts    = _font_mono(18)
        f_badge = _font_syne(17)

        draw.rectangle([(0, 0), (W, H_HDR - 3)], fill=_hex_to_rgb(HDR_BG))
        draw.rectangle([(0, H_HDR - 3), (W, H_HDR)], fill=_hex_to_rgb(BLUE))

        # Badge (optional) — draw first so timestamp can anchor to its left edge
        badge_anchor = W - 16   # right edge available for timestamp
        if badge_text:
            bbbox      = draw.textbbox((0, 0), badge_text, font=f_badge)
            bw, bh     = bbbox[2] - bbbox[0], bbbox[3] - bbbox[1]
            bpx, bpy   = 10, 4
            bx2        = W - 16
            bx1        = bx2 - bw - 2 * bpx
            by1        = (H_HDR - 3 - bh - 2 * bpy) // 2
            by2        = by1 + bh + 2 * bpy
            draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=100,
                                    fill=(24, 36, 56), outline=_hex_to_rgb(BLUE), width=1)
            draw.text((bx1 + bpx, by1 + bpy), badge_text,
                      font=f_badge, fill=_hex_to_rgb(BLUE))
            badge_anchor = bx1 - 12

        # Location — left
        loc_b  = draw.textbbox((0, 0), loc, font=f_loc)
        loc_h  = loc_b[3] - loc_b[1]
        loc_y  = (H_HDR - 3 - loc_h) // 2
        draw.text((16, loc_y), loc, font=f_loc, fill=TEXT_PRI)

        # Timestamp — right-anchored to badge (or right edge); always drawn
        ts_b  = draw.textbbox((0, 0), ts_str, font=f_ts)
        tw, th = ts_b[2] - ts_b[0], ts_b[3] - ts_b[1]
        ts_x  = badge_anchor - tw
        ts_y  = (H_HDR - 3 - th) // 2
        loc_right = 16 + (loc_b[2] - loc_b[0])
        if ts_x < loc_right + 8:
            # Not enough horizontal room — draw timestamp on a second line
            ts_x = W - tw - 16
            ts_y = loc_y + loc_h - th + 4
        draw.text((ts_x, ts_y), ts_str, font=f_ts, fill=TEXT_MUT)

        return H_HDR

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

        # ── Header ────────────────────────────────────────────────────────────
        self._draw_location_header(draw, W, loc, ts_str,
                                   badge_text=c.weather_description[:24])

        # ── Temperature block — °F and °C on the same row ─────────────────────
        draw.rectangle([(0, H_HDR), (W, H_HDR + TEMP_H)], fill=_hex_to_rgb(PANEL_L))
        draw.line([(0, H_HDR + TEMP_H), (W, H_HDR + TEMP_H)],
                  fill=_hex_to_rgb(BORDER), width=1)

        f_num    = _font_syne(94)
        f_unit   = _font_syne(42)
        f_tc_num = _font_syne(66)
        f_tc_u   = _font_syne(42)

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
                       _font_mono(27), TEXT_MUT, W)

        # ── Sunrise / Sunset — right side of temperature block ────────────────
        today_slot = (report.daily_forecast.slots[0]
                      if report.daily_forecast.slots else None)
        if today_slot and (today_slot.sunrise or today_slot.sunset):
            f_sun_lbl  = _font_mono(14)
            f_sun_time = _font_mono(17, medium=True)
            icon_r     = 11
            icon_x     = W - 155
            text_x     = icon_x + icon_r + 14
            row_h      = (TEMP_H - 20) // 2
            row1_y     = H_HDR + 10 + row_h // 2
            row2_y     = H_HDR + 10 + row_h + row_h // 2

            for is_rise, slot_dt, row_y in (
                (True,  today_slot.sunrise, row1_y),
                (False, today_slot.sunset,  row2_y),
            ):
                if slot_dt is None:
                    continue
                _draw_icon(draw, icon_x, row_y, icon_r,
                           "sunrise" if is_rise else "sunset",
                           _hex_to_rgb(AMBER))
                lbl = "RISE" if is_rise else "SET"
                lb  = draw.textbbox((0, 0), lbl, font=f_sun_lbl)
                draw.text((text_x, row_y - (lb[3] - lb[1]) - 1),
                          lbl, font=f_sun_lbl, fill=TEXT_MUT)
                time_str = slot_dt.strftime("%I:%M %p").lstrip("0")
                draw.text((text_x, row_y + 2),
                          time_str, font=f_sun_time, fill=TEXT_PRI)

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
    # Card 2 — Forecast card  (12-hr hourly + 7-day daily + historical)
    # ------------------------------------------------------------------

    def _render_forecast_card(self, report: WeatherReport) -> bytes:
        MARGIN = 20

        hist = report.historical
        has_year_ago  = hist.year_ago is not None
        has_ten_yr    = hist.ten_year_avg is not None
        has_hist      = has_year_ago or has_ten_yr

        # --- 7-day column widths (content-fit) — define early to size the card ---
        _W_DAY  = 62
        _W_DESC = 150
        _W_HILO = 130
        _W_PREC = 46
        _W_WIND = 62
        _GAP    = 16
        _day_total = _W_DAY + _GAP + _W_DESC + _GAP + _W_HILO + _GAP + _W_PREC + _GAP + _W_WIND

        # Card width fits snugly around the 7-day content
        W = _day_total + 2 * MARGIN

        # Hourly strip: fixed 60px columns, centered within the day content area
        col_w      = 60
        strip_left = MARGIN + (_day_total - 6 * col_w) // 2

        # --- Compute card height ---
        H_HDR       = 76     # header bar (2 lines: location + timestamp) + accent line
        H_HR_LBL    = 34     # "NEXT 12 HOURS" section label
        H_HR_ROW    = 118    # height of one hourly row (6 cols)
        H_HR_GAP    = 10     # gap between the two hourly rows
        H_HR        = 2 * H_HR_ROW + H_HR_GAP
        H_DIV       = 6      # divider gap
        H_DAY_LBL   = 34     # "7-DAY FORECAST" section label
        H_DAY_ROW   = 48     # height of each daily row
        H_DAY       = 7 * H_DAY_ROW
        H_HIST_LBL  = 34     # "HISTORICAL" section label
        H_HIST_BLK  = 60     # height of each historical block
        H_FOOT      = 32

        H = H_HDR + H_HR_LBL + H_HR + H_DIV + H_DAY_LBL + H_DAY
        if has_hist:
            H += H_DIV + H_HIST_LBL
            if has_year_ago:  H += H_HIST_BLK
            if has_ten_yr:    H += H_HIST_BLK
        H += H_FOOT

        img, draw = _new_card(W, H)
        loc = report.location.display_name

        # --- Header ---
        ts     = report.current.timestamp
        tz     = _tz_abbr(report.location.timezone, ts)
        hour12 = ts.hour % 12 or 12
        ampm   = "AM" if ts.hour < 12 else "PM"
        ts_str = (f"{ts.strftime('%a').upper()} {ts.strftime('%b').upper()} {ts.day}"
                  f"  \u00b7  {hour12}:{ts.strftime('%M')} {ampm} {tz}")

        self._draw_location_header(draw, W, loc, ts_str)
        y = H_HDR

        # --- Hourly strip ---
        f_hr_lbl     = _font_mono(19)
        hourly_slots = report.forecast.slots[:12]

        draw.text((strip_left, y + 8), "NEXT 12 HOURS", font=f_hr_lbl, fill=TEXT_MUT)
        y += H_HR_LBL

        f_hr_time  = _font_mono(15)
        f_hr_temp  = _font_syne(20)
        f_hr_wind  = _font_mono(14)
        f_hr_pct   = _font_mono(13)

        def _draw_hourly_row(slots_row, row_y):
            for i, slot in enumerate(slots_row):
                cx = strip_left + i * col_w + col_w // 2

                if i > 0:
                    draw.line(
                        [(strip_left + i * col_w, row_y + 4),
                         (strip_left + i * col_w, row_y + H_HR_ROW - 4)],
                        fill=_hex_to_rgb(BORDER), width=1,
                    )

                # Hour label
                lbl = _hour_label(slot.hour)
                lb  = draw.textbbox((0, 0), lbl, font=f_hr_time)
                draw.text((cx - (lb[2] - lb[0]) // 2, row_y + 5),
                          lbl, font=f_hr_time, fill=TEXT_MUT)

                # Temperature
                temp_str = f"{slot.temperature_f:.0f}\u00b0"
                tb = draw.textbbox((0, 0), temp_str, font=f_hr_temp)
                draw.text((cx - (tb[2] - tb[0]) // 2, row_y + 25),
                          temp_str, font=f_hr_temp, fill=AMBER)

                # Wind speed
                wind_str = f"{slot.wind_speed_mph:.0f}mph"
                wb = draw.textbbox((0, 0), wind_str, font=f_hr_wind)
                draw.text((cx - (wb[2] - wb[0]) // 2, row_y + 51),
                          wind_str, font=f_hr_wind, fill=TEXT_MUT)

                # Precip probability bar (bottom-up)
                pct        = slot.precipitation_probability_pct
                bar_max_h  = 22
                bar_w      = max(6, col_w - 16)
                bar_bot    = row_y + H_HR_ROW - 16
                bar_top_bg = bar_bot - bar_max_h
                bx1 = cx - bar_w // 2
                bx2 = cx + bar_w // 2
                draw.rounded_rectangle([(bx1, bar_top_bg), (bx2, bar_bot)],
                                        radius=3, fill=_hex_to_rgb(SEP))
                fill_h = int(bar_max_h * min(pct, 100.0) / 100.0)
                if fill_h >= 2:
                    draw.rounded_rectangle(
                        [(bx1, bar_bot - fill_h), (bx2, bar_bot)],
                        radius=3, fill=_hex_to_rgb(BLUE))

                # Precip % label
                pct_str = f"{pct:.0f}%"
                pb = draw.textbbox((0, 0), pct_str, font=f_hr_pct)
                draw.text((cx - (pb[2] - pb[0]) // 2, bar_bot + 2),
                          pct_str, font=f_hr_pct, fill=TEXT_MUT)

        _draw_hourly_row(hourly_slots[:6], y)
        # Separator between the two rows
        row2_y = y + H_HR_ROW + H_HR_GAP
        draw.line(
            [(strip_left, row2_y - H_HR_GAP // 2),
             (strip_left + 6 * col_w, row2_y - H_HR_GAP // 2)],
            fill=_hex_to_rgb(SEP), width=1,
        )
        _draw_hourly_row(hourly_slots[6:], row2_y)

        y += H_HR

        # --- Divider ---
        draw.line([(MARGIN, y + 3), (W - MARGIN, y + 3)], fill=_hex_to_rgb(SEP), width=1)
        y += H_DIV

        # --- 7-day daily section ---
        today = date.today()
        f_day_name  = _font_mono(17, medium=True)
        f_day_desc  = _font_mono(15)
        f_day_hilo  = _font_mono(17, medium=True)
        f_day_pct   = _font_mono(15)
        f_day_wind  = _font_mono(15)

        # Column left-edge positions (card is sized to fit, so left offset = MARGIN)
        _day_left = MARGIN

        draw.text((_day_left, y + 8), "7-DAY FORECAST", font=f_hr_lbl, fill=TEXT_MUT)
        y += H_DAY_LBL

        C_DAY  = _day_left
        C_DESC = _day_left + _W_DAY  + _GAP
        C_HILO = _day_left + _W_DAY  + _GAP + _W_DESC + _GAP
        C_PREC = _day_left + _W_DAY  + _GAP + _W_DESC + _GAP + _W_HILO + _GAP
        C_WIND = _day_left + _W_DAY  + _GAP + _W_DESC + _GAP + _W_HILO + _GAP + _W_PREC + _GAP

        for i, slot in enumerate(report.daily_forecast.slots[:7]):
            ry  = y + i * H_DAY_ROW
            rym = ry + H_DAY_ROW // 2

            if i > 0:
                draw.line([(_day_left, ry), (_day_left + _day_total, ry)],
                           fill=_hex_to_rgb(SEP), width=1)

            # Highlight today
            if slot.date.date() == today:
                draw.rectangle([(_day_left - 8, ry), (_day_left + _day_total + 8, ry + H_DAY_ROW)],
                                fill=_hex_to_rgb(PANEL_L))
                if i > 0:
                    draw.line([(_day_left, ry), (_day_left + _day_total, ry)],
                               fill=_hex_to_rgb(SEP), width=1)

            # Day name
            if slot.date.date() == today:
                day_lbl   = "TODAY"
                day_color = AMBER
            else:
                day_lbl   = slot.date.strftime("%a").upper()
                day_color = TEXT_PRI

            db = draw.textbbox((0, 0), day_lbl, font=f_day_name)
            draw.text((C_DAY, rym - (db[3] - db[1]) // 2),
                      day_lbl, font=f_day_name, fill=day_color)

            # Weather description (truncate to fit column)
            desc = slot.weather_description
            while desc:
                bb = draw.textbbox((0, 0), desc, font=f_day_desc)
                if bb[2] - bb[0] <= _W_DESC - 4:
                    break
                desc = desc[:-1]
            if desc != slot.weather_description:
                desc = desc[:-1] + ".."
            db2 = draw.textbbox((0, 0), desc, font=f_day_desc)
            draw.text((C_DESC, rym - (db2[3] - db2[1]) // 2),
                      desc, font=f_day_desc, fill=TEXT_MUT)

            # Hi / Lo
            hilo_str = f"Hi {slot.temp_max_f:.0f}  Lo {slot.temp_min_f:.0f}"
            hb = draw.textbbox((0, 0), hilo_str, font=f_day_hilo)
            draw.text((C_HILO, rym - (hb[3] - hb[1]) // 2),
                      hilo_str, font=f_day_hilo, fill=AMBER)

            # Precip %
            pct_str = f"{slot.precipitation_probability_max_pct:.0f}%"
            pb = draw.textbbox((0, 0), pct_str, font=f_day_pct)
            draw.text((C_PREC, rym - (pb[3] - pb[1]) // 2),
                      pct_str, font=f_day_pct, fill=TEXT_SKY)

            # Max wind
            wind_str = f"{slot.wind_speed_max_mph:.0f} mph"
            wbb = draw.textbbox((0, 0), wind_str, font=f_day_wind)
            draw.text((C_WIND, rym - (wbb[3] - wbb[1]) // 2),
                      wind_str, font=f_day_wind, fill=TEXT_MUT)

        y += H_DAY

        # --- Historical section ---
        if has_hist:
            draw.line([(MARGIN, y + 3), (W - MARGIN, y + 3)],
                       fill=_hex_to_rgb(SEP), width=1)
            y += H_DIV
            draw.text((MARGIN, y + 8), "HISTORICAL", font=f_hr_lbl, fill=TEXT_MUT)
            y += H_HIST_LBL

            f_h_hdr = _font_mono(15, medium=True)
            f_h_val = _font_mono(15)

            def _hist_block(rec: DailyHistoricalRecord, header: str, y0: int) -> int:
                draw.text((MARGIN, y0), header, font=f_h_hdr, fill=TEXT_SKY)
                y0 += 22
                draw.text(
                    (MARGIN, y0),
                    f"Hi {rec.temp_max_f:.0f}F ({rec.temp_max_c:.0f}C)  /  "
                    f"Lo {rec.temp_min_f:.0f}F ({rec.temp_min_c:.0f}C)  |  "
                    f"Precip {rec.precipitation_in:.2f}in  |  "
                    f"Wind {rec.wind_speed_max_mph:.0f}mph",
                    font=f_h_val, fill=TEXT_PRI,
                )
                return y0 + (H_HIST_BLK - 22)

            if has_year_ago:
                d = hist.year_ago.date
                y = _hist_block(hist.year_ago,
                                f"Last year  ({d.strftime('%b')} {d.day}, {d.year})", y)

            if has_ten_yr:
                d = hist.ten_year_avg.date
                y = _hist_block(hist.ten_year_avg,
                                f"10-yr avg  ({d.strftime('%b')} {d.day} +/-7d)", y)

        # --- Footer ---
        f_foot  = _font_mono(13)
        footer  = "ZipWx  |  Open-Meteo"
        fb      = draw.textbbox((0, 0), footer, font=f_foot)
        draw.text((W - (fb[2] - fb[0]) - MARGIN, H - H_FOOT + 10),
                  footer, font=f_foot, fill=TEXT_MUT)

        return _to_png(img)

    # ------------------------------------------------------------------
    # Caption and alt texts
    # ------------------------------------------------------------------

    def _caption(self, report: WeatherReport) -> str:
        import re
        c   = report.current
        loc = report.location.display_name
        if report.location.zip_code:
            loc = f"{loc} ({report.location.zip_code})"
        cardinal = _deg_to_cardinal(c.wind_direction_deg)
        tags = ""
        m = re.search(r',\s*([A-Z]{2})\b', loc)
        if m:
            tags = f" #{m.group(1)}Wx"
        text = (
            f"{loc}: {c.weather_description}, "
            f"{c.temperature_f:.0f}F ({c.temperature_c:.0f}C), "
            f"humidity {c.humidity_pct:.0f}%, "
            f"wind {c.wind_speed_mph:.0f}mph {cardinal}.{tags}"
            f"\n\nSee second image for forecast."
        )
        return text[:300]

    def _alt_current(self, report: WeatherReport) -> str:
        c = report.current
        return (
            f"Current conditions for {report.location.display_name}: "
            f"{c.temperature_f:.0f}F, {c.weather_description}"
        )

    def _alt_forecast_card(self, report: WeatherReport) -> str:
        return f"12-hour and 7-day forecast for {report.location.display_name}"
