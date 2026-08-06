"""
Abstract base class for all alert channels.

An AlertChannel is any input source that can trigger a weather request.
Current implementations: FileWatcherAlertChannel, FirehoseAlertChannel, DMAlertChannel
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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DIRECTIVE_RE = re.compile(r"(?:(?<=\s)|^)/(\w+)\b")


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
    source_channel: str          # "file" | "firehose" | "dm"

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
