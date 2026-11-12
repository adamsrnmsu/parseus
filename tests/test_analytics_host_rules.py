"""Behavior tests for the shared host-anomaly allow-list rule helpers."""

from datetime import datetime, timezone

import pytest

from parseus.analytics.host_anomalies import rules


def test_parse_event_datetime_variants():
    aware = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    assert rules.parse_event_datetime(aware) is aware
    naive = rules.parse_event_datetime(datetime(2026, 1, 1, 5))
    assert naive == aware
    assert rules.parse_event_datetime("2026-01-01T05:00:00Z") == aware
    assert rules.parse_event_datetime(" 2026-01-01T05:00:00+02:00 ").utcoffset().total_seconds() == 7200
    assert rules.parse_event_datetime("2026-01-01T05:00:00") == aware


def test_parse_event_datetime_garbage_and_none_fall_back_to_now():
    before = datetime.now(tz=timezone.utc)
    for value in ("not a date", None, ""):
        parsed = rules.parse_event_datetime(value)
        assert parsed.tzinfo is not None
        assert before <= parsed <= datetime.now(tz=timezone.utc)


def test_parse_time_and_normalize():
    assert rules.parse_time(" 22:30 ").hour == 22
    assert rules.parse_time("06:00:15").second == 15
    assert rules.normalize_rule_value("  AbC ") == "abc"
    assert rules.normalize_rule_value(None) == ""


@pytest.mark.parametrize(
    "day,expected",
    [(0, 0), (6, 6), (7, None), (-1, None), ("Mon", 0), (" THURS ", 3), ("sunday", 6), ("bogus", None), (None, None)],
)
def test_day_to_number(day, expected):
    assert rules.day_to_number(day) == expected


def test_get_rule_values_merges_keys_and_normalizes():
    rule = {"values": [" A ", "", None], "users": "Bob", "other": ["C"], "ips": None}
    assert rules.get_rule_values(rule, ("users", "ips")) == {"a", "bob"}
    assert rules.get_rule_values({"values": "X"}, ()) == {"x"}
    assert rules.get_rule_values({}, ("users",)) == set()


def test_rule_matches_value_is_case_insensitive():
    assert rules.rule_matches_value({"users": ["Bob"]}, " BOB", ("users",))
    assert not rules.rule_matches_value({"users": ["Bob"]}, "alice", ("users",))


def test_timestamp_in_window_days_and_hours():
    monday_noon = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)  # a Monday
    assert rules.timestamp_in_window(monday_noon, {})
    assert rules.timestamp_in_window(monday_noon, {"days": ["mon"], "start": "09:00", "end": "17:00"})
    assert not rules.timestamp_in_window(monday_noon, {"days": ["tue", "junk"]})
    assert rules.timestamp_in_window(monday_noon, {"days": ["junk"]})  # unknown days are ignored
    assert not rules.timestamp_in_window(monday_noon, {"start": "13:00", "end": "17:00"})
    assert rules.timestamp_in_window(monday_noon, {"start": "12:00", "end": "12:00"})


def test_timestamp_in_window_overnight():
    window = {"start": "22:00", "end": "06:00"}
    assert rules.timestamp_in_window(datetime(2026, 1, 5, 23, 0), window)
    assert rules.timestamp_in_window(datetime(2026, 1, 5, 5, 0), window)
    assert not rules.timestamp_in_window(datetime(2026, 1, 5, 12, 0), window)


def test_timestamp_allowed_by_rule_windows_and_timezones():
    assert rules.timestamp_allowed_by_rule("2026-01-05T03:00:00Z", {})
    rule = {"allowed_windows": [{"start": "09:00", "end": "17:00"}], "timezone": "America/New_York"}
    assert rules.timestamp_allowed_by_rule("2026-01-05T15:00:00Z", rule)  # 10:00 NY
    assert not rules.timestamp_allowed_by_rule("2026-01-05T03:00:00Z", rule)  # 22:00 NY
    # unknown timezone falls back to UTC
    bad = {"allowed_windows": [{"start": "09:00", "end": "17:00"}], "timezone": "Not/AZone"}
    assert rules.timestamp_allowed_by_rule("2026-01-05T10:00:00Z", bad)
    assert not rules.timestamp_allowed_by_rule("2026-01-05T20:00:00Z", bad)
    # default timezone is used when the rule has none
    plain = {"allowed_windows": [{"start": "09:00", "end": "17:00"}]}
    assert rules.timestamp_allowed_by_rule("2026-01-05T15:00:00Z", plain, "America/New_York")
    assert not rules.timestamp_allowed_by_rule("2026-01-05T03:00:00Z", plain, "America/New_York")
    assert rules.timestamp_allowed_by_rule("2026-01-05T10:00:00Z", plain, None)


def test_evaluate_allow_rules_outcomes():
    ts = "2026-01-05T12:00:00Z"
    allow = {"name": "a", "users": ["bob"]}
    deny = {"name": "d", "users": ["eve"], "allowed_windows": [{"start": "01:00", "end": "02:00"}]}
    disabled = {"name": "x", "users": ["zed"], "enabled": False}
    all_rules = [disabled, allow, deny]
    assert rules.evaluate_allow_rules("BOB", ts, all_rules, ("users",)) == (True, allow, None)
    assert rules.evaluate_allow_rules("eve", ts, all_rules, ("users",)) == (False, deny, "outside_allowed_window")
    assert rules.evaluate_allow_rules("zed", ts, all_rules, ("users",)) == (None, None, None)
    assert rules.evaluate_allow_rules("bob", ts, None, ("users",)) == (None, None, None)
    # without the value key the rule does not match
    assert rules.evaluate_allow_rules("bob", ts, all_rules) == (None, None, None)


def test_describe_allowed_windows():
    assert rules.describe_allowed_windows(None) == "always"
    assert rules.describe_allowed_windows({"allowed_windows": []}) == "always"
    rule = {"allowed_windows": [{"days": ["mon", "tue"], "start": "09:00", "end": "17:00"}, {}]}
    assert rules.describe_allowed_windows(rule) == "mon, tue 09:00-17:00; every day 00:00-23:59:59"
