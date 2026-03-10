"""
Tests for BlueskyPostNotifyChannel.

Unit tests mock the atproto client.
The key unit test (TestThreadRootCid) specifically guards the root_cid
bug fix: in a 3-post thread, post[2].root.cid must equal post[0].cid,
not post[1].cid.

Integration tests (marked) actually post to Bluesky using .local.env credentials.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bluesky_weather_bot.channels.notify.base import NotificationPayload
from bluesky_weather_bot.channels.notify.bluesky_post import BlueskyPostNotifyChannel


def _make_post_ref(uri: str, cid: str) -> MagicMock:
    ref = MagicMock()
    ref.uri = uri
    ref.cid = cid
    return ref


@pytest.fixture
def mock_client():
    """atproto Client mock whose send_post returns sequential post refs."""
    client = MagicMock()
    counter = {"n": 0}

    def send_post(**kwargs):
        i = counter["n"]
        counter["n"] += 1
        return _make_post_ref(f"at://did:plc:test/app.bsky.feed.post/post{i}", f"cid{i}")

    client.send_post.side_effect = send_post
    return client


@pytest.fixture
def channel(mock_client):
    with patch("atproto.Client") as MockClient:
        MockClient.return_value = mock_client
        ch = BlueskyPostNotifyChannel("testbot.bsky.social", "test-pass")
    return ch, mock_client


# ---------------------------------------------------------------------------
# Basic send behaviour
# ---------------------------------------------------------------------------

class TestSend:
    def test_single_post_standalone(self, channel):
        ch, client = channel
        payload = NotificationPayload(request_db_id=1, post_thread=["Hello world"])
        result = ch.send(payload)

        assert result.success
        client.send_post.assert_called_once_with(text="Hello world", reply_to=None)

    def test_returns_root_uri(self, channel):
        ch, client = channel
        payload = NotificationPayload(request_db_id=1, post_thread=["Post"])
        result = ch.send(payload)

        assert result.post_uri == "at://did:plc:test/app.bsky.feed.post/post0"

    def test_empty_thread_returns_failure(self, channel):
        ch, _ = channel
        result = ch.send(NotificationPayload(request_db_id=1, post_thread=[]))
        assert not result.success
        assert result.error

    def test_api_error_returns_failure(self, channel):
        ch, client = channel
        client.send_post.side_effect = Exception("rate limited")
        result = ch.send(NotificationPayload(request_db_id=1, post_thread=["Post"]))
        assert not result.success
        assert "rate limited" in result.error

    def test_three_posts_all_sent(self, channel):
        ch, client = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Post 0", "Post 1", "Post 2"],
        )
        result = ch.send(payload)
        assert result.success
        assert client.send_post.call_count == 3


# ---------------------------------------------------------------------------
# Reply threading — the root_cid bug fix
# ---------------------------------------------------------------------------

class TestThreadRootCid:
    """
    Regression test for the root_cid tracking bug.

    Before the fix, post[2].root.cid was set to post[1].cid (via the
    `cid=root_cid or reply_cid or ""` fallback with root_cid=None).
    After the fix, root_cid is tracked separately and stays pinned to post[0].
    """

    def test_post1_root_is_post0(self, channel):
        ch, client = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Post 0", "Post 1", "Post 2"],
        )
        ch.send(payload)

        reply_to1 = client.send_post.call_args_list[1].kwargs["reply_to"]
        assert reply_to1.root.uri == "at://did:plc:test/app.bsky.feed.post/post0"
        assert reply_to1.root.cid == "cid0"
        assert reply_to1.parent.uri == "at://did:plc:test/app.bsky.feed.post/post0"

    def test_post2_root_is_still_post0_not_post1(self, channel):
        ch, client = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Post 0", "Post 1", "Post 2"],
        )
        ch.send(payload)

        reply_to2 = client.send_post.call_args_list[2].kwargs["reply_to"]
        # root must be post0, parent must be post1
        assert reply_to2.root.uri == "at://did:plc:test/app.bsky.feed.post/post0"
        assert reply_to2.root.cid == "cid0"   # ← was "cid1" before the fix
        assert reply_to2.parent.uri == "at://did:plc:test/app.bsky.feed.post/post1"
        assert reply_to2.parent.cid == "cid1"

    def test_post0_has_no_reply_ref(self, channel):
        ch, client = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Post 0", "Post 1"],
        )
        ch.send(payload)

        reply_to0 = client.send_post.call_args_list[0].kwargs["reply_to"]
        assert reply_to0 is None


# ---------------------------------------------------------------------------
# Replying to an existing post (firehose request)
# ---------------------------------------------------------------------------

class TestReplyToExistingPost:
    def test_post0_replies_to_original(self, channel):
        ch, client = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Weather reply"],
            reply_to_uri="at://did:plc:orig/app.bsky.feed.post/orig123",
            reply_to_cid="origcid",
        )
        ch.send(payload)

        reply_to = client.send_post.call_args_list[0].kwargs["reply_to"]
        assert reply_to.parent.uri == "at://did:plc:orig/app.bsky.feed.post/orig123"
        assert reply_to.parent.cid == "origcid"
        assert reply_to.root.uri == "at://did:plc:orig/app.bsky.feed.post/orig123"

    def test_thread_root_stays_original_post(self, channel):
        """In a reply thread, root should be the original post, not the bot's first post."""
        ch, client = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Post 0", "Post 1", "Post 2"],
            reply_to_uri="at://did:plc:orig/app.bsky.feed.post/orig123",
            reply_to_cid="origcid",
        )
        ch.send(payload)

        reply_to2 = client.send_post.call_args_list[2].kwargs["reply_to"]
        # Root must still be the original request post
        assert reply_to2.root.uri == "at://did:plc:orig/app.bsky.feed.post/orig123"
        assert reply_to2.root.cid == "origcid"
        # Parent is bot's post[1]
        assert reply_to2.parent.uri == "at://did:plc:test/app.bsky.feed.post/post1"


# ---------------------------------------------------------------------------
# Integration tests — hit real Bluesky API
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBlueskyPostIntegration:
    def test_send_single_post(self, settings):
        ch = BlueskyPostNotifyChannel(settings.bluesky_handle, settings.bluesky_app_password)
        payload = NotificationPayload(
            request_db_id=None,
            post_thread=["🤖 ZipWx integration test — single post. (automated test, safe to ignore)"],
        )
        result = ch.send(payload)
        assert result.success, result.error
        assert result.post_uri

    def test_send_three_post_thread(self, settings):
        ch = BlueskyPostNotifyChannel(settings.bluesky_handle, settings.bluesky_app_password)
        payload = NotificationPayload(
            request_db_id=None,
            post_thread=[
                "🤖 ZipWx integration test — post 1/3. (automated test, safe to ignore)",
                "🤖 ZipWx integration test — post 2/3.",
                "🤖 ZipWx integration test — post 3/3.",
            ],
        )
        result = ch.send(payload)
        assert result.success, result.error
        assert result.post_uri
