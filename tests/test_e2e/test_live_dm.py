"""
Live DM smoke test — actually sends a Bluesky Direct Message.

Requires BSKY_HANDLE and BSKY_APP_PASSWORD in .local.env.
Sends a weather report as a DM to the bot's own handle (self-DM),
which verifies the full pipeline: weather fetch → format → DM delivery.

Run with:
    pytest tests/test_e2e/test_live_dm.py -m live -v

This test is intentionally NOT marked `integration` (which would pull it into
the normal CI suite). Mark it `live` and opt-in explicitly.
"""

from __future__ import annotations

import pytest

from bluesky_weather_bot.channels.notify.bluesky_dm import BlueskyDMNotifyChannel
from bluesky_weather_bot.channels.notify.base import NotificationPayload
from bluesky_weather_bot.config.settings import Settings
from bluesky_weather_bot.storage.db import Database
from bluesky_weather_bot.weather.formatter import WeatherFormatter
from bluesky_weather_bot.weather.service import WeatherService


def _load_settings() -> Settings:
    try:
        return Settings.load()
    except Exception as exc:
        pytest.skip(f"Could not load .local.env: {exc}")


@pytest.mark.live
class TestLiveDMPipeline:
    """
    Sends a real DM to the bot's own Bluesky handle.

    Uses self-DM (bot → itself) so no second account is needed.
    This exercises BlueskyDMNotifyChannel._get_or_create_convo() and
    _send_message() against the live Bluesky chat API.
    """

    TEST_LOCATION = "80503"   # Longmont, CO — known-good ZIP

    def test_weather_dm_delivered(self):
        """
        Actually delivers a weather DM to a second test account.

        Requires BSKY_TEST_RECIPIENT in the environment (a Bluesky handle
        distinct from BSKY_HANDLE — Bluesky forbids self-DMs).

        Set it in .local.env:
            BSKY_TEST_RECIPIENT=yourother.bsky.social
        """
        import os
        settings = _load_settings()
        recipient = os.getenv("BSKY_TEST_RECIPIENT", "").strip()
        if not recipient:
            pytest.skip(
                "Set BSKY_TEST_RECIPIENT=<handle> in .local.env to run this test. "
                "Must be a different account from BSKY_HANDLE (Bluesky forbids self-DMs)."
            )

        # 1. Fetch and format weather
        with Database(":memory:") as db:
            svc = WeatherService(db, skip_historical=True)
            reports = svc.lookup(self.TEST_LOCATION)

        assert reports, "WeatherService returned no reports"
        formatter = WeatherFormatter()
        posts = formatter.format_thread(reports[0])
        assert len(posts) >= 2

        # 2. Send as a DM to the test recipient
        dm_channel = BlueskyDMNotifyChannel(
            handle=settings.bluesky_handle,
            app_password=settings.bluesky_app_password,
        )
        payload = NotificationPayload(
            request_db_id=None,
            post_thread=posts,
            recipient_handle=recipient,
            target_channel="bluesky_dm",
        )
        result = dm_channel.send(payload)

        assert result.success, f"DM delivery failed: {result.error}"

    def test_dm_content_has_temperature(self):
        """The sent DM must contain a temperature reading."""
        settings = _load_settings()

        with Database(":memory:") as db:
            svc = WeatherService(db, skip_historical=True)
            reports = svc.lookup(self.TEST_LOCATION)

        posts = WeatherFormatter().format_thread(reports[0])
        full_text = "\n\n".join(posts)

        assert "°F" in full_text, "DM text missing temperature"
        assert "📍" in full_text, "DM text missing location pin"

    def test_dm_text_within_bluesky_dm_limit(self):
        """Each DM message chunk must fit within Bluesky's DM character limit."""
        settings = _load_settings()

        with Database(":memory:") as db:
            svc = WeatherService(db, skip_historical=True)
            reports = svc.lookup(self.TEST_LOCATION)

        posts = WeatherFormatter().format_thread(reports[0])
        full_text = "\n\n".join(posts)

        chunks = BlueskyDMNotifyChannel._split_message(full_text)
        for i, chunk in enumerate(chunks):
            assert len(chunk) <= 1000, f"DM chunk {i} is {len(chunk)} chars"
