"""
FirehoseAlertChannel

Connects to the Bluesky AT Protocol firehose (com.atproto.sync.subscribeRepos)
and streams all public posts in real time. Posts that match the configured
trigger patterns are parsed for a location and dispatched as AlertRequests.

Trigger detection and location extraction are shared with
JetstreamAlertChannel — see mention_parsing.py for the parsing rules and
pattern examples.

Dependencies: pip install atproto
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from bluesky_weather_bot.channels.alert.base import (
    AlertChannel, AlertRequest, ThreadCPUSampler, extract_directives,
)
from bluesky_weather_bot.channels.alert.mention_parsing import (
    extract_location, is_mention_trigger,
)
from bluesky_weather_bot.config.settings import Settings

logger = logging.getLogger(__name__)


class FirehoseAlertChannel(AlertChannel):
    """
    Streams the Bluesky public firehose and dispatches AlertRequests
    for posts that mention the bot or use #ZipWx with a location.
    """

    CHANNEL_NAME = "firehose"

    # The public firehose is extremely high-volume — under normal operation
    # on_message fires many times a second. If a connection has gone this
    # long without a single message, the underlying client has silently
    # wedged (observed after repeated 'ConsumerTooSlow' disconnects) rather
    # than there simply being no traffic, so the watchdog force-reconnects.
    _WATCHDOG_IDLE_SEC = 45.0
    _WATCHDOG_CHECK_INTERVAL_SEC = 15.0

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._bot_handle = settings.bluesky_handle.lower().lstrip("@")
        self._bot_did: Optional[str] = self._resolve_bot_did(self._bot_handle)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_uris: set[str] = set()
        self._seen_lock = threading.Lock()
        self._last_message_at = time.monotonic()

    # ------------------------------------------------------------------
    # AlertChannel interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawns the listener thread and returns immediately (non-blocking)."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="FirehoseListener",
            daemon=True,
        )
        self._thread.start()
        logger.info("[firehose] Started — watching for @%s mentions", self._bot_handle)

    def stop(self) -> None:
        """Signals the listener thread to exit and waits up to 5s for it to finish."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("[firehose] Stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """
        Main firehose loop. Uses atproto's FirehoseSubscribeReposClient.
        Reconnects automatically on transient errors.
        """
        try:
            from atproto import FirehoseSubscribeReposClient, parse_subscribe_repos_message, CAR, models as atproto_models
            from atproto_firehose.models import MessageFrame
        except ImportError:
            import subprocess, sys
            logger.warning("[firehose] atproto not found — installing...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "atproto"],
                                  stdout=subprocess.DEVNULL)
            logger.info("[firehose] atproto installed — retrying import")
            from atproto import FirehoseSubscribeReposClient, parse_subscribe_repos_message, CAR, models as atproto_models
            from atproto_firehose.models import MessageFrame

        # A fresh client is created on every (re)connect attempt below rather
        # than reusing one instance for the thread's lifetime — observed live:
        # after a 'ConsumerTooSlow' disconnect, calling .stop()/.start() again
        # on the *same* client left dead sockets behind in CLOSE-WAIT (visible
        # via `ss`) instead of cleanly reconnecting. client.stop() is still
        # used to release whichever client is currently active.
        client: Optional["FirehoseSubscribeReposClient"] = None
        # Constructed here (not __init__) so time.thread_time() below is
        # scoped to this listener thread, not whichever thread built the
        # channel — see ThreadCPUSampler's docstring.
        cpu_sampler = ThreadCPUSampler()

        def on_message(message: MessageFrame) -> None:
            """
            Per-commit callback, invoked synchronously by the atproto client
            for every commit on the *entire* public network — filtering
            down to bot-mentioning app.bsky.feed.post creates happens here,
            which is the CAR/CBOR decode cost JetstreamAlertChannel avoids
            (see mention_parsing.py and the README's backend comparison).
            """
            self._last_message_at = time.monotonic()
            cpu_sampler.sample("firehose")
            if self._stop_event.is_set():
                if client is not None:
                    client.stop()
                return
            try:
                commit = parse_subscribe_repos_message(message)
                if not isinstance(commit, atproto_models.ComAtprotoSyncSubscribeRepos.Commit):
                    return
                if not commit.blocks:
                    return
                car = CAR.from_bytes(commit.blocks)
                # Skip posts authored by the bot itself to avoid feedback loops
                if self._bot_did and commit.repo == self._bot_did:
                    return

                for op in commit.ops:
                    if op.action != "create":
                        continue
                    if not op.path.startswith("app.bsky.feed.post"):
                        continue
                    record_raw = car.blocks.get(op.cid)
                    if not record_raw:
                        continue
                    record = atproto_models.get_or_create(record_raw, strict=False)
                    if not isinstance(record, atproto_models.AppBskyFeedPost.Record):
                        continue
                    text = record.text or ""
                    if self._is_trigger(text):
                        request = self._build_request(
                            text=text,
                            repo=commit.repo,
                            op=op,
                            created_at=record.created_at,
                        )
                        if request:
                            if request.reply_to_uri:
                                with self._seen_lock:
                                    if request.reply_to_uri in self._seen_uris:
                                        logger.debug("[firehose] Dedup: skipping %s", request.reply_to_uri)
                                        continue
                                    self._seen_uris.add(request.reply_to_uri)
                                    # Trim oldest half when the set gets large,
                                    # preserving recent entries rather than clearing all.
                                    if len(self._seen_uris) > 10_000:
                                        keep = set(list(self._seen_uris)[5_000:])
                                        self._seen_uris.clear()
                                        self._seen_uris.update(keep)
                            self._dispatch(request)
            except Exception as exc:
                logger.debug("[firehose] Skipping malformed message: %s", exc)

        def on_error(error: Exception) -> None:
            """Connection-level error callback from the atproto client — the
            outer while loop (below) does the actual reconnect."""
            if self._stop_event.is_set():
                return
            logger.warning("[firehose] Connection error: %s — will reconnect", error)

        def watchdog() -> None:
            """
            client.start() can wedge without raising or invoking on_error —
            observed live even after recreating the client on reconnect: the
            new socket reaches ESTABLISHED and keeps receiving bytes (visible
            in `ss` as a growing Recv-Q) but on_message never fires, so
            in-process recovery can't be trusted to actually work. Force a
            reconnect on the first stale detection; if traffic still hasn't
            resumed by the next check, escalate to killing the whole process
            so systemd's Restart=on-failure brings it back fully clean —
            every full restart so far has fixed it immediately, so that's
            the one recovery path known to actually work.
            """
            consecutive_stale = 0
            while not self._stop_event.wait(timeout=self._WATCHDOG_CHECK_INTERVAL_SEC):
                idle = time.monotonic() - self._last_message_at
                if idle <= self._WATCHDOG_IDLE_SEC:
                    consecutive_stale = 0
                    continue

                consecutive_stale += 1
                if consecutive_stale >= 2:
                    logger.error(
                        "[firehose] Still no messages %.0fs after a forced "
                        "reconnect — restarting the process.", idle,
                    )
                    os._exit(1)

                logger.warning(
                    "[firehose] No messages for %.0fs — forcing reconnect", idle
                )
                self._last_message_at = time.monotonic()
                try:
                    client.stop()
                except Exception:
                    pass

        threading.Thread(target=watchdog, name="FirehoseWatchdog", daemon=True).start()

        while not self._stop_event.is_set():
            self._last_message_at = time.monotonic()
            try:
                client = FirehoseSubscribeReposClient()
                client.start(on_message, on_error)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("[firehose] Reconnecting after error: %s", exc)
            else:
                if self._stop_event.is_set():
                    break
                logger.warning("[firehose] Connection ended — reconnecting")
            finally:
                if client is not None:
                    try:
                        client.stop()
                    except Exception:
                        pass
            self._stop_event.wait(timeout=5)

    @staticmethod
    def _resolve_bot_did(handle: str) -> Optional[str]:
        """Resolve handle → DID once at startup so we can filter own posts."""
        import urllib.request as _ur
        import urllib.parse as _up
        import json as _json
        url = (
            "https://bsky.social/xrpc/com.atproto.identity.resolveHandle"
            f"?handle={_up.quote(handle)}"
        )
        try:
            with _ur.urlopen(url, timeout=10) as resp:
                did = _json.loads(resp.read()).get("did")
                logger.info("[firehose] Bot DID resolved: %s", did)
                return did
        except Exception as exc:
            logger.warning("[firehose] Could not resolve bot DID: %s", exc)
            return None

    def _is_trigger(self, text: str) -> bool:
        """
        Returns True if the post @mentions the bot handle.

        A plain mention (no location) is still dispatched — bot.py will apply
        the user's saved home location, or silently drop if none is set.
        """
        return is_mention_trigger(text, self._bot_handle)

    def _build_request(self, text: str, repo: str, op, created_at: Optional[str] = None) -> Optional[AlertRequest]:
        """
        Extracts location from post text and builds an AlertRequest.
        Returns None if no location can be found (informational posts, etc.).
        """
        location_text, directives = extract_directives(text)
        raw_location = self._extract_location(location_text)

        # Build the AT-URI for the reply target
        rkey = getattr(op, "path", "").split("/")[-1] if hasattr(op, "path") else ""
        reply_uri = f"at://{repo}/app.bsky.feed.post/{rkey}" if rkey else None
        reply_cid = str(op.cid) if hasattr(op, "cid") and op.cid else None

        if raw_location is None:
            logger.debug("[firehose] Trigger from @%s but no location found: %r",
                         repo, text[:80])

        return AlertRequest(
            source_channel="firehose",
            requester_handle=repo,
            requester_did=repo,   # repo in AT Protocol firehose is the user's DID
            raw_location=raw_location,
            raw_content=text,
            directives=directives,
            reply_to_uri=reply_uri,
            reply_to_cid=reply_cid,
            source_created_at=created_at,
        )

    @staticmethod
    def _extract_location(text: str) -> Optional[str]:
        """Extracts a location from anywhere in the message text — see
        mention_parsing.extract_location for the priority order."""
        return extract_location(text)
