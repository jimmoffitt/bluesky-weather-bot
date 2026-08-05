"""
Tests for Database alarm_rules CRUD methods.
"""

from __future__ import annotations

from bluesky_weather_bot.alarms.models import AlarmRule


def _make_rule(**overrides) -> AlarmRule:
    defaults = dict(
        user_did="did:plc:alice",
        user_handle="alice.bsky.social",
        location_raw="Denver, CO",
        metric="temp_current",
        operator="gte",
        threshold=100.0,
        units="imperial",
    )
    defaults.update(overrides)
    return AlarmRule(**defaults)


class TestAddAndGet:
    def test_add_returns_id_and_round_trips(self, db):
        rule_id = db.add_alarm_rule(_make_rule())
        assert isinstance(rule_id, int)

        rules = db.get_alarm_rules_for_user("did:plc:alice")
        assert len(rules) == 1
        r = rules[0]
        assert r.id == rule_id
        assert r.user_did == "did:plc:alice"
        assert r.location_raw == "Denver, CO"
        assert r.metric == "temp_current"
        assert r.operator == "gte"
        assert r.threshold == 100.0
        assert r.is_active is True
        assert r.fire_count == 0

    def test_get_only_returns_active_rules_for_that_user(self, db):
        db.add_alarm_rule(_make_rule(user_did="did:plc:alice"))
        other_id = db.add_alarm_rule(_make_rule(user_did="did:plc:bob"))
        db.deactivate_alarm_rule(other_id)

        assert len(db.get_alarm_rules_for_user("did:plc:alice")) == 1
        assert db.get_alarm_rules_for_user("did:plc:bob") == []

    def test_get_active_alarm_rules_spans_all_users(self, db):
        db.add_alarm_rule(_make_rule(user_did="did:plc:alice"))
        db.add_alarm_rule(_make_rule(user_did="did:plc:bob"))
        assert len(db.get_active_alarm_rules()) == 2

    def test_ordering_is_by_creation_time(self, db):
        first_id = db.add_alarm_rule(_make_rule(location_raw="first"))
        second_id = db.add_alarm_rule(_make_rule(location_raw="second"))
        rules = db.get_alarm_rules_for_user("did:plc:alice")
        assert [r.id for r in rules] == [first_id, second_id]


class TestDeactivateAndClear:
    def test_deactivate_removes_from_active_list(self, db):
        rule_id = db.add_alarm_rule(_make_rule())
        assert db.deactivate_alarm_rule(rule_id) is True
        assert db.get_alarm_rules_for_user("did:plc:alice") == []

    def test_deactivate_unknown_id_returns_false(self, db):
        assert db.deactivate_alarm_rule(999) is False

    def test_clear_deactivates_all_rules_for_user_only(self, db):
        db.add_alarm_rule(_make_rule(user_did="did:plc:alice"))
        db.add_alarm_rule(_make_rule(user_did="did:plc:alice"))
        db.add_alarm_rule(_make_rule(user_did="did:plc:bob"))

        cleared = db.clear_alarm_rules_for_user("did:plc:alice")
        assert cleared == 2
        assert db.get_alarm_rules_for_user("did:plc:alice") == []
        assert len(db.get_alarm_rules_for_user("did:plc:bob")) == 1

    def test_clear_with_no_active_rules_returns_zero(self, db):
        assert db.clear_alarm_rules_for_user("did:plc:nobody") == 0


class TestUpdates:
    def test_update_alarm_location_persists(self, db):
        rule_id = db.add_alarm_rule(_make_rule())
        db.update_alarm_location(rule_id, lat=39.7, lon=-104.99, display="Denver, CO")
        rule = db.get_alarm_rules_for_user("did:plc:alice")[0]
        assert rule.location_lat == 39.7
        assert rule.location_lon == -104.99
        assert rule.location_display == "Denver, CO"

    def test_update_alarm_checked_sets_timestamp(self, db):
        rule_id = db.add_alarm_rule(_make_rule())
        assert db.get_alarm_rules_for_user("did:plc:alice")[0].last_checked_at is None
        db.update_alarm_checked(rule_id)
        assert db.get_alarm_rules_for_user("did:plc:alice")[0].last_checked_at is not None

    def test_update_alarm_fired_increments_count_and_sets_timestamp(self, db):
        rule_id = db.add_alarm_rule(_make_rule())
        db.update_alarm_fired(rule_id)
        rule = db.get_alarm_rules_for_user("did:plc:alice")[0]
        assert rule.fire_count == 1
        assert rule.last_fired_at is not None

        db.update_alarm_fired(rule_id)
        rule = db.get_alarm_rules_for_user("did:plc:alice")[0]
        assert rule.fire_count == 2
