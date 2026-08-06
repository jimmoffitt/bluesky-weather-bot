from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Supported metrics
# ---------------------------------------------------------------------------
METRICS = {
    "temp_current",        # current observed temperature
    "temp_forecast_high",  # max daily high across next 7 days
    "temp_forecast_low",   # min daily low across next 7 days
    "precip_prob",         # max hourly precip probability across next 24 h
    "wind_speed",          # current wind speed
}

OPERATORS = {"gte", "lte", "gt", "lt"}

METRIC_LABELS = {
    "temp_current":       "current temperature",
    "temp_forecast_high": "forecast daily high",
    "temp_forecast_low":  "forecast daily low",
    "precip_prob":        "precipitation probability",
    "wind_speed":         "wind speed",
}

OPERATOR_SYMBOLS = {
    "gte": "≥",
    "lte": "≤",
    "gt":  ">",
    "lt":  "<",
}

# (imperial_unit, metric_unit)
METRIC_UNITS: dict[str, tuple[str, str]] = {
    "temp_current":       ("°F", "°C"),
    "temp_forecast_high": ("°F", "°C"),
    "temp_forecast_low":  ("°F", "°C"),
    "precip_prob":        ("%",  "%"),
    "wind_speed":         ("mph", "km/h"),
}


# ---------------------------------------------------------------------------
# AlarmRule dataclass
# ---------------------------------------------------------------------------

@dataclass
class AlarmRule:
    """A single user-registered weather alarm condition."""

    user_did: str
    user_handle: Optional[str]
    location_raw: str           # what the user typed (or their home location)
    metric: str                 # one of METRICS
    operator: str               # one of OPERATORS
    threshold: float
    units: str = "imperial"     # "imperial" | "metric"

    location_display: Optional[str] = None
    location_lat: Optional[float] = None
    location_lon: Optional[float] = None

    is_active: bool = True
    is_public: bool = False         # fire as a public @mention post instead of a DM
    cooldown_hours: float = 4.0     # min hours between consecutive fires
    created_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_fired_at: Optional[str] = None
    fire_count: int = 0
    id: Optional[int] = None        # set after DB insert

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def unit_label(self) -> str:
        imp_u, met_u = METRIC_UNITS.get(self.metric, ("", ""))
        return met_u if self.units == "metric" else imp_u

    def describe(self) -> str:
        """Short human-readable condition, e.g. 'current temperature ≥ 100°F'."""
        sym = OPERATOR_SYMBOLS.get(self.operator, self.operator)
        label = METRIC_LABELS.get(self.metric, self.metric)
        return f"{label} {sym} {self.threshold}{self.unit_label()}"
