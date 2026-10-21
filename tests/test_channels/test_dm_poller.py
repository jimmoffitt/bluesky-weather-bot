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

from bluesky_weather_bot.channels.alert.dm_poller import DMAlertChannel


@pytest.fixture
def poller(mock_settings):
    return DMAlertChannel(mock_settings)


@pytest.fixture
def poller_with_db(mock_settings, db):
    return DMAlertChannel(mock_settings, db=db)


def _make_log(msg_id: str, sender_did: str, text: str, convo_id: str = "convo1"):
    """Creates a raw logCreateMessage entry, shaped like the getLog JSON."""
    return {
        "$type": "chat.bsky.convo.defs#logCreateMessage",
        "rev": "rev1",
        "convoId": convo_id,
        "message": {
            "$type": "chat.bsky.convo.defs#messageView",
            "id": msg_id,
            "rev": "rev1",
            "text": text,
            "sender": {"did": sender_did},
            "sentAt": "2026-10-20T17:34:43.000Z",
        },
    }


def _make_get_log_response(logs, cursor=None):
    """Mimics the raw Response returned by invoke_query (JSON in .content)."""
    resp = MagicMock()
    resp.content = {"logs": logs}
    if cursor:
        resp.content["cursor"] = cursor
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
# Command extraction — alarm commands
# ---------------------------------------------------------------------------

class TestExtractCommandAlarms:
    @pytest.mark.parametrize("text", [
        "alarms", "list alarms", "my alarms", "show alarms",
        "alerts", "list alerts", "My Alarms",
    ])
    def test_list_alarms(self, text):
        assert DMAlertChannel._extract_command(text) == "list_alarms"

    @pytest.mark.parametrize("text", [
        "clear alarms", "delete all alarms", "remove all alerts", "Clear Alarms",
    ])
    def test_clear_alarms(self, text):
        assert DMAlertChannel._extract_command(text) == "clear_alarms"

    @pytest.mark.parametrize("text", [
        "delete alarm 1", "remove alarm 2", "delete alert 10",
    ])
    def test_delete_alarm(self, text):
        assert DMAlertChannel._extract_command(text) == "delete_alarm"

    def test_delete_alarm_requires_a_number(self):
        assert DMAlertChannel._extract_command("delete alarm") is None

    @pytest.mark.parametrize("text", [
        "edit alarm 1 to alert if temp hits 90",
        "update alarm 2 to notify me when wind exceeds 30 mph",
        "change alert 3 to rain chance over 50%",
    ])
    def test_edit_alarm(self, text):
        assert DMAlertChannel._extract_command(text) == "edit_alarm"

    @pytest.mark.parametrize("text", [
        "alert me if temp hits 100",
        "notify me when rain chance over 80%",
        "send me a dm if wind exceeds 50 mph",
        "set alarm temp hits 100",
        "add alarm temp hits 100",
        "alert me publicly if temp in Denver, CO hits 100",
        "alert me with post if temp in Denver, CO hits 100",
    ])
    def test_set_alarm(self, text):
        assert DMAlertChannel._extract_command(text) == "set_alarm"

    def test_plain_location_is_not_a_command(self):
        assert DMAlertChannel._extract_command("Denver, CO") is None


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
        chat.invoke_query.return_value = _make_get_log_response(
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

    def test_directive_extracted_and_does_not_corrupt_location(self, poller):
        chat = self._setup(poller)
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[_make_log("msg1", "did:plc:user", "80501 /forecast /day", "convo1")],
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert len(received) == 1
        assert received[0].raw_location == "80501"
        assert received[0].directives == frozenset({"forecast", "day"})

    def test_skips_own_messages(self, poller):
        chat = self._setup(poller, own_did="did:plc:bot")
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[_make_log("msg1", "did:plc:bot", "self-sent 80501")]
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert len(received) == 0

    def test_skips_duplicate_messages(self, poller):
        chat = self._setup(poller)
        poller._mark_seen("msg_dup", "convo1")
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[_make_log("msg_dup", "did:plc:user", "80501")]
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert len(received) == 0

    def test_dispatches_with_null_location_when_no_location(self, poller):
        """No-location DMs are dispatched with raw_location=None so the bot can send a help reply."""
        chat = self._setup(poller)
        chat.invoke_query.return_value = _make_get_log_response(
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
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[_make_log("msg_noloc", "did:plc:user", "hello there")]
        )
        poller._check_dms()
        assert poller._is_seen("msg_noloc")

    def test_cursor_advances_after_poll(self, poller):
        chat = self._setup(poller)
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[], cursor="newcursor"
        )
        poller._check_dms()
        assert poller._cursor == "newcursor"

    def test_unknown_log_types_do_not_block_messages(self, poller):
        """Regression (2026-10-20): Bluesky added logReadConvo, which the
        pinned atproto's closed union rejected — failing the whole page and
        freezing the cursor. Unknown types must be skipped, not fatal."""
        chat = self._setup(poller)
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[
                {"$type": "chat.bsky.convo.defs#logReadConvo", "rev": "r0", "convoId": "convo1"},
                {"$type": "chat.bsky.convo.defs#logSomethingFromTheFuture", "rev": "r0"},
                _make_log("msg1", "did:plc:user", "Weather for 80501", "convo1"),
            ],
            cursor="cur2",
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert [r.raw_location for r in received] == ["80501"]
        assert poller._cursor == "cur2"

    def test_malformed_create_message_skipped_not_fatal(self, poller):
        chat = self._setup(poller)
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[
                {"$type": "chat.bsky.convo.defs#logCreateMessage", "convoId": "convo1"},  # no message
                _make_log("msg2", "did:plc:user", "80501"),
            ],
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert [r.raw_location for r in received] == ["80501"]

    def test_cursor_sent_on_subsequent_poll(self, poller):
        chat = self._setup(poller)
        poller._cursor = "existing_cursor"
        chat.invoke_query.return_value = _make_get_log_response(logs=[])
        poller._check_dms()

        call_params = chat.invoke_query.call_args.kwargs["params"]
        assert call_params.cursor == "existing_cursor"

    def test_no_dispatch_when_chat_client_none(self, poller):
        poller._chat_client = None
        received = []
        poller.on_request(received.append)
        poller._check_dms()  # should not raise
        assert len(received) == 0


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

class TestCursorPersistence:
    def _setup(self, poller):
        mock_chat = MagicMock()
        poller._chat_client = mock_chat
        poller._own_did = "did:plc:bot"
        poller._did_to_handle = MagicMock(return_value="user.bsky.social")
        return mock_chat

    def test_cursor_saved_to_db_when_it_advances(self, poller_with_db, db):
        chat = self._setup(poller_with_db)
        chat.invoke_query.return_value = _make_get_log_response(logs=[], cursor="cur9")
        poller_with_db._check_dms()
        assert db.get_channel_state("dm.getlog_cursor") == "cur9"

    def test_restart_resumes_from_saved_cursor(self, mock_settings, db):
        """Regression (2026-10-20): the cursor lived only in memory, so a
        restart jumped to "now" and dropped DMs sent while the bot was down."""
        db.set_channel_state("dm.getlog_cursor", "saved_cur")
        poller = DMAlertChannel(mock_settings, db=db)
        chat = self._setup(poller)
        chat.invoke_query.return_value = _make_get_log_response(
            logs=[_make_log("msg_offline", "did:plc:user", "80501")], cursor="cur10",
        )
        received = []
        poller.on_request(received.append)
        poller._check_dms()

        assert chat.invoke_query.call_args.kwargs["params"].cursor == "saved_cur"
        assert [r.raw_location for r in received] == ["80501"]
        assert db.get_channel_state("dm.getlog_cursor") == "cur10"

    def test_rejected_saved_cursor_falls_back_to_now(self, mock_settings, db):
        db.set_channel_state("dm.getlog_cursor", "ancient_cur")
        poller = DMAlertChannel(mock_settings, db=db)
        chat = self._setup(poller)
        chat.invoke_query.side_effect = Exception("InvalidCursor")
        poller._check_dms()
        assert poller._cursor is None

        chat.invoke_query.side_effect = None
        chat.invoke_query.return_value = _make_get_log_response(logs=[], cursor="fresh")
        poller._check_dms()
        assert chat.invoke_query.call_args.kwargs["params"].cursor is None
        assert db.get_channel_state("dm.getlog_cursor") == "fresh"

    def test_transient_error_keeps_proven_cursor(self, poller_with_db):
        """Once the server has accepted our cursor, a later blip must not discard it."""
        chat = self._setup(poller_with_db)
        chat.invoke_query.return_value = _make_get_log_response(logs=[], cursor="good")
        poller_with_db._check_dms()
        chat.invoke_query.side_effect = Exception("503")
        poller_with_db._check_dms()
        assert poller_with_db._cursor == "good"

    def test_channel_state_upsert(self, db):
        assert db.get_channel_state("k") is None
        db.set_channel_state("k", "a")
        db.set_channel_state("k", "b")
        assert db.get_channel_state("k") == "b"
