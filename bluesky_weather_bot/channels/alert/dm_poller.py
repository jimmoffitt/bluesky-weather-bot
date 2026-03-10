"""
DMAlertChannel

Polls the Bluesky chat API on a configurable interval to check for new
Direct Messages addressed to the bot. Messages that contain a recognizable
location (zip code, city/state, or bare city name) are dispatched as
AlertRequests with source_channel="dm" so the orchestrator routes the
response back via DM rather than a public reply.

Uses chat.bsky.convo.get_log with a cursor for delta sync — only new
events since the last poll are returned, so there is no redundant
re-scanning of old conversations.

Message deduplication is two-layered:
  1. In-memory set (fast, cleared on restart).
  2. DB seen_dm_ids table (survives restarts; requires db= parameter).

Dependencies: pip install atproto
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

from bluesky_weather_bot.channels.alert.base import AlertChannel, AlertRequest
from bluesky_weather_bot.config.settings import Settings

logger = logging.getLogger(__name__)

_ZIP_RE     = re.compile(r"\b(\d{5})\b")
_CITY_ST_RE = re.compile(r"\b([A-Z][a-zA-Z\s]{2,20}),\s*([A-Z]{2})\b")
_ZIPWX_RE   = re.compile(r"#zipwx", re.IGNORECASE)

DEFAULT_POLL_INTERVAL = 15.0


class DMAlertChannel(AlertChannel):
    """
    Polls Bluesky Direct Messages and dispatches AlertRequests for
    messages that contain weather location requests.
    """

    CHANNEL_NAME = "dm"

    def __init__(
        self,
        settings: Settings,
        db=None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        super().__init__()
        self._handle        = settings.bluesky_handle
        self._app_password  = settings.bluesky_app_password
        self._db            = db          # Optional[Database] for seen_dm_ids persistence
        self._poll_interval = poll_interval
        self._stop_event    = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client        = None        # atproto Client, set in _login()
        self._chat_client   = None        # proxy client for chat.bsky.convo.*
        self._own_did: str  = ""          # bot's own DID, set in _login()
        self._cursor: Optional[str] = None  # get_log delta cursor
        self._seen_ids: set[str] = set()  # in-memory dedup (session only)

    # ------------------------------------------------------------------
    # AlertChannel interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="DMPoller",
            daemon=True,
        )
        self._thread.start()
        logger.info("[dm] Started — polling every %.0fs", self._poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._poll_interval + 2)
        logger.info("[dm] Stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        self._login()
        while not self._stop_event.is_set():
            try:
                self._check_dms()
            except Exception as exc:
                logger.warning("[dm] Error during poll: %s", exc)
                if "auth" in str(exc).lower() or "expired" in str(exc).lower():
                    self._login()
            self._stop_event.wait(timeout=self._poll_interval)

    def _login(self) -> None:
        try:
            from atproto import Client
            self._client = Client()
            self._client.login(self._handle, self._app_password)
            self._own_did = self._client.me.did
            self._chat_client = self._client.with_bsky_chat_proxy()
            logger.info("[dm] Authenticated as %s (DID: %s)", self._handle, self._own_did)
        except ImportError:
            logger.error("[dm] atproto not installed: pip install atproto")
            self._stop_event.set()
        except Exception as exc:
            logger.error("[dm] Login failed: %s", exc)

    def _check_dms(self) -> None:
        if self._chat_client is None:
            return

        from atproto import models as atproto_models

        try:
            params = atproto_models.ChatBskyConvoGetLog.Params(cursor=self._cursor)
            resp = self._chat_client.chat.bsky.convo.get_log(params=params)
        except Exception as exc:
            logger.warning("[dm] get_log failed: %s", exc)
            return

        if resp.cursor:
            self._cursor = resp.cursor

        for log in resp.logs:
            try:
                if not isinstance(log, atproto_models.ChatBskyConvoDefs.LogCreateMessage):
                    continue

                msg = log.message

                # Skip messages sent by the bot itself
                if getattr(getattr(msg, "sender", None), "did", "") == self._own_did:
                    continue

                msg_id = getattr(msg, "id", None)
                if not msg_id or self._is_seen(msg_id):
                    continue

                text = getattr(msg, "text", "") or ""
                raw_location = self._extract_location(text)

                # Mark seen before dispatch so a crash mid-pipeline doesn't reprocess
                self._mark_seen(msg_id, log.convo_id)

                if raw_location is None:
                    logger.debug("[dm] No location in message %s: %r", msg_id, text[:60])

                sender_did = getattr(getattr(msg, "sender", None), "did", "") or ""
                sender_handle = self._did_to_handle(sender_did) or sender_did

                request = AlertRequest(
                    source_channel="dm",
                    requester_handle=sender_handle,
                    raw_location=raw_location,
                    raw_content=text,
                    # reply_to_uri carries convo_id so BlueskyDMNotifyChannel
                    # knows which conversation to reply to
                    reply_to_uri=log.convo_id,
                )
                self._dispatch(request)

            except Exception as exc:
                logger.debug("[dm] Skipping malformed log entry: %s", exc)

    # ------------------------------------------------------------------
    # Deduplication helpers
    # ------------------------------------------------------------------

    def _is_seen(self, msg_id: str) -> bool:
        if msg_id in self._seen_ids:
            return True
        if self._db is not None:
            try:
                return self._db.is_dm_seen(msg_id)
            except Exception:
                pass
        return False

    def _mark_seen(self, msg_id: str, convo_id: str) -> None:
        self._seen_ids.add(msg_id)
        if self._db is not None:
            try:
                self._db.mark_dm_seen(msg_id, convo_id)
            except Exception as exc:
                logger.debug("[dm] Could not persist seen DM %s: %s", msg_id, exc)

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    def _did_to_handle(self, did: str) -> Optional[str]:
        """Resolves a DID to a handle string. Best-effort."""
        if not did or self._client is None:
            return None
        try:
            profile = self._client.app.bsky.actor.get_profile({"actor": did})
            return getattr(profile, "handle", None)
        except Exception:
            return None

    @staticmethod
    def _extract_location(text: str) -> Optional[str]:
        """
        Extracts a location from DM text.

        Priority:
          1. 5-digit zip code
          2. City, ST pattern
          3. Remaining text after stripping #ZipWx trigger (if 3–50 chars)
        """
        zip_m = _ZIP_RE.search(text)
        if zip_m:
            return zip_m.group(1)

        city_m = _CITY_ST_RE.search(text)
        if city_m:
            return f"{city_m.group(1).strip()}, {city_m.group(2)}"

        clean = _ZIPWX_RE.sub("", text).strip()
        if 3 <= len(clean) <= 50:
            return clean

        return None
