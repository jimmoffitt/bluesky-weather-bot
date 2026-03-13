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
import signal
import threading
import time
from typing import Optional

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
            "[bot] Request from=%s channel=%s location=%r",
            request.requester_handle, request.source_channel, request.raw_location,
        )

        # Persist the request
        db_id = self._db.save_request(
            source_channel=request.source_channel,
            raw_content=request.raw_content,
            requester_handle=request.requester_handle,
            raw_location=request.raw_location,
            status="pending",
        )
        request.db_id = db_id

        # No location: file channel broadcasts raw content; social channels get a help reply
        if not request.raw_location:
            if request.source_channel == "file":
                self._broadcast_raw(request)
            else:
                self._send_help_reply(request)
            self._db.update_request_status(db_id, "complete")
            return

        # Weather lookup
        try:
            reports = self._weather.lookup(request.raw_location)
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

        # Format and send one thread per resolved location
        for report in reports:
            target_channel = self._route(request)
            use_images = (
                self._post_mode == "image"
                and self._image_formatter is not None
                and target_channel == "bluesky_post"   # DMs always use text
            )
            if use_images:
                images, alts, caption = self._image_formatter.format_images(report)
                thread_posts = [caption]
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
                thread_posts = self._formatter.format_thread(report)
                payload = NotificationPayload(
                    request_db_id=db_id,
                    post_thread=thread_posts,
                    reply_to_uri=request.reply_to_uri,
                    reply_to_cid=request.reply_to_cid,
                    recipient_handle=request.requester_handle,
                    target_channel=target_channel,
                )
            result = self._deliver(payload)

            # Log response
            for i, text in enumerate(thread_posts):
                self._db.save_response(
                    request_id=db_id,
                    notify_channel=result.channel,
                    message_text=text,
                    post_index=i,
                    post_uri=result.post_uri if i == 0 else None,
                )

        self._db.update_request_status(db_id, "complete")

    def _send_help_reply(self, request: AlertRequest) -> None:
        help_text = (
            "Hi! Send me a US zip code or city to get weather.\n"
            "Examples:\n"
            "  #ZipWx 80501\n"
            "  #ZipWx Denver, CO\n"
            "  #ZipWx Minneapolis"
        )
        payload = NotificationPayload(
            request_db_id=request.db_id,
            post_thread=[help_text],
            reply_to_uri=request.reply_to_uri,
            reply_to_cid=request.reply_to_cid,
            recipient_handle=request.requester_handle,
            target_channel=self._route(request),
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
