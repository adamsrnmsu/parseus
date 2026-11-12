"""Characterization tests for the PEAT / SCPI / OT function-code analytics.

Each scenario feeds fixed input through an analytic and compares the complete
set of alerts a caller sees against the golden JSON in
``tests/data/ot_analytics_golden.json``. Regenerate the golden file only for an
intentional behaviour change:
``python tests/test_analytics_ot_characterization.py --regen``.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from parseus.analytics.ot_internals.peat_auth_enumeration_analytic import (
    PeatAuthEnumerationAnalytic,
)
from parseus.analytics.ot_internals.peat_device_change_analytic import (
    PeatDeviceChangeAnalytic,
)
from parseus.analytics.ot_internals.peat_file_change_analytic import (
    PeatFileChangeAnalytic,
)
from parseus.analytics.ot_internals.peat_firmware_change_analytic import (
    PeatFirmwareChangeAnalytic,
)
from parseus.analytics.ot_internals.peat_io_change_analytic import PeatIOChangeAnalytic
from parseus.analytics.ot_internals.peat_logic_change_analytic import (
    PeatLogicChangeAnalytic,
)
from parseus.analytics.ot_internals.peat_process_change_analytic import (
    PeatProcessChangeAnalytic,
)
from parseus.analytics.ot_internals.peat_service_change_analytic import (
    PeatServiceChangeAnalytic,
)
from parseus.analytics.ot_network.ot_function_code_anomaly_analytic import (
    OTFunctionCodeAnomalyAnalytic,
    function_code_transform,
)
from parseus.analytics.scpi_analytics.scpi_compare_analytic import (
    search_based_make_alerts,
)
from parseus.analytics.scpi_analytics.scpi_transform import (
    get_range_from_dict,
    get_value_ranges,
    handle_mixed_types,
    parse_data,
)
from parseus.analytics.scpi_analytics.scpi_value_ranges_analytic import (
    find_command_alerts,
)

GOLDEN = Path(__file__).parent / "data" / "ot_analytics_golden.json"
T0 = "2024-01-01T00:00:00Z"
T1 = "2024-01-02T00:00:00Z"


def _default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    return str(obj)


def _norm(obj):
    """JSON-normalise (sets sorted, datetimes stringified, volatile fields dropped)."""
    data = json.loads(json.dumps(obj, default=_default))
    for alert in data if isinstance(data, list) else []:
        if isinstance(alert, dict) and isinstance(alert.get("event"), dict):
            alert["event"].pop("created", None)
    return data


def _run(analytic, docs):
    return analytic._run(None, None, docs, None)


def service():
    a = PeatServiceChangeAnalytic()
    a.training_device_services_ = {
        "d1": (
            T0,
            [
                {"protocol": "tcp", "port": 80, "status": "open"},
                {"protocol": "tcp", "port": 22, "status": "open"},
                {"protocol": "udp", "port": 53, "status": "open"},
            ],
        ),
        "d2": (T0, [{"protocol": "tcp", "port": 1, "status": "open"}]),
        "d4": (T0, [{"protocol": "tcp", "port": 1, "status": "open"}]),
    }
    return _run(
        a,
        {
            "d1": (
                T1,
                [
                    {"protocol": "tcp", "port": 80, "status": "closed"},
                    {"protocol": "tcp", "port": 443, "status": "open"},
                    {"protocol": "udp", "port": 53, "status": "open"},
                ],
            ),
            "d2": (T1, [{"protocol": "tcp", "port": 1, "status": "open"}]),
            "d3": (T1, []),
            "d4": (T1, [{"protocol": "tcp", "port": 2, "status": "open"}]),
        },
    )


def _auth_events(n, start_sec=0, step=1, outcome="failure"):
    base = datetime(2024, 1, 1, 12, 0, 0)
    return [
        {
            "outcome": outcome,
            "category": ["authentication"],
            "action": "login",
            "message": f"bad password {i % 2}",
            "created": (base + timedelta(seconds=start_sec + i * step)).isoformat(),
        }
        for i in range(n)
    ]


def auth():
    a = PeatAuthEnumerationAnalytic()
    untimed = {"outcome": "failure", "action": "auth", "message": "no time"}
    return _run(
        a,
        {
            "burst": (T1, {"log_a": _auth_events(12), "log_b": _auth_events(2)}),
            "slow": (T1, {"log_a": _auth_events(12, step=30)}),
            "few": (T1, {"log_a": _auth_events(3) + [untimed]}),
            "ok": (T1, {"log_a": _auth_events(12, outcome="success")}),
            "none": (T1, {}),
        },
    )


def io():
    a = PeatIOChangeAnalytic()
    a.training_device_io_ = {
        "d1": (
            T0,
            {
                "in": {"m1": {"a": 1, "b": [1, 2]}, "m2": {"a": 1}, "m3": {"x": 1}},
                "out": {"o1": {"a": 1}},
                "gone": {"g": {}},
            },
        ),
        "d2": (T0, {"in": {"m1": {"a": 1}}}),
    }
    return _run(
        a,
        {
            "d1": (
                T1,
                {
                    "in": {"m1": {"a": 2, "b": [2, 1]}, "m2": {"a": 1}, "m4": {}},
                    "out": {"o1": {"a": 1}, "o2": {}},
                    "new": {"n": {}},
                },
            ),
            "d2": (T1, {"in": {"m1": {"a": 1}}}),
            "d3": (T1, {}),
        },
    )


def process():
    a = PeatProcessChangeAnalytic()
    a.training_device_processes_ = {
        "d1": (T0, ["init", "sshd"]),
        "d2": (T0, ["init"]),
    }
    return _run(
        a,
        {
            "d1": (T1, ["init", "bash"]),
            "d2": (T1, ["init"]),
            "d3": (T1, ["x"]),
        },
    )


def file():
    a = PeatFileChangeAnalytic()
    a.training_device_files_ = {"d1": (T0, {"a", "b"}), "d2": (T0, {"a"})}
    return _run(
        a, {"d1": (T1, {"b", "c"}), "d2": (T1, {"a"}), "d3": (T1, {"z"})}
    )


def logic():
    a = PeatLogicChangeAnalytic()
    a.training_device_logic_ = {
        "d1": (T0, {"prog": [1, 2], "name": "a"}),
        "d2": (T0, {"prog": [1]}),
    }
    return _run(
        a,
        {
            "d1": (T1, {"prog": [2, 1], "name": "b"}),
            "d2": (T1, {"prog": [1]}),
            "d3": (T1, {}),
        },
    )


def firmware():
    a = PeatFirmwareChangeAnalytic()
    a.training_device_firmware_ = {
        "d1": (T0, {"log1": [{"version": "1.0"}], "log2": []}),
        "d2": (T0, {"log1": [{"version": "1.0"}]}),
    }
    return _run(
        a,
        {
            "d1": (
                T1,
                {
                    "log1": [{"version": "1.0"}, {"version": "2.0"}],
                    "log2": [{"hash": {"sha1": "abc"}}],
                    "log3": [{"version": "9"}],
                },
            ),
            "d2": (T1, {"log1": [{"version": "1.0"}]}),
            "d3": (T1, {"log1": []}),
        },
    )


def device():
    a = PeatDeviceChangeAnalytic()
    a.training_device_set_ = {("same", 1700000000), ("gone", 1700000100)}
    return _run(a, [("same", 1700001000), ("new", 1700002000)])


_FUNC_DOCS = [
    {
        "key": {"date": "2024-01-01T00:00:00Z"},
        "source_ips": {
            "buckets": [
                {
                    "key": "10.0.0.1",
                    "destination_ips": {
                        "buckets": [
                            {
                                "key": "10.0.0.2",
                                "modbus": {
                                    "buckets": [
                                        {"key": "Read", "doc_count": 3},
                                        {"key": "Write", "doc_count": 1},
                                    ]
                                },
                            },
                            {"key": "10.0.0.3"},
                        ]
                    },
                }
            ]
        },
    },
    {
        "key": {"date": "2024-01-01T01:00:00Z"},
        "source_ips": {
            "buckets": [
                {
                    "key": "10.0.0.1",
                    "destination_ips": {
                        "buckets": [
                            {
                                "key": "10.0.0.2",
                                "dnp3": {"buckets": [{"key": "Select", "doc_count": 5}]},
                                "selfm_func_code": {"buckets": [{"key": "X", "doc_count": 9}]},
                            }
                        ]
                    },
                }
            ]
        },
    },
]


def func_transform():
    df = function_code_transform(
        json.loads(json.dumps(_FUNC_DOCS)),
        protocols=["dnp3", "modbus", "selfm_func_code"],
    )
    empty = function_code_transform([], protocols=["dnp3"])
    return {"index": list(df.index), "table": df.to_dict("list"), "empty": empty.empty}


class _Logic:
    def __init__(self, baseline, anomalies):
        self.stats_df = pd.DataFrame({"mean": 1.0}, index=baseline)
        self._anomalies = anomalies

    def run_table(self, _a, dataset, buffer=3):
        return None, self._anomalies(dataset)


def _ot_run(columns, rows, baseline, anomaly_cols=()):
    a = OTFunctionCodeAnomalyAnalytic()
    idx = [datetime(2024, 1, 1, h) for h in range(len(rows))]
    data = pd.DataFrame(rows, columns=columns, index=idx)
    a.logic_ = _Logic(
        baseline,
        lambda d: pd.DataFrame({c: d[c] for c in anomaly_cols}, index=d.index),
    )
    end = datetime(2024, 1, 1, 5)
    return a._run(None, end, data, a._state)


def ot_dos():
    k = "1.1.1.1_2.2.2.2:modbus:Cold Restart"
    k2 = "1.1.1.1_2.2.2.2:dnp3:warm restart"
    k3 = "1.1.1.1_2.2.2.2:dnp3:Read"
    return _ot_run([k, k2, k3], [[0, 5, 1], [31, 40, 1]], [k, k2, k3])


def ot_enum():
    cols = [f"1.1.1.1_2.2.2.2:modbus:c{i}" for i in range(11)]
    cols += ["1.1.1.1_2.2.2.2:dnp3:d1", "3.3.3.3_4.4.4.4:modbus:e1"]
    alerts = _ot_run(cols, [[1] * 13, [1] * 13], [])
    # new codes come from a set: make code order and alert order deterministic
    for alert in alerts:
        codes = alert.get("modbus", {}).get("function_codes")
        if codes:
            alert["modbus"]["function_codes"] = sorted(codes)
            alert["event"]["original"] = "enumeration " + " ".join(sorted(codes))
    return sorted(alerts, key=lambda a: json.dumps(a, default=str, sort_keys=True))


def ot_rate():
    k = "1.1.1.1_2.2.2.2:modbus:Read"
    k2 = "1.1.1.1_2.2.2.2:modbus:Write"
    return _ot_run([k, k2], [[1, 0], [9, 1]], [k, k2], anomaly_cols=[k, k2])


def ot_empty():
    k = "1.1.1.1_2.2.2.2:modbus:Read"
    return _ot_run([k], [], [k], anomaly_cols=[k])


def scpi_parse():
    parsed = parse_data(
        [
            "VOLT 5",
            "VOLT 7.5",
            "OUTP ON",
            "OUTP OFF",
            "*RST",
            "MEAS (@3),x",
            "MEAS (@1)",
            "SENS:CH (@2),ON",
            "SENS:CH ON",
            "CAL 1 2",
            "CHAN 12",
            "CHAN LABEL",
        ]
    )
    return {"parsed": parsed, "ranges": get_value_ranges(parsed)}


def scpi_ranges():
    return {
        "dict_numeric": get_range_from_dict(
            [{"sub_command_or_value": "3", "channel": 1}, {"sub_command_or_value": "4.5", "channel": 5}]
        ),
        "dict_text": get_range_from_dict(
            [{"sub_command_or_value": "ON", "channel": 2}]
        ),
        "dict_text_first": get_range_from_dict(
            [{"sub_command_or_value": "ON", "channel": 2},
             {"sub_command_or_value": "1", "channel": 3}]
        ),
        "mixed": handle_mixed_types(
            ["ON", {"sub_command_or_value": "OFF", "channel": 4}, 3]
        ),
        "mixed_cmd": get_value_ranges(
            {"X": ["ON", {"sub_command_or_value": "OFF", "channel": 4}]}
        ),
    }


def scpi_alerts():
    ts = datetime(2024, 1, 1)

    def find(run, fit):
        return find_command_alerts("CMD", run, fit, ts)

    return {
        "range": find({"min": 0.0, "max": 9.0}, {"min": 1.0, "max": 5.0}),
        "in_range": find({"min": 2.0, "max": 3.0}, {"min": 1.0, "max": 5.0}),
        "channel": find({"channels": {1, 2, 9}}, {"channels": {1, 2}}),
        "value": find({"value": ["ON", "OFF", "X"]}, {"value": ["ON"]}),
        "label": find({"A", "B"}, {"A"}),
        "all": find(
            {"min": 0.0, "max": 9.0, "channels": {7}, "value": ["Q"]},
            {"min": 1.0, "max": 5.0, "channels": {1}, "value": ["ON"]},
        ),
    }


def scpi_compare():
    fit = [
        {"func_code": "A", "@timestamp": datetime(2024, 1, 1)},
        {"func_code": "", "@timestamp": datetime(2024, 1, 1)},
        {"func_code": "B", "@timestamp": datetime(2024, 1, 1)},
    ]
    run = [
        {"func_code": "B", "@timestamp": datetime(2024, 1, 2)},
        {"func_code": "C", "@timestamp": datetime(2024, 1, 2)},
        {"func_code": None, "@timestamp": datetime(2024, 1, 2)},
    ]
    return search_based_make_alerts(fit, run)


SCENARIOS = {
    f.__name__: f
    for f in (
        service, auth, io, process, file, logic, firmware, device,
        func_transform, ot_dos, ot_enum, ot_rate, ot_empty,
        scpi_parse, scpi_ranges, scpi_alerts, scpi_compare,
    )
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_matches_golden(name):
    golden = json.loads(GOLDEN.read_text())
    assert _norm(SCENARIOS[name]()) == golden[name]


def test_scenarios_are_not_vacuous():
    golden = json.loads(GOLDEN.read_text())
    for name in ("service", "auth", "io", "process", "file", "logic", "firmware",
                 "device", "ot_dos", "ot_enum", "ot_rate", "scpi_compare"):
        assert golden[name], name


class _Stub:
    """Suppression stub: refuses (key, action) pairs in `blocked`, records every ask."""

    def __init__(self, blocked):
        self.blocked = blocked
        self.asked = []

    def should_emit(self, key, action, now=None):
        self.asked.append((key, action))
        return (key, action) not in self.blocked


def test_scpi_ranges_suppression_drops_only_blocked_alert():
    run = {"min": 0.0, "max": 9.0, "channels": {7}}
    fit = {"min": 1.0, "max": 5.0, "channels": {1}}
    ts = datetime(2024, 1, 1)
    full = find_command_alerts("CMD", run, fit, ts)
    assert len(full) == 2
    stub = _Stub({("CMD", "unfamliar channel used")})
    kept = find_command_alerts("CMD", run, fit, ts, suppression=stub, now=ts)
    assert kept == [a for a in full if "unfamliar channel used" not in json.dumps(a, default=str)]
    assert len(kept) == 1
    assert [a for _, a in stub.asked] == ["values outside range of training data", "unfamliar channel used"]


def test_scpi_compare_suppression_drops_only_blocked_alert():
    fit = [{"func_code": "A", "@timestamp": datetime(2024, 1, 1)}]
    run = [{"func_code": "C", "@timestamp": datetime(2024, 1, 2)}]
    full = search_based_make_alerts(fit, run)
    assert len(full) == 2
    stub = _Stub({("A", "Value Removed")})
    kept = search_based_make_alerts(fit, run, suppression=stub, now=None)
    assert kept == full[1:]
    assert stub.asked == [("A", "Value Removed"), ("C", "Value Added")]


if __name__ == "__main__" and "--regen" in sys.argv:
    GOLDEN.write_text(
        json.dumps({n: _norm(f()) for n, f in sorted(SCENARIOS.items())}, indent=1, sort_keys=True)
        + "\n"
    )
