"""
Tests for shared AlertChannel helpers.
"""

from __future__ import annotations

from bluesky_weather_bot.channels.alert.base import extract_directives


class TestExtractDirectives:
    def test_no_directive_returns_empty_set(self):
        text, directives = extract_directives("@zipwx.bsky.social 80501")
        assert directives == frozenset()
        assert text == "@zipwx.bsky.social 80501"

    def test_single_directive_extracted_and_stripped(self):
        text, directives = extract_directives("@zipwx.bsky.social 80501 /forecast")
        assert directives == frozenset({"forecast"})
        assert "/forecast" not in text
        assert "80501" in text

    def test_multiple_directives(self):
        text, directives = extract_directives("80501 /forecast /day")
        assert directives == frozenset({"forecast", "day"})
        assert "/forecast" not in text
        assert "/day" not in text

    def test_directives_are_lowercased(self):
        _, directives = extract_directives("80501 /FORECAST")
        assert directives == frozenset({"forecast"})

    def test_directive_in_middle_does_not_glue_neighbors(self):
        text, directives = extract_directives("temp /forecast hits 100")
        assert directives == frozenset({"forecast"})
        assert text.split() == ["temp", "hits", "100"]

    def test_unrelated_slash_is_not_treated_as_directive(self):
        # A bare slash with no following word characters shouldn't match.
        text, directives = extract_directives("N/A for this one")
        assert directives == frozenset()