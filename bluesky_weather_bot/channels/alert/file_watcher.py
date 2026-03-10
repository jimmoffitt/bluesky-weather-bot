"""
FileWatcherAlertChannel

Polls a configured inbox directory on a fixed interval (default 5 seconds).
When a .yaml or .yml file appears, it is parsed into an AlertRequest and
dispatched to the registered handler. Successfully processed files are moved
to the archive directory; files that fail parsing are moved to errors/.

Expected YAML schema:
    full_message: 'Red Rocks Park 30-day rain total: 0.71 inches #RainData #COWx'
    message: 'Red Rocks Park 30-day rain total: 0.71 inches'
    created_at: '2025-02-19T17:30:07+07:00'
    host: Test
    tags:
      - COWx
      - Rain
    mentions:

Location extraction: the bot looks for a zip code or "City, ST" pattern in
full_message or message. If none is found, raw_location is None and the
orchestrator will skip the weather lookup (pure notification pass-through).

Dependencies: pip install PyYAML
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from bluesky_weather_bot.channels.alert.base import AlertChannel, AlertRequest
from bluesky_weather_bot.config.settings import Settings

logger = logging.getLogger(__name__)

# Patterns used to extract a location from message text
_ZIP_RE    = re.compile(r"\b(\d{5})\b")
_CITY_ST_RE = re.compile(
    r"\b([A-Z][a-zA-Z\s]{2,20}),\s*([A-Z]{2})\b"
)


class FileWatcherAlertChannel(AlertChannel):
    """
    Watches a directory for incoming YAML alert files and dispatches
    an AlertRequest for each one.

    Thread safety: runs in its own daemon thread; stop() sets a flag
    that causes the polling loop to exit cleanly.
    """

    CHANNEL_NAME = "file"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._inbox   = Path(settings.inbox_path)
        self._archive = Path(settings.inbox_archive_path)
        self._errors  = Path(settings.inbox_error_path)
        self._interval = settings.inbox_poll_interval_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # AlertChannel interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the polling loop in a background daemon thread."""
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._archive.mkdir(parents=True, exist_ok=True)
        self._errors.mkdir(parents=True, exist_ok=True)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="FileWatcher",
            daemon=True,
        )
        self._thread.start()
        logger.info("[file] Watching %s every %.1fs", self._inbox, self._interval)

    def stop(self) -> None:
        """Signal the polling loop to stop and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval + 2)
        logger.info("[file] Stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._process_inbox()
            except Exception as exc:
                logger.exception("[file] Unexpected error in poll loop: %s", exc)
            self._stop_event.wait(timeout=self._interval)

    def _process_inbox(self) -> None:
        candidates = sorted(
            p for p in self._inbox.iterdir()
            if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
        )
        for path in candidates:
            self._handle_file(path)

    def _handle_file(self, path: Path) -> None:
        logger.info("[file] Processing %s", path.name)
        try:
            request = self._parse_file(path)
        except Exception as exc:
            logger.error("[file] Parse error in %s: %s", path.name, exc)
            self._move(path, self._errors)
            return

        # Dispatch to orchestrator
        self._dispatch(request)

        # Move to archive after successful dispatch
        self._move(path, self._archive)

    def _parse_file(self, path: Path) -> AlertRequest:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Expected a YAML mapping, got {type(data).__name__}")

        full_message = str(data.get("full_message") or data.get("message") or "")
        message      = str(data.get("message") or full_message)

        # Try to extract a location from the message text
        raw_location = self._extract_location(full_message) or self._extract_location(message)

        # Parse created_at if present
        created_at_raw = data.get("created_at")
        try:
            received_at = datetime.fromisoformat(str(created_at_raw)) if created_at_raw else datetime.utcnow()
        except (ValueError, TypeError):
            received_at = datetime.utcnow()

        return AlertRequest(
            source_channel="file",
            requester_handle=None,
            raw_location=raw_location,
            raw_content=full_message,
            received_at=received_at,
            source_file=path.name,
        )

    @staticmethod
    def _extract_location(text: str) -> Optional[str]:
        """
        Attempts to find a zip code or 'City, ST' pattern in free text.
        Returns the first match found, or None.
        """
        zip_match = _ZIP_RE.search(text)
        if zip_match:
            return zip_match.group(1)
        city_match = _CITY_ST_RE.search(text)
        if city_match:
            return f"{city_match.group(1).strip()}, {city_match.group(2)}"
        return None

    @staticmethod
    def _move(src: Path, dest_dir: Path) -> None:
        """Move a file into dest_dir, appending a timestamp if name collides."""
        dest = dest_dir / src.name
        if dest.exists():
            ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
            dest = dest_dir / f"{src.stem}_{ts}{src.suffix}"
        shutil.move(str(src), str(dest))
        logger.debug("[file] Moved %s → %s", src.name, dest)
