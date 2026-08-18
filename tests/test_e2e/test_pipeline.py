"""
End-to-end pipeline tests — no real Bluesky credentials required.

Injects AlertRequest objects directly into ZipWx._handle_request() with
mock notify channels. Hits the real Open-Meteo API for weather data.

Run with:
    pytest tests/test_e2e/test_pipeline.py -m integration -v
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))
from bot import ZipWx
from bluesky_weather_bot.channels.alert.base import AlertRequest
from bluesky_weather_bot.channels.alert.firehose import FirehoseAlertChannel
from bluesky_weather_bot.channels.alert.dm_poller import DMAlertChannel
from bluesky_weather_bot.channels.notify.base import (
    NotificationChannel, NotificationPayload, NotificationResult,
)
from bluesky_weather_bot.config.settings import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(skip_historical: bool = True) -> Settings:
    """Minimal Settings for pipeline tests — no real Bluesky creds needed."""
    tmp = tempfile.mkdtemp()
    return Settings(
        bluesky_handle="test.bsky.social",
        bluesky_app_password="fake-password",
        db_path=Path(":memory:"),
        inbox_path=Path(tmp) / "inbox",
        inbox_archive_path=Path(tmp) / "inbox" / "archive",
        inbox_error_path=Path(tmp) / "inbox" / "errors",
        inbox_poll_interval_sec=5.0,
        log_path=Path(tmp) / "test.log",
        log_level="DEBUG",
        weather_cache_ttl_minutes=30,
        skip_historical=skip_historical,
        post_mode="text",
        server_type="laptop",
        mention_backends=frozenset({"firehose"}),
    )


def _mock_channel(name: str) -> MagicMock:
    ch = MagicMock(spec=NotificationChannel)
    ch.CHANNEL_NAME = name
    ch.send.return_value = NotificationResult(success=True, channel=name)
    return ch


# ---------------------------------------------------------------------------
# Location extraction unit tests (no network)
# ---------------------------------------------------------------------------

class TestFirehoseLocationExtraction:
    """FirehoseAlertChannel._extract_location handles all real-world post patterns."""

    def test_mention_zip(self):
        assert FirehoseAlertChannel._extract_location("@zipwx.bsky.social 80501") == "80501"

    def test_mention_city_state(self):
        loc = FirehoseAlertChannel._extract_location("@zipwx.bsky.social Denver, CO")
        assert loc == "Denver, CO"

    def test_mention_city(self):
        loc = FirehoseAlertChannel._extract_location("@zipwx.bsky.social Seattle, WA")
        assert loc and "Seattle" in loc

    def test_mention_bare_city(self):
        loc = FirehoseAlertChannel._extract_location("@zipwx.bsky.social Minneapolis")
        assert loc == "Minneapolis"

    def test_mention_mixed_case(self):
        assert FirehoseAlertChannel._extract_location("@ZIPWX.BSKY.SOCIAL 94102") == "94102"

    def test_mention_zip_with_trailing_hashtag(self):
        assert FirehoseAlertChannel._extract_location("@zipwx.bsky.social 80501 #weather") == "80501"

    def test_no_trigger_returns_none(self):
        assert FirehoseAlertChannel._extract_location("Just a normal post") is None

    def test_trigger_no_location_returns_none(self):
        assert FirehoseAlertChannel._extract_location("@zipwx.bsky.social") is None


class TestDMLocationExtraction:
    """DMAlertChannel._extract_location handles DM message patterns."""

    def test_zip_code(self):
        assert DMAlertChannel._extract_location("80503") == "80503"

    def test_city_state(self):
        loc = DMAlertChannel._extract_location("Denver, CO please")
        assert loc and "Denver" in loc

    def test_plain_city_name(self):
        loc = DMAlertChannel._extract_location("Minneapolis")
        assert loc == "Minneapolis"

    def test_empty_message_returns_none(self):
        assert DMAlertChannel._extract_location("") is None

    def test_short_text_returns_none(self):
        assert DMAlertChannel._extract_location("hi") is None


# ---------------------------------------------------------------------------
# Full pipeline injection tests (hit real Open-Meteo API)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestPipelineInjection:
    """
    Injects AlertRequests into ZipWx._handle_request() with mocked notify channels.
    The weather service calls the real Open-Meteo API.
    """

    @pytest.fixture
    def post_channel(self):
        return _mock_channel("bluesky_post")

    @pytest.fixture
    def dm_channel(self):
        return _mock_channel("bluesky_dm")

    @pytest.fixture
    def bot(self, post_channel, dm_channel):
        settings = _make_settings(skip_historical=True)
        zipwx = ZipWx(settings)
        zipwx._db.connect()
        zipwx.register_notify_channel(post_channel)
        zipwx.register_notify_channel(dm_channel)
        yield zipwx
        zipwx._db.close()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def test_firehose_routes_to_post_channel(self, bot, post_channel, dm_channel):
        request = AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location="80503",
            raw_content="#ZipWx 80503",
            reply_to_uri="at://did:plc:abc/app.bsky.feed.post/xyz",
            reply_to_cid="bafyreidummy",
        )
        bot._handle_request(request)

        post_channel.send.assert_called_once()
        dm_channel.send.assert_not_called()

    def test_dm_routes_to_dm_channel(self, bot, post_channel, dm_channel):
        request = AlertRequest(
            source_channel="dm",
            requester_handle="user.bsky.social",
            raw_location="80503",
            raw_content="80503",
            reply_to_uri="convo_id_abc123",
        )
        bot._handle_request(request)

        dm_channel.send.assert_called_once()
        post_channel.send.assert_not_called()

    # ------------------------------------------------------------------
    # Payload content
    # ------------------------------------------------------------------

    def test_firehose_payload_has_weather_posts(self, bot, post_channel):
        bot._handle_request(AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location="80503",
            raw_content="#ZipWx 80503",
        ))
        payload: NotificationPayload = post_channel.send.call_args[0][0]
        assert len(payload.post_thread) >= 2
        assert "°F" in payload.post_thread[0]
        assert "6 Hours" in payload.post_thread[1]

    def test_payload_posts_within_char_limit(self, bot, post_channel):
        bot._handle_request(AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location="80503",
            raw_content="#ZipWx 80503",
        ))
        payload: NotificationPayload = post_channel.send.call_args[0][0]
        for i, post in enumerate(payload.post_thread):
            assert len(post) <= 300, f"Post {i} is {len(post)} chars"

    def test_reply_uri_preserved_in_payload(self, bot, post_channel):
        reply_uri = "at://did:plc:abc/app.bsky.feed.post/xyz"
        bot._handle_request(AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location="80503",
            raw_content="#ZipWx 80503",
            reply_to_uri=reply_uri,
        ))
        payload: NotificationPayload = post_channel.send.call_args[0][0]
        assert payload.reply_to_uri == reply_uri

    def test_dm_convo_id_preserved(self, bot, dm_channel):
        convo_id = "convo_deadbeef"
        bot._handle_request(AlertRequest(
            source_channel="dm",
            requester_handle="user.bsky.social",
            raw_location="80501",
            raw_content="80501",
            reply_to_uri=convo_id,
        ))
        payload: NotificationPayload = dm_channel.send.call_args[0][0]
        assert payload.reply_to_uri == convo_id

    # ------------------------------------------------------------------
    # City name requests
    # ------------------------------------------------------------------

    def test_city_name_request(self, bot, post_channel):
        bot._handle_request(AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location="Minneapolis",
            raw_content="#ZipWx Minneapolis",
        ))
        post_channel.send.assert_called_once()
        payload: NotificationPayload = post_channel.send.call_args[0][0]
        assert "Minneapolis" in payload.post_thread[0]

    def test_ambiguous_city_sends_multiple_payloads(self, bot, post_channel):
        """Portland (OR + ME) → two separate notify calls."""
        bot._handle_request(AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location="Portland",
            raw_content="#ZipWx Portland",
        ))
        assert post_channel.send.call_count == 2

    # ------------------------------------------------------------------
    # Error cases
    # ------------------------------------------------------------------

    def test_invalid_zip_sends_error_reply(self, bot, post_channel):
        bot._handle_request(AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location="00000",
            raw_content="#ZipWx 00000",
            reply_to_uri="at://did:plc:abc/app.bsky.feed.post/xyz",
        ))
        post_channel.send.assert_called_once()
        payload: NotificationPayload = post_channel.send.call_args[0][0]
        # Error reply is a single post containing an apology
        assert len(payload.post_thread) == 1
        text = payload.post_thread[0].lower()
        assert "sorry" in text or "couldn't" in text or "not found" in text.lower()

    def test_no_location_firehose_sends_help(self, bot, post_channel):
        """#ZipWx with no location triggers a help reply."""
        bot._handle_request(AlertRequest(
            source_channel="firehose",
            requester_handle="user.bsky.social",
            raw_location=None,
            raw_content="#ZipWx",
            reply_to_uri="at://did:plc:abc/app.bsky.feed.post/xyz",
        ))
        post_channel.send.assert_called_once()
        payload: NotificationPayload = post_channel.send.call_args[0][0]
        assert len(payload.post_thread) == 1
        assert "zip" in payload.post_thread[0].lower() or "city" in payload.post_thread[0].lower()

    def test_no_location_dm_sends_help(self, bot, dm_channel):
        """DM with no location triggers a help reply via DM."""
        bot._handle_request(AlertRequest(
            source_channel="dm",
            requester_handle="user.bsky.social",
            raw_location=None,
            raw_content="hello",
            reply_to_uri="convo_abc",
        ))
        dm_channel.send.assert_called_once()
        payload: NotificationPayload = dm_channel.send.call_args[0][0]
        assert len(payload.post_thread) == 1
        assert "zip" in payload.post_thread[0].lower() or "city" in payload.post_thread[0].lower()
