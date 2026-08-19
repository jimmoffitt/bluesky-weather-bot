"""
Natural-language alarm text → AlarmRule.

Handles phrasings like:
  "alert me if temp hits 100"
  "notify me when temperature is above 100"
  "send me a DM if the forecast high exceeds 100 degrees"
  "alert me if forecast includes a day at 100 or higher"
  "alert if wind exceeds 50 mph"
  "alert me if rain chance over 80%"
  "alert me if temp in Denver, CO drops below 20"
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from bluesky_weather_bot.alarms.models import AlarmRule

# ---------------------------------------------------------------------------
# Metric detection  (higher-specificity patterns first)
# ---------------------------------------------------------------------------

_METRIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(forecast\s+(high|max)|daily\s+(high|max)|high\s+temp|max\s+temp)\b', re.I),
     "temp_forecast_high"),
    (re.compile(r'\b(forecast\s+(low|min)|daily\s+(low|min)|low\s+temp|min\s+temp)\b', re.I),
     "temp_forecast_low"),
    (re.compile(r'\b(wind|gusts?|wind\s+speed)\b', re.I),
     "wind_speed"),
    (re.compile(r'\b(rain|precipitation|precip|shower|precip\s+chance|rain\s+chance)\b', re.I),
     "precip_prob"),
    (re.compile(r'\b(temp(erature)?|heat|cold)\b', re.I),
     "temp_current"),
]

# ---------------------------------------------------------------------------
# Operator detection
# ---------------------------------------------------------------------------

_LT_PAT  = re.compile(r'\b(drops?\s+below|falls?\s+below|below|under|less\s+than)\b', re.I)
_LTE_PAT = re.compile(r'\b(drops?\s+to|falls?\s+to|or\s+lower|or\s+less|at\s+most)\b', re.I)
# "or higher / or more / at least / above / over / hits / exceeds / reaches" → gte (default)

# ---------------------------------------------------------------------------
# Threshold / units extraction
# ---------------------------------------------------------------------------

_TEMP_F_RE = re.compile(r'\b\d+(?:\.\d+)?\s*(?:°\s*f|degrees?\s*f|fahrenheit|f)\b', re.I)
_TEMP_C_RE = re.compile(r'\b\d+(?:\.\d+)?\s*(?:°\s*c|degrees?\s*c|celsius|c)\b', re.I)
_PCT_RE    = re.compile(r'\b\d+(?:\.\d+)?\s*%')
_MPH_RE    = re.compile(r'\b\d+(?:\.\d+)?\s*mph\b', re.I)
_KPH_RE    = re.compile(r'\b\d+(?:\.\d+)?\s*(?:kph|km/h|kmh)\b', re.I)

# Trailing \b alone rejects a number glued directly to a unit letter/word
# ("100F", "50mph") since digit and letter are both \w — no boundary between
# them. Accept the digit run when it's followed by end-of-string, a
# non-letter (space, %, °, punctuation), or one of the known unit words.
_NUMBER_RE = re.compile(
    r'\b(\d+(?:\.\d+)?)(?=$|[^A-Za-z]|(?:f|c|mph|kph|degrees?|fahrenheit|celsius)\b)',
    re.I,
)

# ---------------------------------------------------------------------------
# Location extraction
# "in Denver, CO", "for 80501", "at Portland OR"
# We stop at common condition words so we don't eat them as location text.
# ---------------------------------------------------------------------------

_LOCATION_RE = re.compile(
    r'\b(?:in|for|at)\s+([A-Za-z][A-Za-z0-9 ,]{2,39}?)'
    r'(?=\s*(?:drops?|falls?|hits?|is\b|gets?\b|goes?\b|reaches?|'
    r'above|below|over|under|to\b|and\b|or\b|\d|$))',
    re.I,
)

# Zip codes are all-digit, so they never match _LOCATION_RE above (which
# requires a leading letter) — check for them separately, first.
_LOCATION_ZIP_RE = re.compile(r'\b(?:in|for|at)\s+(\d{5})\b')

# Words that indicate the "in/at/for" is NOT a location prefix
_LOCATION_SKIP_FIRST_WORDS = frozenset(
    ["the", "a", "an", "any", "my", "your", "next", "this", "that"]
)

# ---------------------------------------------------------------------------
# Public-post opt-in
# "alert me publicly if ..." / "alert me if ... with post"
# ---------------------------------------------------------------------------

_PUBLIC_RE = re.compile(r'\b(publicly|with\s+post)\b', re.I)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_alarm_text(
    text: str,
    home_location: Optional[str] = None,
    user_units: str = "imperial",
) -> Tuple[Optional[AlarmRule], Optional[str]]:
    """
    Parse natural-language alarm text into a partial AlarmRule.

    The returned rule has ``user_did``, ``user_handle``, and location coords
    left blank — the caller must fill those in (e.g. after geocoding).

    Returns ``(rule, None)`` on success, ``(None, error_msg)`` on failure.
    """
    metric = _detect_metric(text)
    if metric is None:
        return None, (
            "I couldn't identify what weather condition to watch for.\n"
            "Try: 'alert me if temp hits 100' or 'alert me if rain chance over 80%'"
        )

    is_public = bool(_PUBLIC_RE.search(text))

    explicit_location, location_match = _find_location(text)
    # Strip the location clause before hunting for the threshold number, so a
    # digit location (e.g. a zip code) can't be mistaken for the threshold.
    text_for_threshold = (
        text[:location_match.start()] + text[location_match.end():]
        if location_match else text
    )

    threshold, detected_units = _extract_threshold(text_for_threshold, metric, user_units)
    if threshold is None:
        return None, (
            "Please include a number threshold.\n"
            "Example: 'alert me if temp hits 100'"
        )

    operator = _detect_operator(text)

    if is_public and not explicit_location:
        return None, (
            "Public alarms need an explicit location — I won't post your "
            "home location publicly.\n"
            "Example: 'alert me publicly if temp in Denver, CO hits 100'"
        )

    location = explicit_location or home_location
    if not location:
        return None, (
            "I don't know which location to watch.\n"
            "Set a home location first: 'set home Denver, CO'\n"
            "Or include one in your alarm: 'alert me if temp in Denver, CO hits 100'"
        )

    # Forecast alarms check once a day; current-conditions every 4 h
    cooldown = 24.0 if metric.startswith("temp_forecast") else 4.0

    rule = AlarmRule(
        user_did="",
        user_handle=None,
        location_raw=location,
        metric=metric,
        operator=operator,
        threshold=threshold,
        units=detected_units,
        cooldown_hours=cooldown,
        is_public=is_public,
    )
    return rule, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_metric(text: str) -> Optional[str]:
    """Matches alarm text against _METRIC_PATTERNS in order, returning the
    first metric that matches, or None if the text names no known metric."""
    for pattern, metric in _METRIC_PATTERNS:
        if pattern.search(text):
            return metric
    return None


def _detect_operator(text: str) -> str:
    """Detects the comparison direction from alarm text. Defaults to "gte"
    since that covers the most common phrasings ("hits", "above", "over",
    "exceeds", "or higher") without needing an explicit pattern list for each."""
    if _LT_PAT.search(text):
        return "lt"
    if _LTE_PAT.search(text):
        return "lte"
    return "gte"    # covers "hits", "above", "over", "exceeds", "or higher", etc.


def _extract_threshold(
    text: str, metric: str, fallback_units: str
) -> Tuple[Optional[float], str]:
    """
    Returns (value, units_string).  units is 'imperial' or 'metric'.
    """
    # Detect explicit units attached to a number
    if _TEMP_F_RE.search(text):
        units = "imperial"
    elif _TEMP_C_RE.search(text):
        units = "metric"
    elif _KPH_RE.search(text):
        units = "metric"
    elif _MPH_RE.search(text):
        units = "imperial"
    else:
        units = fallback_units  # use user preference when ambiguous

    m = _NUMBER_RE.search(text)
    if not m:
        return None, units
    return float(m.group(1)), units


def _extract_location(text: str) -> Optional[str]:
    """
    Extract an explicit location like 'in Denver, CO' or 'in 80501' from
    alarm text. Returns None if no location clause is found.
    """
    location, _ = _find_location(text)
    return location


def _find_location(text: str) -> Tuple[Optional[str], Optional[re.Match]]:
    """
    Locate an explicit 'in/for/at <location>' clause, checking zip codes
    first since a bare digit sequence never matches the city pattern (it
    requires a leading letter).

    Returns (candidate_or_None, match_or_None). The match is returned even
    when the candidate is rejected (e.g. a skip word) so callers can still
    strip the matched span out of the text.
    """
    zip_m = _LOCATION_ZIP_RE.search(text)
    if zip_m:
        return zip_m.group(1), zip_m
    city_m = _LOCATION_RE.search(text)
    return _location_from_match(city_m), city_m


def _location_from_match(m: Optional[re.Match]) -> Optional[str]:
    """Cleans up a location regex match: strips trailing punctuation,
    rejects it if the first word looks like a false-positive trigger word
    (see _LOCATION_SKIP_FIRST_WORDS) or the result is too short to be a
    real place name."""
    if not m:
        return None
    candidate = m.group(1).strip().rstrip(",").strip()
    first_word = candidate.split()[0].lower() if candidate else ""
    if first_word in _LOCATION_SKIP_FIRST_WORDS:
        return None
    return candidate if len(candidate) >= 3 else None
