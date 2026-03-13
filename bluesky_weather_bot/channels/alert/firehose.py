"""
FirehoseAlertChannel

Connects to the Bluesky AT Protocol firehose (com.atproto.sync.subscribeRepos)
and streams all public posts in real time. Posts that match the configured
trigger patterns (hashtags and/or mention of the bot handle) are parsed
for a location and dispatched as AlertRequests.

Trigger detection (configurable):
  - Hashtag:  post contains #ZipWx (case-insensitive)
  - Mention:  post mentions @<bot_handle>
  - Both:     post contains either trigger (default)

Location extraction: scans post text for a zip code or "City, ST" pattern
following the trigger tag/mention.

Pattern examples the parser handles:
  "#ZipWx 80501"
  "#ZipWx Denver, CO"
  "#ZipWx Portland"           ← ambiguous; resolver will return both
  "@zipwx.bsky.social 94102"

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

# Regex to find a location token after the trigger word/mention.
# Captures: 5-digit zip, or "City, ST", or a bare city name as fallback.
_LOCATION_RE = re.compile(
    r"(?:#[Zz]ip[Ww]x|@\S+)"   # trigger: hashtag or mention
    r"\s+"                       # whitespace separator
    r"([A-Za-z0-9][^#@\n]{2,40}?)(?:\s*#|\s*@|$)",  # location token
    re.IGNORECASE,
)

_ZIP_RE     = re.compile(r"\b(\d{5})\b")
_CITY_ST_RE = re.compile(r"\b([A-Z][a-zA-Z\s]{2,20}),\s*([A-Z]{2})\b")


class FirehoseAlertChannel(AlertChannel):
    """
    Streams the Bluesky public firehose and dispatches AlertRequests
    for posts that mention the bot or use #ZipWx with a location.
    """

    CHANNEL_NAME = "firehose"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._bot_handle = settings.bluesky_handle.lower().lstrip("@")
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_uris: set[str] = set()
        self._seen_lock = threading.Lock()

    # ------------------------------------------------------------------
    # AlertChannel interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="FirehoseListener",
            daemon=True,
        )
        self._thread.start()
        logger.info("[firehose] Started — watching for #ZipWx and @%s", self._bot_handle)

    def stop(self) -> None:
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

        client = FirehoseSubscribeReposClient()

        def on_message(message: MessageFrame) -> None:
            if self._stop_event.is_set():
                client.stop()
                return
            try:
                commit = parse_subscribe_repos_message(message)
                if not isinstance(commit, atproto_models.ComAtprotoSyncSubscribeRepos.Commit):
                    return
                if not commit.blocks:
                    return
                car = CAR.from_bytes(commit.blocks)
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
                        )
                        if request:
                            if request.reply_to_uri:
                                with self._seen_lock:
                                    if request.reply_to_uri in self._seen_uris:
                                        logger.debug("[firehose] Dedup: skipping %s", request.reply_to_uri)
                                        continue
                                    self._seen_uris.add(request.reply_to_uri)
                                    if len(self._seen_uris) > 10_000:
                                        self._seen_uris.clear()
                            self._dispatch(request)
            except Exception as exc:
                logger.debug("[firehose] Skipping malformed message: %s", exc)

        def on_error(error: Exception) -> None:
            if self._stop_event.is_set():
                return
            logger.warning("[firehose] Connection error: %s — will reconnect", error)

        while not self._stop_event.is_set():
            try:
                client.start(on_message, on_error)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("[firehose] Reconnecting after error: %s", exc)
                self._stop_event.wait(timeout=5)

    def _is_trigger(self, text: str) -> bool:
        """Returns True if the post text contains a valid bot trigger."""
        lower = text.lower()
        return "#zipwx" in lower or f"@{self._bot_handle}" in lower

    def _build_request(self, text: str, repo: str, op) -> Optional[AlertRequest]:
        """
        Extracts location from post text and builds an AlertRequest.
        Returns None if no location can be found (informational posts, etc.).
        """
        raw_location = self._extract_location(text)

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
            raw_location=raw_location,
            raw_content=text,
            reply_to_uri=reply_uri,
            reply_to_cid=reply_cid,
        )

    @staticmethod
    def _extract_location(text: str) -> Optional[str]:
        """
        Extracts location from text following a #ZipWx trigger or mention.

        Priority:
          1. Text immediately after trigger tag/mention
          2. Zip code anywhere in text
          3. City, ST pattern anywhere in text
        """
        # Try location immediately after trigger
        m = _LOCATION_RE.search(text)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate

        # Zip code fallback
        zip_m = _ZIP_RE.search(text)
        if zip_m:
            return zip_m.group(1)

        # City, ST fallback
        city_m = _CITY_ST_RE.search(text)
        if city_m:
            return f"{city_m.group(1).strip()}, {city_m.group(2)}"

        return None
