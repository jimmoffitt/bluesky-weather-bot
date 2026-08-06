"""
AlarmChecker — background thread that evaluates weather alarm rules.

Every CHECK_INTERVAL seconds it:
  1. Loads all active AlarmRules from the database.
  2. Groups them by unique (lat, lon) to fetch weather once per location.
  3. Resolves any rules whose coordinates haven't been geocoded yet.
  4. Evaluates each rule's condition against the current WeatherReport.
  5. Fires a DM notification when the condition is met and the cooldown
     period has passed since the last fire.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from bluesky_weather_bot.alarms.models import (
    AlarmRule,
    METRIC_LABELS,
    OPERATOR_SYMBOLS,
)
from bluesky_weather_bot.channels.notify.base import NotificationPayload
from bluesky_weather_bot.channels.notify.bluesky_dm import BlueskyDMNotifyChannel
from bluesky_weather_bot.channels.notify.bluesky_post import BlueskyPostNotifyChannel
from bluesky_weather_bot.storage.db import Database
from bluesky_weather_bot.weather.models import WeatherReport
from bluesky_weather_bot.weather.service import WeatherService

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL = 900.0  # seconds (15 minutes)


class AlarmChecker:
    """
    Background thread that periodically evaluates all active alarm rules
    and sends DM notifications when conditions are met.

    Usage::

        checker = AlarmChecker(db, weather_service, dm_channel, post_channel)
        checker.start()
        ...
        checker.stop()
    """

    def __init__(
        self,
        db: Database,
        weather_service: WeatherService,
        dm_channel: BlueskyDMNotifyChannel,
        post_channel: Optional[BlueskyPostNotifyChannel] = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self._db = db
        self._weather = weather_service
        self._dm = dm_channel
        self._post = post_channel
        self._check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._check_loop,
            name="AlarmChecker",
            daemon=True,
        )
        self._thread.start()
        logger.info("[alarm] Checker started — interval %.0fs", self._check_interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._check_interval + 5)
        logger.info("[alarm] Checker stopped.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _check_loop(self) -> None:
        """Run one check immediately on start, then repeat on interval."""
        while not self._stop_event.is_set():
            try:
                self._run_checks()
            except Exception:
                logger.exception("[alarm] Unexpected error in check cycle")
            self._stop_event.wait(timeout=self._check_interval)

    def _run_checks(self) -> None:
        rules = self._db.get_active_alarm_rules()
        if not rules:
            return

        logger.debug("[alarm] Evaluating %d active rule(s)", len(rules))

        # Resolve ungeocoded rules; group resolved rules by (lat, lon)
        by_location: dict[tuple[float, float], list[AlarmRule]] = {}

        for rule in rules:
            if rule.location_lat is None or rule.location_lon is None:
                self._try_resolve_location(rule)
            if rule.location_lat is not None and rule.location_lon is not None:
                key = (round(rule.location_lat, 2), round(rule.location_lon, 2))
                by_location.setdefault(key, []).append(rule)

        # Fetch weather once per unique location, then evaluate all rules for it
        for (lat, lon), loc_rules in by_location.items():
            try:
                report = self._weather.lookup_by_coords(lat, lon)
            except Exception as exc:
                logger.warning(
                    "[alarm] Weather fetch failed for (%.2f, %.2f): %s", lat, lon, exc
                )
                continue

            for rule in loc_rules:
                try:
                    self._evaluate_and_maybe_fire(rule, report)
                except Exception:
                    logger.exception("[alarm] Error evaluating rule %d", rule.id)

    def _try_resolve_location(self, rule: AlarmRule) -> None:
        """Geocode rule.location_raw and persist the result."""
        try:
            reports = self._weather.lookup(rule.location_raw)
            if reports:
                loc = reports[0].location
                self._db.update_alarm_location(
                    rule.id, lat=loc.lat, lon=loc.lon, display=loc.display_name
                )
                rule.location_lat = loc.lat
                rule.location_lon = loc.lon
                rule.location_display = loc.display_name
        except Exception as exc:
            logger.warning(
                "[alarm] Could not resolve location %r for rule %d: %s",
                rule.location_raw, rule.id, exc,
            )

    # ------------------------------------------------------------------
    # Condition evaluation and firing
    # ------------------------------------------------------------------

    def _evaluate_and_maybe_fire(
        self, rule: AlarmRule, report: WeatherReport
    ) -> None:
        self._db.update_alarm_checked(rule.id)

        # Respect cooldown
        if rule.last_fired_at:
            try:
                last = datetime.fromisoformat(rule.last_fired_at)
                elapsed = (datetime.utcnow() - last).total_seconds()
                if elapsed < rule.cooldown_hours * 3600:
                    return
            except ValueError:
                pass

        met, actual_value = _evaluate_condition(rule, report)
        if not met:
            return

        location = rule.location_display or rule.location_raw
        logger.info(
            "[alarm] Firing rule %d for @%s — %s actual=%.1f%s (public=%s)",
            rule.id, rule.user_handle, rule.describe(),
            actual_value, rule.unit_label(), rule.is_public,
        )

        if rule.is_public:
            self._fire_public(rule, actual_value, location)
        else:
            self._fire_dm(rule, actual_value, location)

    def _fire_dm(self, rule: AlarmRule, actual_value: float, location: str) -> None:
        message = _format_alarm_message(rule, actual_value, location)
        payload = NotificationPayload(
            request_db_id=None,
            post_thread=[message],
            recipient_handle=rule.user_handle,
            target_channel="bluesky_dm",
        )
        result = self._dm.send(payload)
        if result.success:
            self._db.update_alarm_fired(rule.id)
        else:
            logger.error("[alarm] DM failed for rule %d: %s", rule.id, result.error)

    def _fire_public(self, rule: AlarmRule, actual_value: float, location: str) -> None:
        if self._post is None:
            logger.error(
                "[alarm] Rule %d is public but no post channel is configured — "
                "skipping this cycle, will retry.", rule.id,
            )
            return

        from atproto import client_utils

        mention_text = f"@{rule.user_handle}" if rule.user_handle else "Hey"
        body = _format_public_alarm_body(rule, actual_value, location)
        text_builder = client_utils.TextBuilder().mention(mention_text, rule.user_did).text(body)

        payload = NotificationPayload(
            request_db_id=None,
            post_thread=[text_builder],
            target_channel="bluesky_post",
        )
        result = self._post.send(payload)
        if result.success:
            self._db.update_alarm_fired(rule.id)
        else:
            logger.error("[alarm] Public post failed for rule %d: %s", rule.id, result.error)


# ---------------------------------------------------------------------------
# Pure helpers (module-level so they can be unit-tested independently)
# ---------------------------------------------------------------------------

def _evaluate_condition(rule: AlarmRule, report: WeatherReport) -> tuple[bool, float]:
    """
    Extract the relevant metric value from report and compare to the threshold.
    Returns (condition_met, actual_value).
    """
    use_metric = rule.units == "metric"

    if rule.metric == "temp_current":
        value = report.current.temperature_c if use_metric else report.current.temperature_f

    elif rule.metric == "temp_forecast_high":
        slots = report.daily_forecast.slots
        if not slots:
            return False, 0.0
        value = max(
            s.temp_max_c if use_metric else s.temp_max_f
            for s in slots[:7]
        )

    elif rule.metric == "temp_forecast_low":
        slots = report.daily_forecast.slots
        if not slots:
            return False, 0.0
        value = min(
            s.temp_min_c if use_metric else s.temp_min_f
            for s in slots[:7]
        )

    elif rule.metric == "precip_prob":
        slots = report.forecast.slots
        if not slots:
            return False, 0.0
        value = max(s.precipitation_probability_pct for s in slots[:24])

    elif rule.metric == "wind_speed":
        value = report.current.wind_speed_kph if use_metric else report.current.wind_speed_mph

    else:
        logger.warning("[alarm] Unknown metric %r in rule %d", rule.metric, rule.id)
        return False, 0.0

    result = {
        "gte": value >= rule.threshold,
        "lte": value <= rule.threshold,
        "gt":  value > rule.threshold,
        "lt":  value < rule.threshold,
    }.get(rule.operator, False)

    return result, value


def _format_alarm_message(rule: AlarmRule, actual_value: float, location: str) -> str:
    unit = rule.unit_label()
    metric_label = METRIC_LABELS.get(rule.metric, rule.metric).capitalize()
    op_sym = OPERATOR_SYMBOLS.get(rule.operator, rule.operator)

    return (
        f"Weather Alert — {location}\n"
        f"\n"
        f"{metric_label}: {actual_value:.1f}{unit}\n"
        f"Your condition: {rule.describe()}\n"
        f"\n"
        f"Reply 'list alarms' to manage your alerts."
    )


def _format_public_alarm_body(rule: AlarmRule, actual_value: float, location: str) -> str:
    """
    Text appended after the @mention in a public alarm post. Deliberately
    shorter than the DM version — no "reply to manage" footer, since that
    instruction only makes sense in a DM thread with the bot.
    """
    unit = rule.unit_label()
    metric_label = METRIC_LABELS.get(rule.metric, rule.metric).capitalize()

    return (
        f" — Weather Alert for {location}: "
        f"{metric_label} is {actual_value:.1f}{unit} "
        f"({rule.describe()})."
    )
