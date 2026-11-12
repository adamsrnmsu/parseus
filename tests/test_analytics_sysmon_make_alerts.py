"""Behavior tests for the Sysmon unapproved device/ip/user make_alerts functions."""

from datetime import datetime, timezone

import pytest

from parseus.analytics.core import StateStore, SuppressionWindow
from parseus.analytics.host_anomalies import (
    sysmon_unapproved_device_analytic as device,
    sysmon_unapproved_ip_analytic as ip,
    sysmon_unapproved_user_analytic as user,
)

NOON = "2026-01-05T12:00:00Z"
NIGHT_ONLY = [{"start": "01:00", "end": "02:00"}]

# name, module, doc key, rule-list kwarg, known-good kwarg, rule value key,
# new action, new original, outside action, outside original prefix, category
CASES = {
    "device": dict(
        mod=device, key="DeviceId", rules="device_rules", known="known_good_devices", docs="device_dicts",
        rule_key="devices", new_action="New Device", new_text="NEW Device V1",
        out_action="Device Outside Approved Window", out_text="Device V1 was seen outside approved access window for rule 'night'. Allowed windows: every day 01:00-02:00",
        category=["host"], subject=lambda a: a["device"], expected_subject={"id": "V1"},
    ),
    "ip": dict(
        mod=ip, key="ip", rules="ip_rules", known="known_good_ips", docs="ip_dicts",
        rule_key="ips", new_action="NEW IP, CHECK IP", new_text="NEW IP V1",
        out_action="IP Outside Approved Window", out_text="IP V1 was seen outside approved access window for rule 'night'. Allowed windows: every day 01:00-02:00",
        category=["connection"], subject=lambda a: a["source"], expected_subject={"ip": "V1"},
    ),
    "user": dict(
        mod=user, key="user", rules="user_rules", known="known_good_users", docs="user_dicts",
        rule_key="users", new_action="NEW User", new_text="NEW User V1",
        out_action="User Outside Approved Window", out_text="User V1 logged in outside approved access window for rule 'night'. Allowed windows: every day 01:00-02:00",
        category=["host"], subject=lambda a: a["user"], expected_subject={"name": "V1"},
    ),
}


def call(case, docs, known=(), rules=None, window=None, now=None):
    kwargs = {case["docs"]: docs, case["known"]: list(known), case["rules"]: rules, "window": window, "now": now}
    return case["mod"].make_alerts(**kwargs)


def doc(case, value="V1"):
    return {case["key"]: value, "@timestamp": NOON}


@pytest.fixture(params=sorted(CASES))
def case(request):
    return CASES[request.param]


def test_unknown_value_alerts(case):
    (alert,) = call(case, [doc(case)])
    assert alert["event"]["action"] == case["new_action"]
    assert alert["event"]["original"] == case["new_text"]
    assert alert["rule"] == {"reason": "not_in_whitelist"}
    assert alert["@timestamp"] == NOON
    assert alert["event"]["category"] == case["category"]
    assert case["subject"](alert) == case["expected_subject"]
    assert alert["handled"] is False


def test_known_good_value_is_quiet(case):
    assert call(case, [doc(case)], known=["V1"]) == []


def test_matching_rule_inside_window_allows_even_if_unknown(case):
    rule = {"name": "ok", case["rule_key"]: ["v1"]}
    assert call(case, [doc(case)], rules=[rule]) == []


def test_rule_outside_window_alerts_even_if_known_good(case):
    rule = {"name": "night", case["rule_key"]: ["V1"], "allowed_windows": NIGHT_ONLY}
    (alert,) = call(case, [doc(case)], known=["V1"], rules=[rule])
    assert alert["event"]["action"] == case["out_action"]
    assert alert["event"]["original"] == case["out_text"]
    assert alert["rule"] == {"name": "night", "reason": "outside_allowed_window"}


def test_unnamed_rule_outside_window(case):
    rule = {case["rule_key"]: ["V1"], "allowed_windows": NIGHT_ONLY}
    (alert,) = call(case, [doc(case)], rules=[rule])
    assert alert["rule"]["name"] == "unnamed rule"
    assert "rule 'unnamed rule'" in alert["event"]["original"]


def test_suppression_window_only_emits_once_per_value(case):
    store = StateStore()
    window = SuppressionWindow(store, "1h")
    now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    alerts = call(case, [doc(case), doc(case), doc(case, "V2")], window=window, now=now)
    assert [case["subject"](a) for a in alerts] == [
        case["expected_subject"],
        {k: "V2" for k in case["expected_subject"]},
    ]


def test_no_window_alerts_every_event(case):
    assert len(call(case, [doc(case), doc(case)])) == 2
