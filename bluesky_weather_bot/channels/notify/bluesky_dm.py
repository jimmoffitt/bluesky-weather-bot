"""
BlueskyDMNotifyChannel

Sends a weather response as a Bluesky Direct Message.

For DM responses the full thread is concatenated into a single message
(DMs don't support threads). If the joined text exceeds Bluesky's DM
character limit, it is split into sequential messages in the same convo.

The convo_id is passed in payload.reply_to_uri (set by DMAlertChannel).
If no convo_id is provided, a new conversation is started with the recipient.

Dependencies: pip install atproto
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from bluesky_weather_bot.channels.notify.base import (
    NotificationChannel,
    NotificationPayload,
    NotificationResult,
)

logger = logging.getLogger(__name__)

DM_MAX_CHARS    = 1000   # Bluesky DM character limit (approximate)
INTER_MSG_DELAY = 0.3    # seconds between sequential messages


class BlueskyDMNotifyChannel(NotificationChannel):
    """
    Sends formatted weather reports as Bluesky Direct Messages.

    payload.reply_to_uri should contain the convo_id from the originating DM.
    payload.recipient_handle is used to start a new convo if no convo_id.
    """

    CHANNEL_NAME = "bluesky_dm"

    def __init__(self, handle: str, app_password: str) -> None:
        self._handle       = handle
        self._app_password = app_password
        self._client       = None
        self._chat_client  = None
        self._login()

    # ------------------------------------------------------------------
    # NotificationChannel interface
    # ------------------------------------------------------------------

    def send(self, payload: NotificationPayload) -> NotificationResult:
        """
        Sends payload.post_thread as one or more sequential DMs (split by
        _split_message if it exceeds DM_MAX_CHARS). Resolves the target
        convo from payload.reply_to_uri, falling back to creating/finding a
        convo with payload.recipient_handle. Never raises — failures come
        back as NotificationResult(success=False, error=...).
        """
        if not payload.post_thread:
            return NotificationResult(
                success=False, channel=self.CHANNEL_NAME,
                error="Empty post_thread",
            )

        if self._chat_client is None:
            ok = self._login()
            if not ok:
                return NotificationResult(
                    success=False, channel=self.CHANNEL_NAME,
                    error="Not authenticated",
                )

        # Join thread posts into one or more DM messages
        full_text = "\n\n".join(payload.post_thread)
        messages  = self._split_message(full_text)

        # Determine convo_id
        convo_id = payload.reply_to_uri  # set by DMAlertChannel
        if not convo_id and payload.recipient_handle:
            convo_id = self._get_or_create_convo(payload.recipient_handle)
        if not convo_id:
            return NotificationResult(
                success=False, channel=self.CHANNEL_NAME,
                error="No convo_id and no recipient_handle",
            )

        sent_any = False
        for i, text in enumerate(messages):
            try:
                self._send_message(convo_id, text)
                sent_any = True
                logger.info("[bluesky_dm] Sent message %d/%d to convo %s",
                            i + 1, len(messages), convo_id)
                if i < len(messages) - 1:
                    time.sleep(INTER_MSG_DELAY)
            except Exception as exc:
                logger.error("[bluesky_dm] Failed on message %d: %s", i, exc)
                return NotificationResult(
                    success=sent_any, channel=self.CHANNEL_NAME,
                    error=str(exc),
                )

        return NotificationResult(success=True, channel=self.CHANNEL_NAME)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _login(self) -> bool:
        """Authenticates and sets up the chat-proxy client. Called once from
        __init__ and again lazily by send() if the client was never set up."""
        try:
            from atproto import Client
            self._client = Client()
            self._client.login(self._handle, self._app_password)
            self._chat_client = self._client.with_bsky_chat_proxy()
            logger.info("[bluesky_dm] Authenticated as %s", self._handle)
            return True
        except ImportError:
            logger.error("[bluesky_dm] atproto not installed: pip install atproto")
            return False
        except Exception as exc:
            logger.error("[bluesky_dm] Login failed: %s", exc)
            return False

    def _get_or_create_convo(self, handle: str) -> Optional[str]:
        """Finds an existing 1:1 convo with the handle, or creates one."""
        try:
            from atproto import models as atproto_models
            # API requires a DID, not a handle — resolve first
            resolved = self._client.resolve_handle(handle=handle)
            resp = self._chat_client.chat.bsky.convo.get_convo_for_members(
                atproto_models.ChatBskyConvoGetConvoForMembers.Params(members=[resolved.did])
            )
            return getattr(getattr(resp, "convo", None), "id", None)
        except Exception as exc:
            logger.error("[bluesky_dm] Could not get/create convo with %s: %s", handle, exc)
            return None

    def _send_message(self, convo_id: str, text: str) -> None:
        """Sends a single message to an existing convo. Raises on failure —
        the caller (send()) catches it and reports via NotificationResult."""
        if self._chat_client is None:
            raise RuntimeError("Not authenticated")
        from atproto import models as atproto_models
        self._chat_client.chat.bsky.convo.send_message(
            atproto_models.ChatBskyConvoSendMessage.Data(
                convo_id=convo_id,
                message=atproto_models.ChatBskyConvoDefs.MessageInput(text=text),
            )
        )

    @staticmethod
    def _split_message(text: str, max_len: int = DM_MAX_CHARS) -> list[str]:
        """
        Splits a long message at paragraph boundaries to stay under max_len.
        """
        if len(text) <= max_len:
            return [text]

        paragraphs = text.split("\n\n")
        chunks: list[str] = []
        current = ""
        for para in paragraphs:
            candidate = (current + "\n\n" + para).lstrip("\n") if current else para
            if len(candidate) <= max_len:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # If a single paragraph itself is too long, hard-split it
                if len(para) > max_len:
                    for i in range(0, len(para), max_len):
                        chunks.append(para[i:i + max_len])
                    current = ""
                else:
                    current = para
        if current:
            chunks.append(current)
        return chunks
