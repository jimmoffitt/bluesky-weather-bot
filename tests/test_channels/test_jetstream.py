"""
Unit tests for JetstreamAlertChannel.
No Bluesky credentials required.
"""

from __future__ import annotations

import pytest

from bluesky_weather_bot.channels.alert.jetstream import JetstreamAlertChannel


@pytest.fixture
def channel(mock_settings):
    return JetstreamAlertChannel(mock_settings)


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

class TestIsTrigger:
    def test_mention_trigger(self, channel):
        assert channel._is_trigger("@testbot.bsky.social 80501")

    def test_mention_trigger_mid_text(self, channel):
        assert channel._is_trigger("hey @testbot.bsky.social Portland")

    def test_no_trigger(self, channel):
        assert not channel._is_trigger("Just talking about weather")


# ---------------------------------------------------------------------------
# Location extraction — delegates to mention_parsing; see
# test_mention_parsing.py for the full priority-order test matrix. These
# just confirm the delegation is wired correctly.
# ---------------------------------------------------------------------------

class TestExtractLocation:
    def test_zip_after_mention(self, channel):
        assert channel._extract_location("@testbot.bsky.social 80501") == "80501"

    def test_known_city_found_anywhere_in_message(self, channel):
        text = "Hey @testbot.bsky.social what is the forecast for Minneapolis?"
        assert channel._extract_location(text) == "Minneapolis"


# ---------------------------------------------------------------------------
# AlertRequest construction
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def _make_commit(self, rkey="abc123", cid="bafyreid123"):
        return {"rkey": rkey, "cid": cid}

    def test_builds_request_with_zip(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501",
            did="did:plc:abc123",
            commit=self._make_commit(),
            record={"text": "@testbot.bsky.social 80501"},
        )
        assert req is not None
        assert req.raw_location == "80501"
        assert req.requester_handle == "did:plc:abc123"
        assert req.source_channel == "jetstream"

    def test_at_uri_constructed_correctly(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501",
            did="did:plc:abc123",
            commit=self._make_commit(rkey="post999"),
            record={"text": "@testbot.bsky.social 80501"},
        )
        assert req.reply_to_uri == "at://did:plc:abc123/app.bsky.feed.post/post999"

    def test_cid_stored(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501",
            did="did:plc:abc123",
            commit=self._make_commit(cid="bafyreid999"),
            record={"text": "@testbot.bsky.social 80501"},
        )
        assert req.reply_to_cid == "bafyreid999"

    def test_source_created_at_taken_from_record(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501",
            did="did:plc:abc123",
            commit=self._make_commit(),
            record={"text": "@testbot.bsky.social 80501", "createdAt": "2026-08-17T23:28:18.955Z"},
        )
        assert req.source_created_at == "2026-08-17T23:28:18.955Z"

    def test_returns_request_with_null_location_when_no_location(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social",
            did="did:plc:abc123",
            commit=self._make_commit(),
            record={"text": "@testbot.bsky.social"},
        )
        assert req is not None
        assert req.raw_location is None

    def test_directive_extracted_and_does_not_corrupt_location(self, channel):
        req = channel._build_request(
            text="@testbot.bsky.social 80501 /forecast",
            did="did:plc:abc123",
            commit=self._make_commit(),
            record={"text": "@testbot.bsky.social 80501 /forecast"},
        )
        assert req.raw_location == "80501"
        assert req.directives == frozenset({"forecast"})
