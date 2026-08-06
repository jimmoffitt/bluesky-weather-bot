"""
ZipWx — top-level orchestrator.

Wires alert channels → weather service → notification channels.
This is the single entry point that starts and stops the entire bot.

Data flow:
  AlertChannel (file / firehose / dm)
      → AlertRequest
          → WeatherService.lookup(raw_location)
              → list[WeatherReport]
                  → WeatherFormatter.format_thread(report)
                      → NotificationPayload
                          → NotificationChannel (bluesky_post / bluesky_dm)

Routing rules:
  - Requests from "dm" channel  → BlueskyDMNotifyChannel
  - Requests from "file" channel with no reply target → BlueskyPostNotifyChannel
    (posts to bot's own timeline as a notification broadcast)
  - Requests from "firehose"    → BlueskyPostNotifyChannel (public reply)

Usage:
    from bluesky_weather_bot.bot import ZipWx
    from bluesky_weather_bot.config.settings import Settings

    cfg = Settings.load()
    bot = ZipWx(cfg)
    bot.start()          # starts all channels; blocks until KeyboardInterrupt
"""

from __future__ import annotations

import logging
import re
import signal
import threading
import time
from datetime import datetime
from typing import Optional

from bluesky_weather_bot.alarms.checker import AlarmChecker
from bluesky_weather_bot.alarms.models import AlarmRule
from bluesky_weather_bot.alarms.parser import parse_alarm_text
from bluesky_weather_bot.channels.alert.base import AlertChannel, AlertRequest
from bluesky_weather_bot.channels.alert.dm_poller import DMAlertChannel
from bluesky_weather_bot.channels.alert.file_watcher import FileWatcherAlertChannel
from bluesky_weather_bot.channels.alert.firehose import FirehoseAlertChannel
from bluesky_weather_bot.channels.notify.base import (
    NotificationChannel, NotificationPayload,
)
from bluesky_weather_bot.channels.notify.bluesky_dm import BlueskyDMNotifyChannel
from bluesky_weather_bot.channels.notify.bluesky_post import BlueskyPostNotifyChannel
from bluesky_weather_bot.config.settings import Settings
from bluesky_weather_bot.storage.db import Database
from bluesky_weather_bot.weather.formatter import WeatherFormatter
from bluesky_weather_bot.weather.service import WeatherService

logger = logging.getLogger(__name__)


def _try_build_image_formatter():
    """Load WeatherImageFormatter; return None (log warning) if Pillow/matplotlib absent."""
    try:
        from bluesky_weather_bot.weather.image_formatter import WeatherImageFormatter
        return WeatherImageFormatter()
    except ImportError as exc:
        logger.warning("[bot] WeatherImageFormatter unavailable: %s", exc)
        return None


class ZipWx:
    """
    Assembles and runs the full bot pipeline.

    Alert channels and notification channels are registered separately,
    so adding a new channel type only requires instantiating it here
    and registering it — no changes to the pipeline logic.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings  = settings
        self._db        = Database(path=settings.db_path)
        self._formatter = WeatherFormatter()
        self._weather   = WeatherService(
            db=self._db,
            skip_historical=settings.skip_historical,
        )

        self._post_mode      = settings.post_mode
        self._image_formatter = None
        if settings.post_mode == "image":
            self._image_formatter = _try_build_image_formatter()
            if self._image_formatter is None:
                logger.warning("[bot] Falling back to text mode.")
                self._post_mode = "text"
        logger.info("[bot] post_mode=%s", self._post_mode)

        # Alert channels (inputs)
        self._alert_channels: list[AlertChannel] = []

        # Notification channels (outputs), keyed by channel name
        self._notify_channels: dict[str, NotificationChannel] = {}

        # Background alarm checker (started in start(), stopped in stop())
        self._alarm_checker: Optional[AlarmChecker] = None

        self._running = False
        self._lock    = threading.Lock()

    # ------------------------------------------------------------------
    # Channel registration
    # ------------------------------------------------------------------

    def register_alert_channel(self, channel: AlertChannel) -> None:
        """Register an alert input channel. Call before start()."""
        channel.on_request(self._handle_request)
        self._alert_channels.append(channel)
        logger.info("Registered alert channel: %s", channel)

    def register_notify_channel(self, channel: NotificationChannel) -> None:
        """Register a notification output channel. Call before start()."""
        self._notify_channels[channel.CHANNEL_NAME] = channel
        logger.info("Registered notify channel: %s", channel)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, block: bool = True) -> None:
        """
        Opens the DB, starts all registered channels, then optionally blocks.
        Installs SIGINT/SIGTERM handlers when blocking so Ctrl+C stops cleanly.
        """
        settings = self._settings
        settings.ensure_directories()
        self._db.connect()

        self._running = True
        for ch in self._alert_channels:
            ch.start()

        dm_channel = self._notify_channels.get("bluesky_dm")
        post_channel = self._notify_channels.get("bluesky_post")
        if isinstance(dm_channel, BlueskyDMNotifyChannel):
            self._alarm_checker = AlarmChecker(
                db=self._db,
                weather_service=self._weather,
                dm_channel=dm_channel,
                post_channel=post_channel if isinstance(post_channel, BlueskyPostNotifyChannel) else None,
            )
            self._alarm_checker.start()

        logger.info("ZipWx running. %d alert channel(s), %d notify channel(s).",
                    len(self._alert_channels), len(self._notify_channels))

        if block:
            self._block_until_signal()

    def stop(self) -> None:
        """Stop all channels and close the database."""
        if not self._running:
            return
        self._running = False
        logger.info("ZipWx shutting down...")
        if self._alarm_checker is not None:
            try:
                self._alarm_checker.stop()
            except Exception as exc:
                logger.warning("Error stopping AlarmChecker: %s", exc)
        for ch in self._alert_channels:
            try:
                ch.stop()
            except Exception as exc:
                logger.warning("Error stopping %s: %s", ch, exc)
        self._db.close()
        logger.info("ZipWx stopped.")

    # ------------------------------------------------------------------
    # Request handler (called by alert channels on their threads)
    # ------------------------------------------------------------------

    def _handle_request(self, request: AlertRequest) -> None:
        """
        Core pipeline: AlertRequest → weather lookup → format → notify.
        Runs on the alert channel's thread; exceptions are caught here
        so a bad request never crashes the channel.
        """
        logger.info(
            "[bot] Request from=%s channel=%s location=%r command=%r",
            request.requester_handle, request.source_channel,
            request.raw_location, request.command,
        )

        # Persist the request — returns None if this source_uri was already processed
        db_id = self._db.save_request(
            source_channel=request.source_channel,
            raw_content=request.raw_content,
            requester_handle=request.requester_handle,
            raw_location=request.raw_location,
            status="pending",
            ingested_at=request.received_at.isoformat(),
            source_uri=request.reply_to_uri if request.source_channel == "firehose" else None,
        )
        if db_id is None:
            logger.debug(
                "[bot] Duplicate request dropped: channel=%s uri=%s",
                request.source_channel, request.reply_to_uri,
            )
            return
        request.db_id = db_id

        # DM preference commands are handled separately
        if request.command:
            self._handle_dm_command(request)
            self._db.update_request_status(db_id, "complete")
            return

        # Home location fallback for any channel with no explicit location
        if not request.raw_location and request.requester_did:
            prefs = self._db.get_user_prefs(request.requester_did)
            if prefs and prefs.get("home_raw"):
                request.raw_location = prefs["home_raw"]
                logger.info("[bot] Using home location %r for %s",
                            request.raw_location, request.requester_handle)

        # No location: file channel broadcasts raw content; DM gets help; firehose is silent drop
        if not request.raw_location:
            if request.source_channel == "file":
                self._broadcast_raw(request)
            elif request.source_channel == "dm":
                self._send_dm_reply(request, (
                    "Send me a zip code or city name for weather.\n"
                    "Examples: 80501  or  Denver, CO\n\n"
                    "You can also set a home location:\n"
                    "  set home Denver, CO\n\n"
                    "Send 'help' for all commands."
                ))
            elif request.source_channel == "firehose":
                # Plain mention with no location and no home — ignore quietly
                logger.debug("[bot] No location and no home for %s — ignoring",
                             request.requester_handle)
            else:
                self._send_help_reply(request)
            self._db.update_request_status(db_id, "complete")
            return

        # Directives: '/forecast' and '/day' opt into extra images/sections —
        # both are skipped by default since '/day' in particular (a ~75-year
        # archive query) dominates request latency when nobody asked for it.
        include_forecast = "forecast" in request.directives
        include_day      = "day" in request.directives

        # Weather lookup
        self._db.update_request_processing_start(db_id)
        try:
            reports = self._weather.lookup(request.raw_location, include_day_history=include_day)
        except ValueError as exc:
            logger.warning("[bot] Location not resolved: %s", exc)
            self._send_error_reply(request, f"Sorry, I couldn't find weather for {request.raw_location!r}.")
            self._db.update_request_status(db_id, "error")
            return
        except Exception as exc:
            logger.error("[bot] Weather lookup failed: %s", exc)
            self._send_error_reply(request, "Sorry, weather data is temporarily unavailable.")
            self._db.update_request_status(db_id, "error")
            return

        # Look up user preferences (units + layout; applies to all channels)
        units  = "imperial"
        layout = "phone"
        if request.requester_did:
            prefs = self._db.get_user_prefs(request.requester_did)
            if prefs:
                units  = prefs.get("units",  "imperial")
                layout = prefs.get("layout", "phone")

        # Format and send one thread per resolved location
        self._db.update_request_formatting_start(db_id)
        delivery_finished_at = None
        for report in reports:
            target_channel = self._route(request)
            use_images = (
                self._post_mode == "image"
                and self._image_formatter is not None
                and target_channel == "bluesky_post"   # DMs always use text
            )
            if use_images:
                images, alts, caption = self._image_formatter.format_images(
                    report, units=units, layout=layout,
                    include_forecast=include_forecast, include_day=include_day,
                )
                thread_posts = [caption]
                _append_latency_footer(thread_posts, request.received_at, self._settings.server_type)
                payload = NotificationPayload(
                    request_db_id=db_id,
                    post_thread=thread_posts,
                    reply_to_uri=request.reply_to_uri,
                    reply_to_cid=request.reply_to_cid,
                    recipient_handle=request.requester_handle,
                    target_channel=target_channel,
                    post_images=images,
                    post_image_alts=alts,
                )
            else:
                thread_posts = self._formatter.format_thread(report, units=units)
                _append_latency_footer(thread_posts, request.received_at, self._settings.server_type)
                payload = NotificationPayload(
                    request_db_id=db_id,
                    post_thread=thread_posts,
                    reply_to_uri=request.reply_to_uri,
                    reply_to_cid=request.reply_to_cid,
                    recipient_handle=request.requester_handle,
                    target_channel=target_channel,
                )
            delivery_started_at = _now()
            result = self._deliver(payload)
            delivery_finished_at = _now()

            # Log response
            for i, text in enumerate(thread_posts):
                self._db.save_response(
                    request_id=db_id,
                    notify_channel=result.channel,
                    message_text=text,
                    post_index=i,
                    post_uri=result.post_uri if i == 0 else None,
                    delivery_started_at=delivery_started_at,
                    delivery_finished_at=delivery_finished_at,
                )

        self._db.update_request_processing_finish(db_id)
        if delivery_finished_at:
            self._db.update_request_mark_slow(
                db_id,
                delivery_finished_at=delivery_finished_at,
                source_created_at=None,  # source_created_at not yet captured from AT Protocol
            )
        self._db.update_request_status(db_id, "complete")

    def _handle_dm_command(self, request: AlertRequest) -> None:
        """Handles DM preference commands (set home, set units, settings, reset, help)."""
        cmd = request.command
        did = request.requester_did

        if cmd == "set_home":
            raw = request.raw_content.strip()
            for prefix in ("set home ", "set home"):
                if raw.lower().startswith(prefix):
                    raw = raw[len(prefix):].strip()
                    break
            if not raw:
                self._send_dm_reply(request, "Please provide a location: set home Denver, CO")
                return
            try:
                reports = self._weather.lookup(raw)
                display = reports[0].location.display_name if reports else raw
                self._db.set_user_prefs(did, handle=request.requester_handle,
                                        home_raw=raw, home_display=display)
                self._send_dm_reply(request, f"Configuration received. Home location set to {display}.")
            except Exception:
                self._send_dm_reply(request,
                    f"Couldn't find location: {raw!r}. Try a zip code or 'City, ST'.")

        elif cmd == "set_units_imperial":
            self._db.set_user_prefs(did, handle=request.requester_handle, units="imperial")
            self._send_dm_reply(request,
                "Configuration received. Showing °F first (°C still included).")

        elif cmd == "set_units_metric":
            self._db.set_user_prefs(did, handle=request.requester_handle, units="metric")
            self._send_dm_reply(request,
                "Configuration received. Showing °C first (°F still included).")

        elif cmd == "clear_home":
            if did:
                self._db.clear_home(did)
            self._send_dm_reply(request, "Configuration received. Home location cleared.")

        elif cmd == "reset_prefs":
            if did:
                self._db.reset_prefs(did)
            self._send_dm_reply(request, "Configuration received. Preferences reset to defaults (imperial units, phone layout, no home).")

        elif cmd == "set_layout_desktop":
            self._db.set_user_prefs(did, handle=request.requester_handle, layout="desktop")
            self._send_dm_reply(request,
                "Configuration received. Now creating images optimized for a monitor.")

        elif cmd == "set_layout_phone":
            self._db.set_user_prefs(did, handle=request.requester_handle, layout="phone")
            self._send_dm_reply(request,
                "Configuration received. Now creating images optimized for a phone.")

        elif cmd == "settings":
            prefs = self._db.get_user_prefs(did) if did else None
            if prefs:
                home   = prefs.get("home_display") or prefs.get("home_raw") or "not set"
                units  = prefs.get("units", "imperial")
                layout = prefs.get("layout", "phone")
            else:
                home, units, layout = "not set", "imperial", "phone"
            self._send_dm_reply(request,
                f"Your settings:\n  Units: {units}\n  Layout: {layout}\n  Home: {home}")

        elif cmd == "set_alarm":
            if not did:
                self._send_dm_reply(request, "Sorry, I couldn't identify your account.")
                return
            prefs = self._db.get_user_prefs(did)
            user_units    = (prefs or {}).get("units", "imperial")
            home_location = (prefs or {}).get("home_raw")

            rule, err = parse_alarm_text(
                request.raw_content,
                home_location=home_location,
                user_units=user_units,
            )
            if err:
                self._send_dm_reply(request, f"Couldn't create alarm.\n\n{err}")
                return

            existing = self._db.get_alarm_rules_for_user(did)
            dup = _find_duplicate_alarm(rule, existing)
            if dup is not None:
                loc = dup.location_display or dup.location_raw
                self._send_dm_reply(request,
                    f"You already have that alarm.\n"
                    f"  {loc}: {dup.describe()}\n\n"
                    f"Reply 'list alarms' to see all your alerts."
                )
                return

            rule.user_did    = did
            rule.user_handle = request.requester_handle

            # Geocode location eagerly; checker retries on failure
            try:
                reports = self._weather.lookup(rule.location_raw)
                if reports:
                    loc = reports[0].location
                    rule.location_lat     = loc.lat
                    rule.location_lon     = loc.lon
                    rule.location_display = loc.display_name
            except Exception:
                pass  # checker will resolve on first run

            rule_id = self._db.add_alarm_rule(rule)
            rule.id = rule_id
            location_display = rule.location_display or rule.location_raw
            notify_line = (
                "I'll post publicly (mentioning you) when this is met"
                if rule.is_public else
                "I'll DM you when this is met"
            )
            self._send_dm_reply(request,
                f"Alarm set for {location_display}.\n"
                f"Condition: {rule.describe()}\n"
                f"{notify_line} (cooldown: {int(rule.cooldown_hours)}h).\n\n"
                f"Reply 'list alarms' to manage your alerts."
            )

        elif cmd == "list_alarms":
            if not did:
                self._send_dm_reply(request, "Sorry, I couldn't identify your account.")
                return
            rules = self._db.get_alarm_rules_for_user(did)
            if not rules:
                self._send_dm_reply(request,
                    "You have no active alarms.\n\n"
                    "To set one, try:\n"
                    "  alert me if temp hits 100\n"
                    "  alert me if rain chance over 80%"
                )
                return
            lines = [f"Your active alarms ({len(rules)}):"]
            for i, r in enumerate(rules, 1):
                loc = r.location_display or r.location_raw
                fires = f" ({r.fire_count}x fired)" if r.fire_count else ""
                public = " [public]" if r.is_public else ""
                lines.append(f"  {i}. {loc}: {r.describe()}{public}{fires}")
            lines.append("\nTo change: 'edit alarm 1 to ...'  |  'delete alarm 1'  |  'clear alarms'")
            self._send_dm_reply(request, "\n".join(lines))

        elif cmd == "delete_alarm":
            if not did:
                self._send_dm_reply(request, "Sorry, I couldn't identify your account.")
                return
            m = re.search(r"\d+", request.raw_content)
            if not m:
                self._send_dm_reply(request,
                    "Please specify which alarm to delete.\nExample: 'delete alarm 1'")
                return
            index = int(m.group()) - 1
            rules = self._db.get_alarm_rules_for_user(did)
            if not rules:
                self._send_dm_reply(request, "You have no active alarms.")
                return
            if index < 0 or index >= len(rules):
                self._send_dm_reply(request,
                    f"Alarm #{index + 1} not found. "
                    f"You have {len(rules)} active alarm(s).\n"
                    f"Reply 'list alarms' to see them."
                )
                return
            rule = rules[index]
            self._db.deactivate_alarm_rule(rule.id)
            loc = rule.location_display or rule.location_raw
            self._send_dm_reply(request,
                f"Alarm #{index + 1} deleted.\n"
                f"  {loc}: {rule.describe()}"
            )

        elif cmd == "edit_alarm":
            if not did:
                self._send_dm_reply(request, "Sorry, I couldn't identify your account.")
                return
            m = re.match(
                r"^(?:edit|update|change)\s+(?:alarm|alert)\s+(\d+)\s*(?:to\s*)?[:,-]?\s*(.*)$",
                request.raw_content.strip(),
                re.I,
            )
            new_condition_text = m.group(2).strip() if m else ""
            if not m or not new_condition_text:
                self._send_dm_reply(request,
                    "Please describe the new condition.\n"
                    "Example: 'edit alarm 1 to alert if temp hits 90'")
                return

            index = int(m.group(1)) - 1
            rules = self._db.get_alarm_rules_for_user(did)
            if index < 0 or index >= len(rules):
                self._send_dm_reply(request,
                    f"Alarm #{index + 1} not found. "
                    f"You have {len(rules)} active alarm(s).\n"
                    f"Reply 'list alarms' to see them."
                )
                return
            rule = rules[index]

            prefs = self._db.get_user_prefs(did)
            user_units = (prefs or {}).get("units", "imperial")
            home_location = (prefs or {}).get("home_raw")

            new_rule, err = parse_alarm_text(
                new_condition_text,
                home_location=home_location or rule.location_raw,
                user_units=user_units,
            )
            if err:
                self._send_dm_reply(request, f"Couldn't update alarm.\n\n{err}")
                return

            location_changed = new_rule.location_raw.strip().lower() != (rule.location_raw or "").strip().lower()
            if location_changed:
                lat, lon, display = None, None, None
                try:
                    reports = self._weather.lookup(new_rule.location_raw)
                    if reports:
                        loc = reports[0].location
                        lat, lon, display = loc.lat, loc.lon, loc.display_name
                except Exception:
                    pass  # checker will resolve on first run
            else:
                lat, lon, display = rule.location_lat, rule.location_lon, rule.location_display

            self._db.update_alarm_rule(
                rule.id,
                location_raw=new_rule.location_raw,
                location_display=display,
                location_lat=lat,
                location_lon=lon,
                metric=new_rule.metric,
                operator=new_rule.operator,
                threshold=new_rule.threshold,
                units=new_rule.units,
                is_public=new_rule.is_public,
            )
            loc = display or new_rule.location_raw
            public_note = " [public]" if new_rule.is_public else ""
            self._send_dm_reply(request,
                f"Alarm #{index + 1} updated.\n"
                f"  {loc}: {new_rule.describe()}{public_note}"
            )

        elif cmd == "clear_alarms":
            if not did:
                self._send_dm_reply(request, "Sorry, I couldn't identify your account.")
                return
            count = self._db.clear_alarm_rules_for_user(did)
            if count == 0:
                self._send_dm_reply(request, "You have no active alarms to clear.")
            else:
                self._send_dm_reply(request,
                    f"Cleared {count} alarm{'s' if count != 1 else ''}.")

        elif cmd == "help":
            lines = [
                "ZipWx commands (via DM):\n"
                "  80501 or Denver, CO           — get weather\n"
                "  set home Denver, CO           — save home location\n"
                "  clear home                    — remove home location\n"
                "  imperial / metric             — display units\n"
                "  desktop / phone               — image layout\n"
                "  settings                      — view preferences\n"
                "  reset                         — clear all preferences\n\n"
                "Weather alarms:\n"
                "  alert me if temp hits 100     — DM when condition is met\n"
                "  alert me if rain chance > 80% — any threshold or metric\n"
                "  alert me publicly if ...      — public post + mention instead\n"
                "                                   of a DM (needs an explicit\n"
                "                                   location, e.g. 'in Denver, CO')\n"
                "  list alarms                   — view active alarms\n"
                "  edit alarm 1 to ...           — change an alarm's condition\n"
                "  delete alarm 1                — remove alarm by number\n"
                "  clear alarms                  — remove all alarms"
            ]
            _append_latency_footer(lines, request.received_at, self._settings.server_type)
            self._send_dm_reply(request, lines[0])

        else:
            logger.warning("[bot] Unknown DM command: %r", cmd)

    def _send_dm_reply(self, request: AlertRequest, message: str) -> None:
        payload = NotificationPayload(
            request_db_id=request.db_id,
            post_thread=[message],
            reply_to_uri=request.reply_to_uri,
            recipient_handle=request.requester_handle,
            target_channel="bluesky_dm",
        )
        self._deliver(payload)

    def _send_help_reply(self, request: AlertRequest) -> None:
        help_text = (
            "Mention me with a zip code or city to get weather.\n"
            "Examples:\n"
            "  @zipwx.bsky.social 80501\n"
            "  @zipwx.bsky.social Denver, CO\n\n"
            "Add /forecast for the 12-hr + 7-day forecast card, "
            "or /day for the historical \"on this day\" chart:\n"
            "  @zipwx.bsky.social 80501 /forecast /day\n\n"
            "DM me for personalized weather and to set a home location."
        )
        images, alts = None, None
        if self._image_formatter is not None:
            try:
                from bluesky_weather_bot.weather.image_formatter import WeatherImageFormatter
                card = WeatherImageFormatter.render_help_card()
                images = [card]
                alts   = ["ZipWx DM command reference card"]
            except Exception:
                logger.warning("[bot] Could not render help card", exc_info=True)
        payload = NotificationPayload(
            request_db_id=request.db_id,
            post_thread=[help_text],
            reply_to_uri=request.reply_to_uri,
            reply_to_cid=request.reply_to_cid,
            recipient_handle=request.requester_handle,
            target_channel=self._route(request),
            post_images=images,
            post_image_alts=alts,
        )
        self._deliver(payload)

    def _broadcast_raw(self, request: AlertRequest) -> None:
        """
        Sends the raw message content (no weather lookup) as a public post.
        Used for file-based alerts that are pure notification broadcasts.
        """
        payload = NotificationPayload(
            request_db_id=request.db_id,
            post_thread=[request.raw_content],
            target_channel="bluesky_post",
        )
        self._deliver(payload)

    def _send_error_reply(self, request: AlertRequest, message: str) -> None:
        payload = NotificationPayload(
            request_db_id=request.db_id,
            post_thread=[message],
            reply_to_uri=request.reply_to_uri,
            reply_to_cid=request.reply_to_cid,
            recipient_handle=request.requester_handle,
            target_channel=self._route(request),
        )
        self._deliver(payload)

    # ------------------------------------------------------------------
    # Routing and delivery
    # ------------------------------------------------------------------

    def _route(self, request: AlertRequest) -> str:
        """
        Determines which notification channel to use based on the source.
          dm       → bluesky_dm
          firehose → bluesky_post
          file     → bluesky_post
        """
        if request.source_channel == "dm":
            return "bluesky_dm"
        return "bluesky_post"

    def _deliver(self, payload: NotificationPayload) -> "NotificationResult":
        channel = self._notify_channels.get(payload.target_channel)
        if channel is None:
            # Fallback to any available notify channel
            channel = next(iter(self._notify_channels.values()), None)
        if channel is None:
            logger.error("[bot] No notification channel available!")
            from bluesky_weather_bot.channels.notify.base import NotificationResult
            return NotificationResult(success=False, channel="none", error="No channel")
        return channel.send(payload)

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _block_until_signal(self) -> None:
        stop_event = threading.Event()

        def _handler(sig, frame):
            logger.info("Signal %s received — stopping.", sig)
            stop_event.set()

        signal.signal(signal.SIGINT,  _handler)
        signal.signal(signal.SIGTERM, _handler)

        stop_event.wait()
        self.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat()


def _find_duplicate_alarm(candidate: AlarmRule, existing: list[AlarmRule]) -> Optional[AlarmRule]:
    """
    Returns the existing rule that duplicates ``candidate`` (same metric,
    operator, threshold, location, and public/private-ness), or None if
    there's no match. A public and private alarm with an otherwise identical
    condition are distinct rules, not duplicates of each other.
    """
    candidate_loc = candidate.location_raw.strip().lower()
    for rule in existing:
        if (
            rule.metric == candidate.metric
            and rule.operator == candidate.operator
            and rule.threshold == candidate.threshold
            and rule.location_raw.strip().lower() == candidate_loc
            and rule.is_public == candidate.is_public
        ):
            return rule
    return None


_SERVER_DESCRIPTIONS = {
    "Pi": "a Raspberry Pi running in my basement",
}


def _append_latency_footer(thread_posts: list[str], received_at: datetime,
                           server_type: str = "laptop") -> None:
    """
    Appends a latency footer to the last post in the thread.
    Measures time from when the request was ingested to now (just before delivery).
    Skips silently if the footer would push the last post over 300 chars.
    """
    elapsed = (datetime.utcnow() - received_at).total_seconds()
    description = _SERVER_DESCRIPTIONS.get(server_type, f"a {server_type}")
    footer = f"\n\nResponded in {elapsed:.1f} seconds from {description}."
    if len(thread_posts[-1]) + len(footer) <= 300:
        thread_posts[-1] += footer


# ---------------------------------------------------------------------------
# Convenience factory — wires the standard configuration
# ---------------------------------------------------------------------------

def build_bot(settings: Settings) -> ZipWx:
    """
    Constructs a fully wired ZipWx with all standard channels.
    Override this (or build manually) to customize the channel mix.
    """
    bot = ZipWx(settings)

    # Alert channels
    bot.register_alert_channel(FileWatcherAlertChannel(settings))
    bot.register_alert_channel(FirehoseAlertChannel(settings))
    bot.register_alert_channel(DMAlertChannel(settings, db=bot._db))

    # Notification channels
    bot.register_notify_channel(
        BlueskyPostNotifyChannel(settings.bluesky_handle, settings.bluesky_app_password)
    )
    bot.register_notify_channel(
        BlueskyDMNotifyChannel(settings.bluesky_handle, settings.bluesky_app_password)
    )

    return bot
