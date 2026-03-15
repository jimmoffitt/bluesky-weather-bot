"""
Tests for DMAlertChannel.

Unit tests mock the atproto chat client and verify:
  - Location extraction
  - Two-layer deduplication (memory + DB)
  - get_log cursor advances correctly
  - Own messages are skipped
  - Dispatches correct AlertRequests
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from atproto import models as atproto_models

from bluesky_weather_bot.channels.alert.dm_poller import DMAlertChannel


@pytest.fixture
def poller(mock_settings):
    return DMAlertChannel(mock_settings)


@pytest.fixture
def poller_with_db(mock_settings, db):
    return DMAlertChannel(mock_settings, db=db)


def _make_log(msg_id: str, sender_did: str, text: str, convo_id: str = "convo1"):
    """Creates a mock LogCreateMessage entry."""
    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.sender.did = sender_did

    log = MagicMock(spec=atproto_models.ChatBskyConvoDefs.LogCreateMessage)
    log.message = msg
    log.convo_id = convo_id
    return log


def _make_get_log_response(logs, cursor=None):
    resp = MagicMock()
    resp.cursor = cursor
    resp.logs = logs
    return resp


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

class TestExtractLocation:
    def test_zip_code(self, poller):
        assert poller._extract_location("What's the weather at 80501?") == "80501"

    def test_city_st(self, poller):
        assert poller._extract_location("Denver, CO please") == "Denver, CO"

    def test_bare_city_name(self, poller):
        assert poller._extract_location("Portland") == "Portland"

    def test_bare_text_without_trigger(self, poller):
        assert poller._extract_location("Longmont") == "Longmont"

    def test_too_short_returns_none(self, poller):
        assert poller._extract_location("hi") is None

    def test_too_long_returns_none(self, poller):
        assert poller._extract_location("a" * 60) is None

    def test_zip_takes_priority_over_city(self, poller):
        assert poller._extract_location("Denver, CO 80501") == "80501"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDedup:
    def test_unseen_message_not_in_memory(self, poller):
        assert not poller._is_seen("msg_new")

    def test_mark_seen_adds_to_memory(self, poller):
        poller._mark_seen("msg1", "convo1")
        assert poller._is_seen("msg1")

    def test_db_seen_detected_before_memory(self, poller_with_db, db):
        db.mark_dm_seen("msg_db", "convo1")
        assert poller_with_db._is_seen("msg_db")

    def test_mark_seen_persists_to_db(self, poller_with_db, db):
        poller_with_db._mark_seen("msg_persist", "convo_x")
        assert db.is_dm_seen("msg_persist")

    def test_poller_without_db_still_works(self, poller):
        # Ensure no DB-related errors when db=None
        poller._mark_seen("msg1", "convo1")
        assert poller._is_seen("msg1")


# ---------------------------------------------------------------------------
# _check_dms — core polling logic
# ---------------------------------------------------------------------------

class TestCheckDMs:
    def _setup(self, poller, own_did="did:plc:bot"):
        mock_chat = MagicMock()
        poller._chat_client = mock_chat
        poller._own_did = own_did
        poller._did_to_handle = MagicMock(return_value="user.bsky.social")
        return mock_chat

    def test_dispatches_message_with_location(self, poller):
        chat = self._setup(poller)
        chat.chat.bsky.convo.get_log.return_value = _make_get_log_response(
            logs=[_make_log("msg1", "did:plc:user", "Weather for 80501", "convo1")],
            cursor="cur1",
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert len(received) == 1
        assert received[0].raw_location == "80501"
        assert received[0].reply_to_uri == "convo1"
        assert received[0].source_channel == "dm"

    def test_skips_own_messages(self, poller):
        chat = self._setup(poller, own_did="did:plc:bot")
        chat.chat.bsky.convo.get_log.return_value = _make_get_log_response(
            logs=[_make_log("msg1", "did:plc:bot", "self-sent 80501")]
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert len(received) == 0

    def test_skips_duplicate_messages(self, poller):
        chat = self._setup(poller)
        poller._mark_seen("msg_dup", "convo1")
        chat.chat.bsky.convo.get_log.return_value = _make_get_log_response(
            logs=[_make_log("msg_dup", "did:plc:user", "80501")]
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert len(received) == 0

    def test_dispatches_with_null_location_when_no_location(self, poller):
        """No-location DMs are dispatched with raw_location=None so the bot can send a help reply."""
        chat = self._setup(poller)
        chat.chat.bsky.convo.get_log.return_value = _make_get_log_response(
            logs=[_make_log("msg1", "did:plc:user", "hi")]  # too short for location extraction
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert len(received) == 1
        assert received[0].raw_location is None

    def test_message_marked_seen_even_without_location(self, poller):
        """No-location messages should still be marked seen to avoid reprocessing."""
        chat = self._setup(poller)
        chat.chat.bsky.convo.get_log.return_value = _make_get_log_response(
            logs=[_make_log("msg_noloc", "did:plc:user", "hello there")]
        )
        poller._check_dms()
        assert poller._is_seen("msg_noloc")

    def test_cursor_advances_after_poll(self, poller):
        chat = self._setup(poller)
        chat.chat.bsky.convo.get_log.return_value = _make_get_log_response(
            logs=[], cursor="newcursor"
        )
        poller._check_dms()
        assert poller._cursor == "newcursor"

    def test_cursor_sent_on_subsequent_poll(self, poller):
        chat = self._setup(poller)
        poller._cursor = "existing_cursor"
        chat.chat.bsky.convo.get_log.return_value = _make_get_log_response(logs=[])
        poller._check_dms()

        call_params = chat.chat.bsky.convo.get_log.call_args.kwargs["params"]
        assert call_params.cursor == "existing_cursor"

    def test_no_dispatch_when_chat_client_none(self, poller):
        poller._chat_client = None
        received = []
        poller.on_request(received.append)
        poller._check_dms()  # should not raise
        assert len(received) == 0
