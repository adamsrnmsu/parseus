"""Tests for PEAT normalization helpers."""

from parseus.preprocessors import peat_normalize
from parseus.preprocessors.peat_normalize import (
    event_cause_effect,
    event_is_auth_failure,
    firmware_version_label,
    normalize_io_modules,
    normalize_services,
)


def test_normalize_services_populates_required_comparison_fields():
    services = normalize_services({"protocol": "modbus", "port": 502})

    assert services == [{"protocol": "modbus", "port": 502, "status": "unknown"}]


def test_normalize_io_modules_maps_list_records_by_identity():
    assert normalize_io_modules([{"name": "module-a", "value": 1}]) == {
        "IO Modules": {"module-a": {"name": "module-a", "value": 1}}
    }


def test_normalize_io_modules_maps_single_record_by_identity():
    assert normalize_io_modules({"name": "module-a", "value": 1}) == {
        "IO Modules": {"module-a": {"name": "module-a", "value": 1}}
    }


def test_normalize_io_modules_maps_category_records_by_identity():
    assert normalize_io_modules({"Inputs": [{"id": "in-1", "value": True}]}) == {
        "Inputs": {"in-1": {"id": "in-1", "value": True}}
    }


def test_normalize_io_modules_preserves_named_category_mappings():
    assert normalize_io_modules({"Inputs": {"in-1": {"value": True}}}) == {
        "Inputs": {"in-1": {"value": True}}
    }


def test_event_helpers_support_normalized_auth_failure_events():
    event = {
        "action": "login",
        "category": ["authentication"],
        "outcome": "failure",
        "message": "bad password",
    }

    assert event_is_auth_failure(event) is True
    assert event_cause_effect(event) == ("login", "failure", "bad password")


def test_firmware_version_label_uses_best_available_identity():
    assert firmware_version_label({"version": "1.2.3"}) == "1.2.3"
    assert firmware_version_label({"hash": {"sha256": "abc"}}) == "abc"


def test_event_timestamp_picks_first_parseable_key_and_drops_tz():
    from datetime import datetime

    assert peat_normalize.event_timestamp({}) is None
    assert peat_normalize.event_timestamp("nope") is None
    assert peat_normalize.event_timestamp(
        {"created": "", "@timestamp": "2024-01-02T03:04:05Z"}
    ) == datetime(2024, 1, 2, 3, 4, 5)
    # an unparseable value falls through to the next key
    assert peat_normalize.event_timestamp(
        {"created": "garbage", "timestamp": "2024-05-06T07:08:09"}
    ) == datetime(2024, 5, 6, 7, 8, 9)
    assert peat_normalize.event_timestamp({"created": "garbage"}) is None
    # keys are tried in order of precedence
    assert peat_normalize.event_timestamp(
        {"ingested": "2024-01-01T00:00:00Z", "created": "2023-01-01T00:00:00Z"}
    ) == datetime(2023, 1, 1)


def test_normalize_firmware_logs_keeps_non_empty_record_lists():
    summary = {"firmware": {"v": 1}, "boot_firmware": [{}, {"v": 2}], "other": [1]}
    assert peat_normalize.normalize_firmware_logs(summary) == {
        "firmware": [{"v": 1}],
        "boot_firmware": [{"v": 2}],
    }
    assert peat_normalize.normalize_firmware_logs({"firmware": [None]}) == {}
    assert peat_normalize.normalize_firmware_logs({}) == {}


def test_normalize_process_memory_keys_by_process_name():
    summary = {
        "memory": [
            {"process": "init", "value": "10M"},
            {"process": "sshd", "kb": 5},
            {"process": "", "value": "ignored"},
            {"value": "no process"},
            "not a dict",
            {"process": "empty", "value": ""},
        ]
    }
    assert peat_normalize.normalize_process_memory(summary) == {
        "init": "10M",
        "sshd": {"process": "sshd", "kb": 5},
        "empty": "memory-5",
    }
