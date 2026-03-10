"""
Tests for BlueskyDMNotifyChannel.

Unit tests mock the atproto client and verify:
  - All chat API calls go through with_bsky_chat_proxy(), not the raw client.
  - Message splitting behaves correctly.

Integration tests (marked) actually send DMs using .local.env credentials.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bluesky_weather_bot.channels.notify.base import NotificationPayload
from bluesky_weather_bot.channels.notify.bluesky_dm import BlueskyDMNotifyChannel


@pytest.fixture
def mock_client():
    """Returns (raw_client, chat_proxy_client) pair."""
    raw = MagicMock()
    chat = MagicMock()
    raw.with_bsky_chat_proxy.return_value = chat
    return raw, chat


@pytest.fixture
def channel(mock_client):
    raw, chat = mock_client
    with patch("atproto.Client") as MockClient:
        MockClient.return_value = raw
        ch = BlueskyDMNotifyChannel("testbot.bsky.social", "test-pass")
    return ch, raw, chat


# ---------------------------------------------------------------------------
# Chat proxy usage
# ---------------------------------------------------------------------------

class TestChatProxy:
    def test_with_bsky_chat_proxy_called_on_login(self, mock_client):
        raw, _ = mock_client
        with patch("atproto.Client") as MockClient:
            MockClient.return_value = raw
            BlueskyDMNotifyChannel("testbot.bsky.social", "test-pass")
        raw.with_bsky_chat_proxy.assert_called_once()

    def test_send_message_uses_proxy_not_raw_client(self, channel):
        ch, raw, chat = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Weather update"],
            reply_to_uri="convo123",
        )
        ch.send(payload)

        raw.chat.bsky.convo.send_message.assert_not_called()
        chat.chat.bsky.convo.send_message.assert_called_once()

    def test_get_convo_uses_proxy_not_raw_client(self, channel):
        ch, raw, chat = channel
        raw.resolve_handle.return_value = MagicMock(did="did:plc:someone")
        chat.chat.bsky.convo.get_convo_for_members.return_value = MagicMock(
            convo=MagicMock(id="convo456")
        )
        ch._get_or_create_convo("someone.bsky.social")

        raw.chat.bsky.convo.get_convo_for_members.assert_not_called()
        chat.chat.bsky.convo.get_convo_for_members.assert_called_once()


# ---------------------------------------------------------------------------
# Send behaviour
# ---------------------------------------------------------------------------

class TestSend:
    def test_sends_to_convo_id(self, channel):
        ch, _, chat = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Current: 72°F\n\nForecast: Sunny"],
            reply_to_uri="convo123",
        )
        result = ch.send(payload)

        assert result.success
        call_data = chat.chat.bsky.convo.send_message.call_args[0][0]
        assert call_data.convo_id == "convo123"
        assert "72°F" in call_data.message.text

    def test_thread_joined_into_single_message(self, channel):
        ch, _, chat = channel
        payload = NotificationPayload(
            request_db_id=1,
            post_thread=["Post 0", "Post 1", "Post 2"],
            reply_to_uri="convo123",
        )
        ch.send(payload)

        call_data = chat.chat.bsky.convo.send_message.call_args[0][0]
        assert "Post 0" in call_data.message.text
        assert "Post 1" in call_data.message.text
        assert "Post 2" in call_data.message.text

    def test_empty_thread_returns_failure(self, channel):
        ch, _, _ = channel
        result = ch.send(NotificationPayload(request_db_id=1, post_thread=[]))
        assert not result.success

    def test_no_convo_no_recipient_returns_failure(self, channel):
        ch, _, _ = channel
        result = ch.send(NotificationPayload(
            request_db_id=1,
            post_thread=["msg"],
            reply_to_uri=None,
            recipient_handle=None,
        ))
        assert not result.success

    def test_api_error_returns_failure(self, channel):
        ch, _, chat = channel
        chat.chat.bsky.convo.send_message.side_effect = Exception("network error")
        result = ch.send(NotificationPayload(
            request_db_id=1,
            post_thread=["msg"],
            reply_to_uri="convo123",
        ))
        assert not result.success
        assert "network error" in result.error


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------

class TestSplitMessage:
    def _ch(self):
        return BlueskyDMNotifyChannel.__new__(BlueskyDMNotifyChannel)

    def test_short_message_not_split(self):
        result = self._ch()._split_message("Short message", max_len=100)
        assert result == ["Short message"]

    def test_splits_at_paragraph_boundary(self):
        text = "Para one\n\nPara two\n\nPara three"
        result = self._ch()._split_message(text, max_len=15)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 15

    def test_all_content_preserved_after_split(self):
        text = "Para one\n\nPara two\n\nPara three"
        result = self._ch()._split_message(text, max_len=15)
        # Rejoin with paragraph separator and verify all text is present
        joined = "\n\n".join(result)
        assert "Para one" in joined
        assert "Para two" in joined
        assert "Para three" in joined

    def test_hard_splits_oversized_paragraph(self):
        text = "x" * 50
        result = self._ch()._split_message(text, max_len=20)
        assert all(len(c) <= 20 for c in result)
        assert "".join(result) == text

    def test_exactly_max_len_not_split(self):
        text = "a" * 100
        result = self._ch()._split_message(text, max_len=100)
        assert result == [text]


# ---------------------------------------------------------------------------
# Integration tests — hit real Bluesky DM API
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBlueskyDMIntegration:
    def test_login_and_chat_proxy(self, settings):
        """Verifies credentials are valid and the chat proxy initialises."""
        ch = BlueskyDMNotifyChannel(settings.bluesky_handle, settings.bluesky_app_password)
        assert ch._client is not None, "Login failed"
        assert ch._chat_client is not None, "Chat proxy not initialised"
        assert ch._chat_client is not ch._client, "Chat proxy should be a separate client"

    def test_resolve_handle_to_did(self, settings):
        """Verifies handle → DID resolution works (needed by _get_or_create_convo)."""
        ch = BlueskyDMNotifyChannel(settings.bluesky_handle, settings.bluesky_app_password)
        resolved = ch._client.resolve_handle(handle=settings.bluesky_handle)
        assert resolved.did.startswith("did:"), f"Unexpected DID format: {resolved.did}"

    # Note: full send-DM integration test requires a second Bluesky account.
    # DMs to self are rejected by the API ("Convos may only contain two members").
