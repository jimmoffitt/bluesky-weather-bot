"""
Shared pytest fixtures for bluesky_weather_bot tests.

Unit tests use mock_settings (no real credentials, no network).
Integration tests use settings (real .local.env credentials, hits Bluesky APIs).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluesky_weather_bot.config.settings import Settings
from bluesky_weather_bot.storage.db import Database

ENV_FILE = Path(__file__).parent.parent / ".local.env"


@pytest.fixture(scope="session")
def settings():
    """Real settings loaded from .local.env — used by integration tests only."""
    return Settings.load(ENV_FILE)


@pytest.fixture
def db():
    """In-memory Database, connected and schema-initialised."""
    with Database(":memory:") as d:
        yield d


@pytest.fixture
def mock_settings(tmp_path):
    """Minimal Settings for unit tests — no real credentials, paths under tmp_path."""
    return Settings(
        bluesky_handle="testbot.bsky.social",
        bluesky_app_password="test-xxxx-xxxx-xxxx",
        db_path=tmp_path / "test.db",
        inbox_path=tmp_path / "inbox",
        inbox_archive_path=tmp_path / "inbox" / "archive",
        inbox_error_path=tmp_path / "inbox" / "errors",
        inbox_poll_interval_sec=1.0,
        log_path=tmp_path / "test.log",
        log_level="DEBUG",
        weather_cache_ttl_minutes=30,
        skip_historical=True,
        post_mode="text",
    )
