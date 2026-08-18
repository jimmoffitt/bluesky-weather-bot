"""
Unit tests for mention_parsing — the shared trigger/location logic used by
FirehoseAlertChannel and JetstreamAlertChannel.
"""

from __future__ import annotations

from bluesky_weather_bot.channels.alert.mention_parsing import (
    extract_location, is_mention_trigger,
)


class TestIsMentionTrigger:
    def test_mention_trigger(self):
        assert is_mention_trigger("@zipwx.bsky.social 80501", "zipwx.bsky.social")

    def test_mention_trigger_mixed_case(self):
        assert is_mention_trigger("@ZipWx.bsky.social Denver, CO", "zipwx.bsky.social")

    def test_mention_trigger_mid_text(self):
        assert is_mention_trigger("hey @zipwx.bsky.social sup", "zipwx.bsky.social")

    def test_no_trigger(self):
        assert not is_mention_trigger("Just talking about weather", "zipwx.bsky.social")

    def test_zip_alone_is_not_trigger(self):
        assert not is_mention_trigger("80501 forecast today", "zipwx.bsky.social")


class TestExtractLocation:
    def test_zip_after_mention(self):
        assert extract_location("@zipwx.bsky.social 80501") == "80501"

    def test_city_st_after_mention(self):
        assert extract_location("@zipwx.bsky.social Denver, CO") == "Denver, CO"

    def test_bare_city_after_mention(self):
        assert extract_location("@zipwx.bsky.social Portland") == "Portland"

    def test_zip_anywhere_in_text(self):
        assert extract_location("check weather 80501 please @zipwx.bsky.social") == "80501"

    def test_city_st_anywhere_in_text(self):
        assert extract_location("Denver, CO needs weather @zipwx.bsky.social") == "Denver, CO"

    def test_none_when_mention_only(self):
        assert extract_location("@zipwx.bsky.social") is None

    def test_unknown_town_after_mention_still_returned_as_candidate(self):
        # Not a zip, not "City, ST", not in the top-200 list — falls through
        # to the last-resort "immediately after mention" candidate, left to
        # WeatherService's Nominatim fallback to resolve.
        assert extract_location("@zipwx.bsky.social Timnath") == "Timnath"

    # ------------------------------------------------------------------
    # Whole-message scanning — the fix for the reported bug: a known city
    # name anywhere in the message is found even when it's not the text
    # immediately following the mention.
    # ------------------------------------------------------------------

    def test_known_city_found_when_mention_leads_a_question(self):
        text = "Hey @zipwx.bsky.social what is the forecast for Minneapolis?"
        assert extract_location(text) == "Minneapolis"

    def test_known_city_found_regardless_of_mention_position(self):
        text = "Testing the new message consumer with two channels running\n\n@zipwx.bsky.social Minneapolis"
        assert extract_location(text) == "Minneapolis"

    def test_known_city_without_state_anywhere_in_text(self):
        assert extract_location("weather for Denver? @zipwx.bsky.social") == "Denver"

    def test_zip_takes_priority_over_known_city(self):
        text = "@zipwx.bsky.social is it snowing in Denver? try 80501"
        assert extract_location(text) == "80501"

    def test_city_st_takes_priority_over_bare_known_city(self):
        text = "@zipwx.bsky.social Denver, CO or maybe Chicago"
        assert extract_location(text) == "Denver, CO"
