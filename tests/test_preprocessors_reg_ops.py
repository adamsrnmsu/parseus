"""Characterization tests for modbus packet -> regop conversion."""

import pytest

from parseus.preprocessors.reg_ops import modbus_to_regop, packet_to_regops


def _packet(
    func="READ_HOLDING_REGISTERS",
    values_key="response_values",
    values=(5, 6),
    address=10,
):
    detailed = {"address": address}
    if values_key:
        detailed[values_key] = list(values)
    return {
        "@timestamp": "2024-01-02T03:04:05.678Z",
        "source": {"ip": "10.0.0.1"},
        "destination": {"ip": "10.0.0.2"},
        "zeek": {"modbus_detailed": detailed, "modbus": {"func": func}},
    }


TS = "2024-01-02T03:04:05.678000Z"


def test_holding_registers_emit_presence_then_values():
    assert modbus_to_regop(_packet()) == [
        (TS, "modbus", "10.0.0.1", "10.0.0.2", "MODBUS_PRESENT", 1),
        (TS, "modbus", "10.0.0.1", "10.0.0.2", "modbus_HR_10", 5.0),
        (TS, "modbus", "10.0.0.1", "10.0.0.2", "modbus_HR_11", 6.0),
    ]


@pytest.mark.parametrize(
    "func,reg_type",
    [
        ("READ_HOLDING_REGISTERS", "HR"),
        ("WRITE_MULTIPLE_REGISTERS", "HR"),
        ("WRITE_SINGLE_REGISTER", "HR"),
        ("READ_COILS", "C"),
        ("WRITE_SINGLE_COIL", "C"),
        ("WRITE_MULTIPLE_COILS", "C"),
        ("READ_INPUT_REGISTERS", "IR"),
        ("READ_DISCRETE_INPUTS", "DI"),
    ],
)
def test_function_code_selects_register_type(func, reg_type):
    ops = modbus_to_regop(_packet(func=func, values=(1,)))
    assert ops[1][4] == f"modbus_{reg_type}_10"


def test_request_values_used_when_no_response_values():
    ops = modbus_to_regop(_packet(values_key="request_values", values=(7,)))
    assert ops[1][4:] == ("modbus_HR_10", 7.0)


def test_unparseable_value_is_skipped():
    ops = modbus_to_regop(_packet(values=("x", 2)))
    assert [op[4:] for op in ops] == [("MODBUS_PRESENT", 1), ("modbus_HR_11", 2.0)]


def test_unmapped_function_code_yields_presence_only():
    ops = modbus_to_regop(_packet(func="MASK_WRITE_REGISTER"))
    assert ops == [(TS, "modbus", "10.0.0.1", "10.0.0.2", "MODBUS_PRESENT", 1)]


def _without(path):
    pkt = _packet()
    node = pkt
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = None
    return pkt


@pytest.mark.parametrize(
    "packet",
    [
        {},
        {"zeek": None},
        _without(["zeek", "modbus_detailed"]),
        _without(["zeek", "modbus_detailed", "address"]),
        _without(["zeek", "modbus"]),
        _without(["zeek", "modbus", "func"]),
        _packet(values=()),
        _packet(values_key=None),
    ],
)
def test_incomplete_packet_yields_nothing(packet):
    assert modbus_to_regop(packet) == []


def test_packet_to_regops_requires_zeek():
    assert packet_to_regops({"source": {}}) == []
    assert packet_to_regops(_packet())[0][4] == "MODBUS_PRESENT"
