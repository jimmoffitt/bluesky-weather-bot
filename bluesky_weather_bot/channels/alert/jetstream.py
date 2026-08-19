"""
JetstreamAlertChannel

Connects to Bluesky's Jetstream service (https://bsky.network/docs/jetstream)
instead of the raw AT Protocol firehose (com.atproto.sync.subscribeRepos).

Jetstream re-streams the network as plain JSON over a WebSocket, with
server-side filtering by collection NSID. Requesting only
`app.bsky.feed.post` means this channel never decodes CAR/CBOR blocks at
all (unlike FirehoseAlertChannel, which must CAR-decode every commit on the
network before it can even check the record type) — the server does that
filtering upstream. Built as a side-by-side comparison to
FirehoseAlertChannel so the two can be measured against each other (CPU,
thermal) on the Pi before deciding whether to switch.

Trigger detection and location extraction are shared with
FirehoseAlertChannel — see mention_parsing.py for the parsing rules and
pattern examples.

Dependencies: pip install websockets
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional

from bluesky_weather_bot.channels.alert.base import (
    AlertChannel, AlertRequest, ThreadCPUSampler, extract_directives,
)
from bluesky_weather_bot.channels.alert.firehose import FirehoseAlertChannel
from bluesky_weather_bot.channels.alert.mention_parsing import (
    extract_location, is_mention_trigger,
)
from bluesky_weather_bot.config.settings import Settings

logger = logging.getLogger(__name__)


class JetstreamAlertChannel(AlertChannel):
    """
    Streams Bluesky's Jetstream service (filtered to app.bsky.feed.post) and
    dispatches AlertRequests for posts that mention the bot.
    """

    CHANNEL_NAME = "jetstream"

    # Public instances documented at https://bsky.network/docs/jetstream —
    # us-east is primary, us-west is the fallback if a connection can't be
    # established at all (not used for mid-stream reconnects; those retry
    # the same host).
    _ENDPOINTS = (
        "wss://jetstream1.us-east.bsky.network/subscribe",
        "wss://jetstream1.us-west.bsky.network/subscribe",
    )
    _WANTED_COLLECTIONS = ("app.bsky.feed.post",)

    # Same idle-watchdog rationale as FirehoseAlertChannel: a wedged
    # connection can keep receiving bytes without on_message-equivalent
    # logic ever firing, so absence of traffic this long forces a reconnect.
    _WATCHDOG_IDLE_SEC = 45.0
    _WATCHDOG_CHECK_INTERVAL_SEC = 15.0

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._bot_handle = settings.bluesky_handle.lower().lstrip("@")
        self._bot_did: Optional[str] = FirehoseAlertChannel._resolve_bot_did(self._bot_handle)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_uris: set[str] = set()
        self._seen_lock = threading.Lock()
        self._last_message_at = time.monotonic()
        # In-memory only — resets to "live tail" on process restart. Unlike
        # FirehoseAlertChannel's reconnects (which always resume from "now"
        # and silently miss whatever arrived during the gap), a forced
        # reconnect *within* this process's lifetime resumes from here
        # instead, so a watchdog-triggered reconnect doesn't drop traffic.
        self._cursor: Optional[int] = None
        self._active_ws = None
        # Built in _run() itself, not here — see ThreadCPUSampler's docstring.
        self._cpu_sampler: Optional[ThreadCPUSampler] = None

    # ------------------------------------------------------------------
    # AlertChannel interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawns the listener thread and returns immediately (non-blocking)."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="JetstreamListener",
            daemon=True,
        )
        self._thread.start()
        logger.info("[jetstream] Started — watching for @%s mentions", self._bot_handle)

    def stop(self) -> None:
        """Signals the listener thread to exit and waits up to 5s for it to finish."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("[jetstream] Stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """
        Listener thread entry point. Lazily installs `websockets` if
        missing, then hands off to the asyncio event loop that owns the
        connection for the rest of this thread's life.
        """
        try:
            import websockets
        except ImportError:
            import subprocess, sys
            logger.warning("[jetstream] websockets not found — installing...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"],
                                  stdout=subprocess.DEVNULL)
            logger.info("[jetstream] websockets installed — retrying import")
            import websockets

        # Constructed here (not __init__) so time.thread_time() in
        # _handle_message is scoped to this listener thread, not whichever
        # thread built the channel — see ThreadCPUSampler's docstring.
        self._cpu_sampler = ThreadCPUSampler()
        asyncio.run(self._async_main(websockets))

    def _build_url(self) -> str:
        """Builds the subscribe URL for the primary endpoint, including the
        collection filter and, if we have one, a cursor to resume from."""
        endpoint = self._ENDPOINTS[0]
        params = [f"wantedCollections={c}" for c in self._WANTED_COLLECTIONS]
        if self._cursor is not None:
            params.append(f"cursor={self._cursor}")
        return f"{endpoint}?{'&'.join(params)}"

    async def _async_main(self, websockets) -> None:
        """
        Owns the connect/consume/reconnect loop for this channel's whole
        lifetime, plus the watchdog task running alongside it. Reconnects
        (with a 5s backoff) on any connection error or watchdog-forced
        close; exits only when stop() has been called.
        """
        watchdog_task = asyncio.create_task(self._watchdog(websockets))
        try:
            while not self._stop_event.is_set():
                self._last_message_at = time.monotonic()
                url = self._build_url()
                try:
                    async with websockets.connect(url, open_timeout=10) as ws:
                        self._active_ws = ws
                        async for raw in ws:
                            self._last_message_at = time.monotonic()
                            if self._stop_event.is_set():
                                break
                            self._handle_message(raw)
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    logger.warning("[jetstream] Connection error: %s — reconnecting", exc)
                finally:
                    self._active_ws = None
                if self._stop_event.is_set():
                    break
                self._stop_event.wait(timeout=5)
        finally:
            watchdog_task.cancel()

    async def _watchdog(self, websockets) -> None:
        """
        Mirrors FirehoseAlertChannel's watchdog: force a reconnect on the
        first stale detection (closing the active socket so the consuming
        loop above raises and re-enters the reconnect branch); escalate to
        killing the process if traffic still hasn't resumed by the next
        check, since a full restart is the one recovery path known to
        actually clear a wedged connection (see FirehoseAlertChannel).
        """
        consecutive_stale = 0
        while not self._stop_event.is_set():
            await asyncio.sleep(self._WATCHDOG_CHECK_INTERVAL_SEC)
            idle = time.monotonic() - self._last_message_at
            if idle <= self._WATCHDOG_IDLE_SEC:
                consecutive_stale = 0
                continue

            consecutive_stale += 1
            if consecutive_stale >= 2:
                logger.error(
                    "[jetstream] Still no messages %.0fs after a forced "
                    "reconnect — restarting the process.", idle,
                )
                os._exit(1)

            logger.warning("[jetstream] No messages for %.0fs — forcing reconnect", idle)
            self._last_message_at = time.monotonic()
            ws = self._active_ws
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    def _handle_message(self, raw: str) -> None:
        """
        Per-message entry point, called synchronously for every event
        Jetstream sends (already filtered server-side to app.bsky.feed.post
        creates — see _WANTED_COLLECTIONS). Parses the JSON envelope,
        updates the resume cursor, filters down to bot-mentioning posts,
        and dispatches an AlertRequest for each new one (deduped against
        _seen_uris so a reconnect replay can't double-process).
        """
        self._cpu_sampler.sample("jetstream")
        try:
            evt = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.debug("[jetstream] Skipping malformed message: %s", exc)
            return

        cursor = evt.get("cursor") or evt.get("time_us")
        if cursor is not None:
            self._cursor = cursor

        if evt.get("kind") != "commit":
            return
        commit = evt.get("commit") or {}
        if commit.get("operation") != "create":
            return
        if commit.get("collection") != "app.bsky.feed.post":
            return

        did = evt.get("did")
        if self._bot_did and did == self._bot_did:
            return  # skip posts authored by the bot itself

        record = commit.get("record") or {}
        text = record.get("text") or ""
        if not self._is_trigger(text):
            return

        request = self._build_request(text=text, did=did, commit=commit, record=record)
        if request is None:
            return

        if request.reply_to_uri:
            with self._seen_lock:
                if request.reply_to_uri in self._seen_uris:
                    logger.debug("[jetstream] Dedup: skipping %s", request.reply_to_uri)
                    return
                self._seen_uris.add(request.reply_to_uri)
                if len(self._seen_uris) > 10_000:
                    keep = set(list(self._seen_uris)[5_000:])
                    self._seen_uris.clear()
                    self._seen_uris.update(keep)
        self._dispatch(request)

    def _is_trigger(self, text: str) -> bool:
        """Returns True if the post text @mentions this bot, anywhere in it."""
        return is_mention_trigger(text, self._bot_handle)

    def _build_request(self, text: str, did: str, commit: dict, record: dict) -> Optional[AlertRequest]:
        """Builds the AlertRequest for a confirmed trigger post. did is the
        author's DID (Jetstream's equivalent of the firehose's repo field)."""
        location_text, directives = extract_directives(text)
        raw_location = self._extract_location(location_text)

        rkey = commit.get("rkey")
        reply_uri = f"at://{did}/app.bsky.feed.post/{rkey}" if rkey else None
        reply_cid = commit.get("cid")

        if raw_location is None:
            logger.debug("[jetstream] Trigger from %s but no location found: %r", did, text[:80])

        return AlertRequest(
            source_channel="jetstream",
            requester_handle=did,
            requester_did=did,
            raw_location=raw_location,
            raw_content=text,
            directives=directives,
            reply_to_uri=reply_uri,
            reply_to_cid=reply_cid,
            source_created_at=record.get("createdAt"),
        )

    @staticmethod
    def _extract_location(text: str) -> Optional[str]:
        """Extracts a location from anywhere in the message text — see
        mention_parsing.extract_location for the priority order."""
        return extract_location(text)
