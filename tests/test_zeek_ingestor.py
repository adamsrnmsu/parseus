"""Zeek ingestor: pure normalization of Zeek JSON logs plus the log-tail step.

Fixtures in tests/fixtures/zeek are real Zeek 9.0.1 output (deploy/integrations/zeek)
for a Modbus/DNP3 pcap with known values (deploy/integrations/zeek/scripts/make_test_pcap.py)
and for the dnp3_read.pcap trace from the Zeek test suite.
"""

import json
from pathlib import Path

from parseus.ingestors.zeek.normalize import doc_id, index_name, normalize
from parseus.ingestors.zeek.runner import read_new_lines
from parseus.preprocessors.reg_ops import modbus_to_regop, packet_to_regops

FIXTURES = Path(__file__).parent / "fixtures" / "zeek"


def load(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def test_modbus_detailed_matches_the_register_contract():
    rows = load("modbus_detailed.jsonl")
    doc = normalize("modbus_detailed", rows[1])  # READ_HOLDING_REGISTERS response
    assert doc["event"] == {"provider": "zeek", "dataset": "modbus_detailed"}
    assert doc["source"] == {"ip": "10.1.1.10", "port": 40000}
    assert doc["destination"] == {"ip": "10.1.1.20", "port": 502}
    assert doc["zeek"]["uid"] == rows[1]["uid"]
    assert doc["zeek"]["modbus"]["func"] == "READ_HOLDING_REGISTERS"
    assert doc["zeek"]["modbus_detailed"]["address"] == 100
    assert doc["zeek"]["modbus_detailed"]["response_values"] == [0, 1, 65535, 12345]
    assert doc["@timestamp"].startswith("2023-11-1") and doc["@timestamp"].endswith("Z")


def test_normalized_modbus_detailed_yields_register_operations():
    ops = {}
    for row in load("modbus_detailed.jsonl"):
        for op in packet_to_regops(normalize("modbus_detailed", row)):
            ops[op[4]] = op[5]
    assert ops["modbus_HR_100"] == 0.0
    assert ops["modbus_HR_102"] == 65535.0
    assert ops["modbus_HR_103"] == 12345.0
    assert ops["modbus_IR_7"] == 500.0 and ops["modbus_IR_8"] == 65000.0
    assert ops["modbus_HR_9"] == 4242.0  # write single register
    assert [ops[f"modbus_HR_{a}"] for a in (20, 21, 22)] == [1.0, 2.0, 3.0]
    assert [ops[f"modbus_C_{a}"] for a in (0, 2, 9)] == [1.0, 1.0, 1.0]
    assert ops["modbus_C_1"] == 0.0 and ops["modbus_C_3"] == 1.0  # coil write
    assert [ops[f"modbus_DI_{a}"] for a in (4, 5, 6)] == [1.0, 0.0, 1.0]


def test_read_request_without_values_yields_no_register_operations():
    request = next(r for r in load("modbus_detailed.jsonl") if r["is_orig"] and "request_values" not in r)
    assert modbus_to_regop(normalize("modbus_detailed", request)) == []


def test_dnp3_points_document():
    rows = load("dnp3_points.jsonl")
    row = next(r for r in rows if r["group"] == 30 and r["variation"] == 1 and r["index"] == 11)
    doc = normalize("dnp3_points", row)
    assert doc["event"] == {"provider": "zeek", "dataset": "dnp3_points"}
    assert doc["source"]["ip"] == "10.1.1.10" and doc["destination"]["port"] == 20000
    assert doc["zeek"]["dnp3_points"] == {
        "is_orig": False,
        "function_code": "RESPONSE",
        "group": 30,
        "object_type": "analog_input",
        "variation": 1,
        "index": 11,
        "value": -5.0,
        "flags": 1,
    }


def test_dnp3_point_values_match_the_pcap():
    got = {
        (d["group"], d["variation"], d["index"]): d["value"]
        for d in (normalize("dnp3_points", r)["zeek"]["dnp3_points"] for r in load("dnp3_points.jsonl"))
    }
    assert got[(30, 1, 12)] == 2000000000.0
    assert got[(30, 2, 21)] == -300.0
    assert got[(30, 5, 31)] == -0.15625  # float32
    assert got[(20, 1, 1)] == 4294967295.0  # counter
    assert [got[(1, 1, i)] for i in range(10)] == [1, 0, 1, 0, 0, 0, 0, 0, 0, 1]  # packed bits
    assert got[(1, 2, 7)] == 1.0 and got[(1, 2, 42)] == 0.0  # prefixed (0x17) index
    assert got[(30, 1, 1000)] == -1.0  # prefixed (0x28) index


def test_real_trace_points_keep_their_zero_based_range_index():
    rows = [normalize("dnp3_points", r) for r in load("dnp3_points_real.jsonl")]
    binary = [d["zeek"]["dnp3_points"] for d in rows if d["zeek"]["dnp3_points"]["group"] == 1]
    assert [p["index"] for p in binary] == list(range(9))
    assert [p["value"] for p in binary] == [1, 0, 1, 0, 0, 0, 0, 0, 0]  # same as tshark


def test_stock_logs_are_normalized_with_their_own_fields():
    doc = normalize("modbus", load("modbus.jsonl")[0])
    assert doc["event"]["dataset"] == "modbus"
    assert doc["zeek"]["modbus"]["func"] == load("modbus.jsonl")[0]["func"]
    doc = normalize("dnp3", load("dnp3.jsonl")[0])
    assert doc["zeek"]["dnp3"]["fc_request"] == "READ"


def test_unknown_or_malformed_records_are_skipped():
    row = load("dnp3_points.jsonl")[0]
    assert normalize("conn", row) is None
    assert normalize("dnp3_points", {"uid": "x"}) is None
    assert normalize("dnp3_points", {**row, "ts": "nope"}) is None


def test_doc_id_is_stable_and_unique_per_record():
    rows = load("dnp3_points.jsonl")
    ids = [doc_id("dnp3_points", r) for r in rows]
    assert ids == [doc_id("dnp3_points", dict(r)) for r in rows]
    assert len(set(ids)) == len(rows)


def test_index_name_is_partitioned_by_event_date():
    doc = normalize("modbus_detailed", load("modbus_detailed.jsonl")[0])
    assert index_name("arkime_sessions3-parseus-zeek-%y%m%d", doc) == "arkime_sessions3-parseus-zeek-231115"


def test_read_new_lines_resumes_and_leaves_partial_lines(tmp_path):
    log, state = tmp_path / "dnp3_points.log", {}
    log.write_bytes(b'{"a":1}\n{"a":2}\n{"a"')
    assert read_new_lines(tmp_path, "dnp3_points", state) == ['{"a":1}', '{"a":2}']
    assert read_new_lines(tmp_path, "dnp3_points", state) == []
    with log.open("ab") as fh:
        fh.write(b':3}\n')
    assert read_new_lines(tmp_path, "dnp3_points", state) == ['{"a":3}']


def test_read_new_lines_follows_a_rotated_file_by_inode(tmp_path):
    log, state = tmp_path / "modbus.log", {}
    log.write_bytes(b"one\ntwo\n")
    assert read_new_lines(tmp_path, "modbus", state) == ["one", "two"]
    with log.open("ab") as fh:
        fh.write(b"three\n")
    log.rename(tmp_path / "modbus.2023-11-15-01-00-00.log")  # rotation keeps the inode
    (tmp_path / "modbus.log").write_bytes(b"four\n")
    assert read_new_lines(tmp_path, "modbus", state) == ["three", "four"]
    assert len(state) == 2


def test_read_new_lines_rereads_a_truncated_or_replaced_file(tmp_path):
    log, state = tmp_path / "modbus.log", {}
    log.write_bytes(b"first-line-aaaaaaaaaa\n")
    read_new_lines(tmp_path, "modbus", state)
    log.write_bytes(b"other-line-bbbbbbbbbbbb\n")  # same inode, new content
    assert read_new_lines(tmp_path, "modbus", state) == ["other-line-bbbbbbbbbbbb"]


def test_stock_dnp3_glob_does_not_match_dnp3_points(tmp_path):
    (tmp_path / "dnp3_points.log").write_bytes(b"x\n")
    assert read_new_lines(tmp_path, "dnp3", {}) == []


def test_dnp3_point_becomes_a_register_operation():
    row = next(r for r in load("dnp3_points.jsonl") if r["group"] == 30 and r["index"] == 300)
    ((ts, kind, src, dst, address, value),) = packet_to_regops(normalize("dnp3_points", row))
    assert (kind, src, dst, address, value) == ("dnp3", "10.1.1.10", "10.1.1.20", "dnp3_analog_input_300", 77.0)
    assert ts.startswith("2023-11-15")


def test_dnp3_points_query_targets_the_dataset():
    from parseus.datastore.query_api import get_dnp3_points_query, get_dnp3_return_fields

    must = get_dnp3_points_query()["bool"]["must"]
    assert {"term": {"event.dataset": "dnp3_points"}} in must
    assert "zeek.dnp3_points" in get_dnp3_return_fields()


def test_dnp3_function_code_lands_where_the_ot_analytic_aggregates():
    doc = normalize("dnp3", load("dnp3.jsonl")[0])
    assert doc["zeek"]["dnp3_objects"] == {"function_code": "READ"}


def test_registers_page_state_includes_dnp3_points(monkeypatch):
    from parseus.webapp.registers import utility

    point = normalize("dnp3_points", next(r for r in load("dnp3_points.jsonl") if r["group"] == 30))
    modbus = normalize("modbus_detailed", load("modbus_detailed.jsonl")[1])
    monkeypatch.setattr(utility, "get_modbus_registers", lambda *a, **k: [modbus])
    monkeypatch.setattr(utility, "get_dnp3_points", lambda *a, **k: [point])
    state = utility.get_reg_state(index="x", dshost=object())
    assert any("dnp3_analog_input" in str(c) for c in state.columns)


def test_read_new_lines_caps_each_poll_and_resumes(tmp_path, monkeypatch):
    from parseus.ingestors.zeek import runner

    monkeypatch.setattr(runner, "MAX_READ_BYTES", 10)
    (tmp_path / "modbus.log").write_bytes(b"aaaa\nbbbb\ncccc\n")
    state = {}
    assert read_new_lines(tmp_path, "modbus", state) == ["aaaa", "bbbb"]
    assert read_new_lines(tmp_path, "modbus", state) == ["cccc"]
    assert read_new_lines(tmp_path, "modbus", state) == []


def test_read_new_lines_does_not_reopen_fully_read_rotated_files(tmp_path, monkeypatch):
    rotated = tmp_path / "modbus.2023-11-15-01-00-00.log"
    rotated.write_bytes(b"old\n")
    state = {}
    assert read_new_lines(tmp_path, "modbus", state) == ["old"]
    opened = []
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: opened.append(self.name) or real_open(self, *a, **k))
    assert read_new_lines(tmp_path, "modbus", state) == []
    assert rotated.name not in opened
