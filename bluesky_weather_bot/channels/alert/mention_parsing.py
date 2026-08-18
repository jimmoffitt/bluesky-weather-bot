"""
Shared text-parsing logic for the public-@mention alert channels
(FirehoseAlertChannel, JetstreamAlertChannel).

Kept in one place — not duplicated per channel — because both channels
parse the exact same kind of content (a Bluesky post's text) from different
transports and must interpret it identically. Duplicating this logic
already caused one real bug: FirehoseAlertChannel and JetstreamAlertChannel
each carried their own copy, and only one got the "scan the whole message"
fix before this module existed.

Trigger detection:
  Post must @mention the bot handle, anywhere in the text — position
  doesn't matter. A location token is not required — if absent, bot.py
  will use the user's saved home location (or silently drop the request
  if none is set).

Location extraction — the whole message is scanned, not just the text
immediately following the mention. Priority (most reliable signal first):
  1. Zip code, anywhere in the text
  2. "City, ST", anywhere in the text
  3. A known city name (top-200 list), anywhere in the text
  4. Fallback: whatever immediately follows the mention — catches small
     towns not in the top-200 list (e.g. "@zipwx.bsky.social Timnath"),
     left to WeatherService's Nominatim fallback to resolve.

Pattern examples the parser handles:
  "@zipwx.bsky.social 80501"
  "@zipwx.bsky.social Denver, CO"
  "@zipwx.bsky.social Portland"                    ← ambiguous; resolver returns both
  "weather for Denver? @zipwx.bsky.social"
  "Hey @zipwx.bsky.social what's the forecast for Minneapolis?"
  "@zipwx.bsky.social"                             ← plain mention → uses saved home location
"""

from __future__ import annotations

import re
from typing import Optional

# Regex to find a location token after the trigger word/mention — the
# last-resort fallback (priority 4 below), for locations not caught by the
# more specific patterns.
_LOCATION_RE = re.compile(
    r"(?:@\S+)"                  # trigger: mention
    r"\s+"                       # whitespace separator
    r"([A-Za-z0-9][^#@\n]{2,40}?)(?:\s*#|\s*@|$)",  # location token
    re.IGNORECASE,
)

_ZIP_RE     = re.compile(r"\b(\d{5})\b")
_CITY_ST_RE = re.compile(r"\b([A-Z][a-zA-Z\s]{2,20}),\s*([A-Z]{2})\b")


def _build_city_re() -> re.Pattern:
    """Regex matching any top-200 city name, sorted longest-first so
    "New York" matches before "York"."""
    try:
        from bluesky_weather_bot.archive.cities import TOP_200
        names = sorted(
            {c.name.split(",")[0].strip() for c in TOP_200},
            key=len,
            reverse=True,
        )
        pattern = r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b"
        return re.compile(pattern, re.IGNORECASE)
    except Exception:
        # Fallback: never matches — zip/city-ST still work
        return re.compile(r"(?!)")


_KNOWN_CITY_RE = _build_city_re()


def is_mention_trigger(text: str, bot_handle: str) -> bool:
    """Returns True if the post @mentions the bot handle, anywhere in the text."""
    return f"@{bot_handle}" in text.lower()


def extract_location(text: str) -> Optional[str]:
    """Extracts a location from anywhere in the message text. See module
    docstring for the priority order. Returns None if nothing matches
    (informational posts, plain mentions, etc.)."""
    zip_m = _ZIP_RE.search(text)
    if zip_m:
        return zip_m.group(1)

    city_st_m = _CITY_ST_RE.search(text)
    if city_st_m:
        return f"{city_st_m.group(1).strip()}, {city_st_m.group(2)}"

    known_city_m = _KNOWN_CITY_RE.search(text)
    if known_city_m:
        return known_city_m.group(0)

    fallback_m = _LOCATION_RE.search(text)
    if fallback_m:
        candidate = fallback_m.group(1).strip()
        if candidate:
            return candidate

    return None
