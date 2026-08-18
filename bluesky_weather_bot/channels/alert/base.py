"""
Abstract base class for all alert channels.

An AlertChannel is any input source that can trigger a weather request.
Current implementations: FileWatcherAlertChannel, FirehoseAlertChannel,
                          JetstreamAlertChannel, DMAlertChannel
Future examples:         WebhookAlertChannel, SMSAlertChannel, EmailAlertChannel

Each channel produces AlertRequest objects and delivers them to a callback
registered by the orchestrator (bot.py). The channel itself never calls the
weather service or notification channels — that is the orchestrator's job.

Usage pattern (in bot.py):
    channel = FileWatcherAlertChannel(settings)
    channel.on_request(handler_fn)
    channel.start()
    ...
    channel.stop()
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DIRECTIVE_RE = re.compile(r"(?:(?<=\s)|^)/(\w+)\b")


class ThreadCPUSampler:
    """
    Periodically logs the CPU usage of *the thread that calls it* — for
    comparing channels like FirehoseAlertChannel and JetstreamAlertChannel
    that each run their message-processing loop on one dedicated thread.

    systemd/top only give whole-process CPU, which is useless once two
    channels share a process (e.g. MENTION_BACKEND=firehose,jetstream) —
    this uses time.thread_time(), which is scoped to the calling thread,
    so each channel's number is its own regardless of what else is running
    in the process.

    Must be constructed AND sampled from the same thread — construct it as
    the first thing inside the channel's run loop (not in __init__, which
    runs on whichever thread called the constructor), then call sample()
    from inside the loop wherever messages are already being handled (no
    separate timer thread needed).
    """

    def __init__(self, interval_sec: float = 300.0) -> None:
        self._interval = interval_sec
        self._last_wall = time.monotonic()
        self._last_cpu = time.thread_time()

    def sample(self, tag: str) -> None:
        """Call frequently (e.g. once per message handled); logs at most once per interval."""
        now_wall = time.monotonic()
        elapsed_wall = now_wall - self._last_wall
        if elapsed_wall < self._interval:
            return
        now_cpu = time.thread_time()
        elapsed_cpu = now_cpu - self._last_cpu
        pct = (elapsed_cpu / elapsed_wall * 100) if elapsed_wall > 0 else 0.0
        logger.info(
            "[%s] Thread CPU: %.1f%% over last %.1fs (%.2fs of CPU time)",
            tag, pct, elapsed_wall, elapsed_cpu,
        )
        self._last_wall = now_wall
        self._last_cpu = now_cpu


def extract_directives(text: str) -> tuple[str, frozenset[str]]:
    """
    Pulls '/word' directive tokens (e.g. '/forecast', '/day') out of request
    text before location extraction runs, so they can't be swallowed into a
    location string. Returns (text_with_directives_removed, lowercased set
    of directive names found).
    """
    directives = frozenset(m.group(1).lower() for m in _DIRECTIVE_RE.finditer(text))
    stripped = _DIRECTIVE_RE.sub(" ", text)
    return stripped, directives


@dataclass
class AlertRequest:
    """
    Normalized representation of an inbound weather request,
    regardless of which channel it arrived on.
    """
    # Which channel produced this request
    source_channel: str          # "file" | "firehose" | "jetstream" | "dm"

    # Who asked (Bluesky handle for social channels; None for file alerts)
    requester_handle: Optional[str]

    # The raw location string extracted from the request
    # e.g. "Denver, CO" or "80501" or "Portland"
    raw_location: Optional[str]

    # The full original content (post text, DM text, or file contents)
    raw_content: str

    # '/word' directive tokens found in the text (e.g. {'forecast', 'day'}),
    # already stripped out of raw_location's extraction input by the channel.
    directives: frozenset[str] = field(default_factory=frozenset)

    # When the request was received by the channel
    received_at: datetime = field(default_factory=datetime.utcnow)

    # When the underlying post was created on the AT Protocol network
    # (the record's own createdAt) — set by firehose/jetstream channels.
    # None for file/DM, where there's no equivalent "creation" moment
    # distinct from receipt. This is what the receive-latency metric
    # (ingested_at - source_created_at) is measured against.
    source_created_at: Optional[str] = None

    # For firehose/DM channels: the AT-URI of the post to reply to
    reply_to_uri: Optional[str] = None
    reply_to_cid: Optional[str] = None

    # For file channel: the source filename for traceability
    source_file: Optional[str] = None

    # Bluesky DID of the requester (set by firehose/DM channels)
    requester_did: Optional[str] = None

    # Set by DMAlertChannel when the message is a preference command,
    # not a weather location request.
    # Values: "set_home" | "set_units_imperial" | "set_units_metric" |
    #         "clear_home" | "reset_prefs" | "settings" | "help"
    command: Optional[str] = None

    # Set by orchestrator after DB insert
    db_id: Optional[int] = None


# Type alias for the handler callback
AlertHandler = Callable[[AlertRequest], None]


class AlertChannel(ABC):
    """
    Abstract base for all alert input channels.

    Subclasses must implement:
      - start(): begin listening / polling
      - stop():  clean shutdown

    Subclasses call self._dispatch(request) when a new request arrives.
    """

    CHANNEL_NAME: str = "base"   # Override in each subclass

    def __init__(self) -> None:
        self._handler: Optional[AlertHandler] = None

    def on_request(self, handler: AlertHandler) -> None:
        """Register the callback that receives AlertRequest objects."""
        self._handler = handler

    def _dispatch(self, request: AlertRequest) -> None:
        """Called by the subclass when a new request is ready."""
        if self._handler is None:
            logger.warning("[%s] No handler registered; dropping request from %s",
                           self.CHANNEL_NAME, request.requester_handle)
            return
        try:
            self._handler(request)
        except Exception as exc:
            logger.exception("[%s] Handler raised an exception: %s", self.CHANNEL_NAME, exc)

    @abstractmethod
    def start(self) -> None:
        """Start listening for requests. May block (run in a thread) or be async."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Signal the channel to stop cleanly."""
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} channel={self.CHANNEL_NAME!r}>"
