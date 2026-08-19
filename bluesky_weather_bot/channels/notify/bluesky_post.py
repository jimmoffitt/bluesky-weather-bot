"""
BlueskyPostNotifyChannel

Sends a weather response as a public Bluesky post thread.
Index 0 of payload.post_thread is the root post (or reply to the
original request post if reply_to_uri is set). Subsequent posts
are threaded as replies to the preceding post.

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

# Delay between posts in a thread to avoid rate-limit issues
INTER_POST_DELAY = 0.5  # seconds


class BlueskyPostNotifyChannel(NotificationChannel):
    """
    Sends formatted weather reports as public Bluesky post threads.

    Each NotificationPayload.post_thread becomes a chain of posts:
      post_thread[0] → root post (replies to the original request if uri provided)
      post_thread[1] → reply to post_thread[0]
      post_thread[2] → reply to post_thread[1]
    """

    CHANNEL_NAME = "bluesky_post"

    def __init__(self, handle: str, app_password: str) -> None:
        self._handle       = handle
        self._app_password = app_password
        self._client       = None
        self._login()

    # ------------------------------------------------------------------
    # NotificationChannel interface
    # ------------------------------------------------------------------

    def send(self, payload: NotificationPayload) -> NotificationResult:
        """
        Sends payload.post_thread as a chain of posts (see class docstring
        for the threading rule), or delegates to _send_image_post if
        payload.post_images is set (image mode is always a single post, not
        a thread). Never raises — failures come back as
        NotificationResult(success=False, error=...).
        """
        if not payload.post_thread:
            return NotificationResult(
                success=False, channel=self.CHANNEL_NAME,
                error="Empty post_thread in payload",
            )

        if self._client is None:
            ok = self._login()
            if not ok:
                return NotificationResult(
                    success=False, channel=self.CHANNEL_NAME,
                    error="Not authenticated",
                )

        if payload.post_images:
            return self._send_image_post(payload)

        root_uri: Optional[str] = None
        root_cid: Optional[str] = None
        parent_uri: Optional[str] = None
        parent_cid: Optional[str] = None

        # The first post replies to the original request (if provided)
        reply_to_uri = payload.reply_to_uri
        reply_to_cid = payload.reply_to_cid

        for i, text in enumerate(payload.post_thread):
            try:
                if i == 0:
                    post_ref = self._post(
                        text=text,
                        reply_uri=reply_to_uri,
                        reply_cid=reply_to_cid,
                        root_uri=reply_to_uri,
                        root_cid=reply_to_cid,
                    )
                else:
                    post_ref = self._post(
                        text=text,
                        reply_uri=parent_uri,
                        reply_cid=parent_cid,
                        root_uri=root_uri,
                        root_cid=root_cid,
                    )

                if post_ref is None:
                    raise RuntimeError("post() returned None")

                uri = getattr(post_ref, "uri", None) or str(post_ref)
                cid = getattr(post_ref, "cid", None)

                if i == 0:
                    # If we're replying to an existing post, that post stays as root
                    # throughout the whole thread (AT Protocol convention).
                    # For standalone threads, our first post becomes the root.
                    if not reply_to_uri:
                        root_uri = uri
                        root_cid = str(cid) if cid else None
                    else:
                        root_uri = reply_to_uri
                        root_cid = reply_to_cid
                parent_uri = uri
                parent_cid = str(cid) if cid else None

                logger.info("[bluesky_post] Posted %d/%d: %s",
                            i + 1, len(payload.post_thread), uri)

                if i < len(payload.post_thread) - 1:
                    time.sleep(INTER_POST_DELAY)

            except Exception as exc:
                logger.error("[bluesky_post] Failed on post %d: %s", i, exc)
                return NotificationResult(
                    success=False, channel=self.CHANNEL_NAME,
                    post_uri=root_uri,
                    error=str(exc),
                )

        return NotificationResult(
            success=True,
            channel=self.CHANNEL_NAME,
            post_uri=root_uri,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _login(self) -> bool:
        """Authenticates and sets up the client. Called once from __init__
        and again lazily by send() if the client was never set up."""
        try:
            from atproto import Client
            self._client = Client()
            self._client.login(self._handle, self._app_password)
            logger.info("[bluesky_post] Authenticated as %s", self._handle)
            return True
        except ImportError:
            logger.error("[bluesky_post] atproto not installed: pip install atproto")
            return False
        except Exception as exc:
            logger.error("[bluesky_post] Login failed: %s", exc)
            return False

    def _post(
        self,
        text: str,
        reply_uri: Optional[str] = None,
        reply_cid: Optional[str] = None,
        root_uri: Optional[str] = None,
        root_cid: Optional[str] = None,
    ):
        """
        Sends a single post. If reply_uri is provided, sends as a reply.
        Returns the post StrongRef (has .uri and .cid attributes).
        """
        if self._client is None:
            raise RuntimeError("Not authenticated")

        reply_ref = None
        if reply_uri:
            from atproto import models as atproto_models
            root   = atproto_models.ComAtprotoRepoStrongRef.Main(
                uri=root_uri or reply_uri,
                cid=root_cid or reply_cid or "",
            )
            parent = atproto_models.ComAtprotoRepoStrongRef.Main(
                uri=reply_uri,
                cid=reply_cid or "",
            )
            reply_ref = atproto_models.AppBskyFeedPost.ReplyRef(root=root, parent=parent)

        return self._client.send_post(text=text, reply_to=reply_ref)

    def _send_image_post(self, payload: NotificationPayload) -> NotificationResult:
        """Send a single post with up to 3 embedded PNG images."""
        caption = payload.post_thread[0] if payload.post_thread else ""
        images  = payload.post_images or []
        alts    = payload.post_image_alts or [""] * len(images)

        # Build reply ref using same logic as _post()
        reply_ref = None
        if payload.reply_to_uri:
            from atproto import models as atproto_models
            root   = atproto_models.ComAtprotoRepoStrongRef.Main(
                uri=payload.reply_to_uri,
                cid=payload.reply_to_cid or "",
            )
            parent = atproto_models.ComAtprotoRepoStrongRef.Main(
                uri=payload.reply_to_uri,
                cid=payload.reply_to_cid or "",
            )
            reply_ref = atproto_models.AppBskyFeedPost.ReplyRef(root=root, parent=parent)

        try:
            result = self._client.send_images(
                text=caption,
                images=images,
                image_alts=alts,
                reply_to=reply_ref,
            )
            uri = getattr(result, "uri", None) or str(result)
            logger.info("[bluesky_post] Image post sent: %s (%d image(s))", uri, len(images))
            return NotificationResult(success=True, channel=self.CHANNEL_NAME, post_uri=uri)
        except Exception as exc:
            logger.error("[bluesky_post] Image post failed: %s", exc)
            return NotificationResult(success=False, channel=self.CHANNEL_NAME, error=str(exc))
