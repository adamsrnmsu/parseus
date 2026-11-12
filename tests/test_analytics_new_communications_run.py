"""Behavior tests for NewCommunicationsAnalytic fit/run."""

from datetime import datetime, timezone

from parseus.analytics.core import StateStore
from parseus.analytics.network_anomalies.new_communications_analytic import (
    NewCommunicationsAnalytic,
)


def bucket(src, dst, *protocols):
    return {
        "key": {"source": src, "dest": dst},
        "protocols": {"buckets": [{"key": p} for p in protocols]},
    }


def fitted():
    analytic = NewCommunicationsAnalytic()
    analytic._fit([bucket("10.0.0.1", "10.0.0.2", "tcp", "http")], [bucket("10.0.0.2", "10.0.0.3", "udp")])
    return analytic


def run(analytic, docs, window="0s"):
    analytic.suppression_ = window
    return list(
        analytic._run(
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            docs,
            StateStore(),
        )
    )


def test_fit_stores_conversations_and_ips():
    analytic = fitted()
    assert analytic.protocol_convos_ == {
        ("10.0.0.1", "10.0.0.2"): {"tcp", "http"},
        ("10.0.0.2", "10.0.0.3"): {"udp"},
    }
    assert analytic.seen_ips_ == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}
    df = analytic.get_fit_results()
    assert list(df.columns) == ["ip_pair", "protocols"]
    assert sorted(map(tuple, df["protocols"])) == [("http", "tcp"), ("udp",)]


def test_fit_empty_training_set():
    analytic = NewCommunicationsAnalytic()
    analytic._fit([])
    assert analytic.protocol_convos_ == {}
    assert analytic.seen_ips_ == set()


def test_known_conversation_is_quiet():
    assert run(fitted(), [bucket("10.0.0.1", "10.0.0.2", "tcp")]) == []


def test_new_conversation_alert():
    alerts = run(fitted(), [bucket("10.0.0.1", "10.0.0.3", "udp", "tcp")])
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["event"]["action"] == "new conversation alert"
    assert alert["event"]["original"] == "New Conversation Alert: 10.0.0.1->10.0.0.3:['tcp', 'udp']"
    assert alert["source"] == {"ip": "10.0.0.1"}
    assert alert["destination"] == {"ip": "10.0.0.3"}
    assert alert["network"] == {"new_protocols": ["tcp", "udp"]}


def test_new_protocol_alert_lists_only_new_protocols():
    alerts = run(fitted(), [bucket("10.0.0.1", "10.0.0.2", "tcp", "smb", "ftp")])
    assert len(alerts) == 1
    assert alerts[0]["event"]["action"] == "new protocol seen"
    assert alerts[0]["event"]["original"] == "New Protocol Seen: 10.0.0.1->10.0.0.2:['ftp', 'smb']"
    assert alerts[0]["network"] == {"new_protocols": ["ftp", "smb"]}


def test_new_ip_alert_for_each_unseen_endpoint():
    alerts = run(fitted(), [bucket("10.9.9.1", "10.9.9.2", "tcp")])
    actions = [a["event"]["action"] for a in alerts]
    assert actions == ["new conversation alert", "new ip alert", "new ip alert"]
    ip_alerts = alerts[1:]
    assert [a["event"]["original"] for a in ip_alerts] == [
        "New IP Alert: 10.9.9.1",
        "New IP Alert: 10.9.9.2",
    ]
    assert ip_alerts[0]["event"]["category"] == ["network"]


def test_suppression_window_silences_repeats():
    analytic = fitted()
    analytic.suppression_ = "1h"
    state = StateStore()
    docs = [bucket("10.0.0.1", "10.0.0.2", "ftp")]
    args = (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc), docs)
    assert len(list(analytic._run(*args, state))) == 1
    assert list(analytic._run(*args, state)) == []
