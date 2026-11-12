"""Tests for OTDevice construction from datastore documents."""

from parseus.interfaces.ot_device import OTDevice, find_device

FULL = {
    "@timestamp": "2024-01-01T00:00:00Z",
    "host": {
        "hostname": "  plc-1 ",
        "ip": " 10.0.0.5 ",
        "ssh_keys": ["k1"],
        "description": {
            "full": " Acme PLC 9000 ",
            "brand": " Acme ",
            "model": " 9000 ",
            "vendor": {"id": " acme-id "},
        },
    },
}


def test_full_record_is_stripped_and_exposed():
    device = OTDevice(FULL)
    assert device.device_name == "plc-1"
    assert device.host_ip == "10.0.0.5"
    assert device.full_description == "Acme PLC 9000"
    assert device.brand == "Acme"
    assert device.model == "9000"
    assert device.vendor == "acme-id"
    assert device.ssh_keys == ["k1"]
    assert device.device_timestamp == "2024-01-01T00:00:00Z"
    assert device.device_identifier == "Acme PLC 9000 10.0.0.5"
    assert device.interface is None


def test_str_lists_every_field():
    text = str(OTDevice(FULL))
    assert "HostName: plc-1" in text
    assert "Vendor: acme-id" in text
    assert "Brand: Acme" in text
    assert "Model: 9000" in text
    assert "IP: 10.0.0.5" in text
    assert text.endswith("Timestamp: 2024-01-01T00:00:00Z")


def test_partial_host_leaves_missing_fields_none():
    device = OTDevice({"@timestamp": "t", "host": {"hostname": "h", "ip": "1.1.1.1"}})
    assert device.device_name == "h"
    assert device.full_description is None
    assert device.vendor is None
    assert device.model is None
    assert device.brand is None
    assert device.ssh_keys is None


def test_description_without_vendor_id_or_full():
    device = OTDevice(
        {"@timestamp": "t", "host": {"ip": "1.1.1.1", "description": {"vendor": {}}}}
    )
    assert device.vendor is None
    assert device.full_description is None
    assert device.brand is None


def test_record_without_host_does_not_crash():
    device = OTDevice({"@timestamp": "t"})
    assert device.device_name is None
    assert device.host_ip is None
    assert device.vendor is None
    assert device.brand is None
    assert device.ssh_keys is None
    assert device.device_timestamp == "t"
    assert isinstance(device.device_identifier, str)


def test_find_device_matches_on_identifier():
    a, b = OTDevice(FULL), OTDevice(FULL)
    other = OTDevice(
        {"@timestamp": "t", "host": {"ip": "9.9.9.9", "description": {"full": "x"}}}
    )
    assert find_device(b, [other, a]) is a
    assert find_device(other, [a]) is None
    assert find_device(a, []) is None


def test_ssh_keys_are_passed_through_unmodified():
    host = {"ssh_keys": " ssh-rsa AAA ", "ip": "1.1.1.1"}
    assert OTDevice({"@timestamp": "t", "host": host}).ssh_keys == " ssh-rsa AAA "
