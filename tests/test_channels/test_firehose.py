"""
Unit tests for FirehoseAlertChannel.
No Bluesky credentials required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bluesky_weather_bot.channels.alert.firehose import FirehoseAlertChannel


@pytest.fixture
def channel(mock_settings):
    return FirehoseAlertChannel(mock_settings)


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

class TestIsTrigger:
    def test_mention_trigger(self, channel):
        assert channel._is_trigger("@testbot.bsky.social 80501")

    def test_mention_trigger_mixed_case(self, channel):
        assert channel._is_trigger("@TestBot.bsky.social Denver, CO")

    def test_mention_trigger_mid_text(self, channel):
        assert channel._is_trigger("hey @testbot.bsky.social Portland")

    def test_no_trigger(self, channel):
        assert not channel._is_trigger("Just talking about weather")

    def test_zip_alone_is_not_trigger(self, channel):
        assert not channel._is_trigger("80501 forecast today")

    def test_hashtag_alone_is_not_trigger(self, channel):
        assert not channel._is_trigger("#ZipWx Denver, CO")


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

class TestExtractLocation:
    def test_zip_after_mention(self, channel):
        assert channel._extract_location("@testbot.bsky.social 80501") == "80501"

    def test_city_st_after_mention(self, channel):
        assert channel._extract_location("@testbot.bsky.social Denver, CO") == "Denver, CO"

    def test_bare_city_after_mention(self, channel):
        assert channel._extract_location("@testbot.bsky.social Portland") == "Portland"

    def test_zip_fallback_anywhere_in_text(self, channel):
        assert channel._extract_location("check weather 80501 please @testbot.bsky.social") == "80501"

    def test_city_st_fallback(self, channel):
        assert channel._extract_location("Denver, CO needs weather @testbot.bsky.social") == "Denver, CO"

    def test_none_when_mention_only(self, channel):
        assert channel._extract_location("@testbot.bsky.social") is None

    def test_text_after_mention_returned_as_candidate(self, channel):
        # Any text after the mention is returned as a location candidate;
        # the resolver will decide whether it's valid.
        result = channel._extract_location("@testbot.bsky.social please tell me the weather")
        assert result is not None


# ---------------------------------------------------------------------------
# AlertRequest construction
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def _make_op(self, rkey="abc123", cid="bafyreid123"):
        op = MagicMock()
        op.path = f"app.bsky.feed.post/{rkey}"
        op.cid = cid
        return op

    def test_builds_request_with_zip(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501",
            repo="did:plc:abc123",
            op=self._make_op(),
        )
        assert req is not None
        assert req.raw_location == "80501"
        assert req.requester_handle == "did:plc:abc123"
        assert req.source_channel == "firehose"

    def test_at_uri_constructed_correctly(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501",
            repo="did:plc:abc123",
            op=self._make_op(rkey="post999"),
        )
        assert req.reply_to_uri == "at://did:plc:abc123/app.bsky.feed.post/post999"

    def test_cid_stored_as_string(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501",
            repo="did:plc:abc123",
            op=self._make_op(cid="bafyreid999"),
        )
        assert req.reply_to_cid == "bafyreid999"

    def test_returns_request_with_null_location_when_no_location(self, channel):
        """No location → AlertRequest dispatched with raw_location=None (triggers help reply)."""
        req = channel._build_request(
            text="@testbot.bsky.social",
            repo="did:plc:abc123",
            op=self._make_op(),
        )
        assert req is not None
        assert req.raw_location is None
