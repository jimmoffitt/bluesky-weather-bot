"""
Unit tests for FileWatcherAlertChannel.
No Bluesky credentials required.
"""

from __future__ import annotations

import pytest
import yaml
from pathlib import Path

from bluesky_weather_bot.channels.alert.file_watcher import FileWatcherAlertChannel


@pytest.fixture
def watcher(mock_settings):
    return FileWatcherAlertChannel(mock_settings)


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

class TestExtractLocation:
    def test_zip_code(self, watcher):
        assert watcher._extract_location("Weather for 80501 today") == "80501"

    def test_city_st(self, watcher):
        assert watcher._extract_location("Denver, CO forecast") == "Denver, CO"

    def test_zip_takes_priority_over_city(self, watcher):
        assert watcher._extract_location("Denver, CO 80501") == "80501"

    def test_no_location_returns_none(self, watcher):
        assert watcher._extract_location("general alert, no location") is None

    def test_empty_string(self, watcher):
        assert watcher._extract_location("") is None


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------

class TestParseFile:
    def _write(self, tmp_path, data, filename="alert.yaml"):
        f = tmp_path / filename
        f.write_text(yaml.dump(data))
        return f

    def test_zip_in_full_message(self, watcher, tmp_path):
        f = self._write(tmp_path, {
            "full_message": "Rain at Red Rocks 80127 #COWx",
            "message": "Rain at Red Rocks",
            "created_at": "2025-02-19T17:30:07+00:00",
            "host": "Test",
        })
        req = watcher._parse_file(f)
        assert req.raw_location == "80127"
        assert req.source_channel == "file"
        assert "Rain" in req.raw_content
        assert req.source_file == "alert.yaml"

    def test_city_st_in_full_message(self, watcher, tmp_path):
        f = self._write(tmp_path, {
            "full_message": "Storm warning for Denver, CO",
            "message": "Storm warning",
        })
        req = watcher._parse_file(f)
        assert req.raw_location == "Denver, CO"

    def test_location_falls_back_to_message_field(self, watcher, tmp_path):
        # full_message has no location; message field has a clear "City, ST" token
        f = self._write(tmp_path, {
            "full_message": "General weather notice",
            "message": "Longmont, CO forecast",
        })
        req = watcher._parse_file(f)
        assert req.raw_location == "Longmont, CO"

    def test_no_location_yields_none(self, watcher, tmp_path):
        f = self._write(tmp_path, {"full_message": "General alert", "message": "No loc"})
        req = watcher._parse_file(f)
        assert req.raw_location is None

    def test_message_only_field(self, watcher, tmp_path):
        f = self._write(tmp_path, {"message": "Flooding near 94102"})
        req = watcher._parse_file(f)
        assert req.raw_location == "94102"

    def test_invalid_yaml_raises(self, watcher, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("{ not valid yaml: [")
        with pytest.raises(Exception):
            watcher._parse_file(f)

    def test_non_mapping_yaml_raises(self, watcher, tmp_path):
        f = tmp_path / "list.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="mapping"):
            watcher._parse_file(f)


# ---------------------------------------------------------------------------
# File routing: archive on success, errors on parse failure
# ---------------------------------------------------------------------------

class TestHandleFile:
    def _setup_dirs(self, watcher):
        watcher._inbox.mkdir(parents=True, exist_ok=True)
        watcher._archive.mkdir(parents=True, exist_ok=True)
        watcher._errors.mkdir(parents=True, exist_ok=True)

    def test_dispatches_and_archives_on_success(self, watcher):
        self._setup_dirs(watcher)
        received = []
        watcher.on_request(received.append)

        f = watcher._inbox / "alert.yaml"
        f.write_text(yaml.dump({"full_message": "Test 80501", "message": "Test"}))
        watcher._handle_file(f)

        assert len(received) == 1
        assert received[0].raw_location == "80501"
        assert not f.exists()
        assert any(watcher._archive.iterdir())

    def test_moves_to_errors_on_parse_failure(self, watcher):
        self._setup_dirs(watcher)
        f = watcher._inbox / "bad.yaml"
        f.write_text("- not a mapping")

        watcher._handle_file(f)

        assert not f.exists()
        assert any(watcher._errors.iterdir())

    def test_no_dispatch_on_parse_failure(self, watcher):
        self._setup_dirs(watcher)
        received = []
        watcher.on_request(received.append)

        f = watcher._inbox / "bad.yaml"
        f.write_text("- not a mapping")
        watcher._handle_file(f)

        assert len(received) == 0


# ---------------------------------------------------------------------------
# Collision handling in _move
# ---------------------------------------------------------------------------

class TestMove:
    def test_timestamp_suffix_on_collision(self, watcher, tmp_path):
        src = tmp_path / "test.yaml"
        dst_dir = tmp_path / "archive"
        dst_dir.mkdir()
        src.write_text("new content")
        (dst_dir / "test.yaml").write_text("existing")

        watcher._move(src, dst_dir)

        files = list(dst_dir.iterdir())
        assert len(files) == 2
        assert not src.exists()

    def test_simple_move_when_no_collision(self, watcher, tmp_path):
        src = tmp_path / "test.yaml"
        dst_dir = tmp_path / "archive"
        dst_dir.mkdir()
        src.write_text("content")

        watcher._move(src, dst_dir)

        assert (dst_dir / "test.yaml").exists()
        assert not src.exists()
