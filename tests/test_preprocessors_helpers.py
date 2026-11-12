"""Characterization tests for small preprocessor helpers."""

import logging

import numpy as np
import pandas as pd
import pytest

from parseus.preprocessors import reg_updates, state_reduction
from parseus.preprocessors.function_code import (
    fc_transformer,
    format_code_count_string,
    get_function_codes_data,
    get_summary_from_function_codes,
)
from parseus.preprocessors.util import build_stats_frame, get_end, get_start

# ----------------------------------------------------------------- reg_updates

COLS = [
    "10.2.2.2->10.2.2.1:point_Counter__16Bit_0",
    "10.2.2.2->10.2.2.1:point_Counter__16Bit_1",
    "10.2.2.3->10.2.2.1:modbus",
]


def test_parse_reg_names_with_designation():
    assert reg_updates.parse_reg_names(COLS[0]) == {
        "con": COLS[0],
        "src": "10.2.2.2",
        "dst": "10.2.2.1",
        "reg_designation": "point_Counter__16Bit",
        "reg": "0",
    }


def test_parse_reg_names_without_designation():
    assert reg_updates.parse_reg_names("a->b:r1") == {
        "con": "a->b:r1",
        "src": "a",
        "dst": "b",
        "reg_designation": "none",
        "reg": "r1",
    }


def test_parse_reg_names_verbose_logs_each_field(caplog):
    with caplog.at_level(logging.DEBUG):
        reg_updates.parse_reg_names("a->b:r1", verbose=True)
    assert "src: a" in caplog.text and "reg: r1" in caplog.text


def test_get_title_single_and_multiple_devices():
    one = pd.DataFrame(columns=COLS[:2])
    assert reg_updates.get_title(one) == "10.2.2.2->10.2.2.1"
    assert reg_updates.get_title(pd.DataFrame(columns=[COLS[0], "9.9.9.9->x:p_1"])) == (
        "Multiple Devices"
    )


def test_reg_designation_and_ip_pair_maps():
    frame = pd.DataFrame(columns=["a->b:p_q_0", "a->b:p_q_1", "c->b:r_0"])
    assert sorted(reg_updates.map_all_reg_designations(frame)) == ["p_q", "r"]
    assert sorted(reg_updates.map_all_reg_designations(frame).values()) == [1, 2]
    assert sorted(reg_updates.map_ip_pairs(frame)) == ["a->b", "c->b"]
    short = reg_updates.map_shortname_to_canonical(frame)
    assert sorted(short.values()) == sorted(frame.columns)
    assert all(name.count(":") == 2 for name in short)


def test_make_reg_updates_df_flags_changes():
    binned = pd.DataFrame({"x": [1, 1, 2], "y": [5, 6, 6]})
    result = reg_updates.make_reg_updates_df(binned)
    assert result.to_dict("list") == {"x": [0, 1], "y": [1, 0]}
    assert result.index.tolist() == [1, 2]


# ---------------------------------------------------------------- function_code


def _bucket(codes):
    return {
        "key": {
            "date": "2024-01-01",
            "source_ips": "1.1.1.1",
            "destination_ips": "2.2.2.2",
        },
        "func_codes": {"buckets": [{"key": k, "doc_count": c} for k, c in codes]},
    }


def test_fc_transformer_flattens_buckets():
    assert fc_transformer(_bucket([("READ_COILS", 3), ("WRITE", 1)])) == [
        {
            "@timestamp": "2024-01-01",
            "source_ip": "1.1.1.1",
            "dest_ip": "2.2.2.2",
            "code": "READ_COILS",
            "count": 3,
        },
        {
            "@timestamp": "2024-01-01",
            "source_ip": "1.1.1.1",
            "dest_ip": "2.2.2.2",
            "code": "WRITE",
            "count": 1,
        },
    ]


def test_format_code_count_string_aligns_columns():
    assert format_code_count_string(["A", "LONGER"], [100, 5]) == (
        "100  A     \n  5  LONGER"
    )


@pytest.fixture
def fc_state():
    index = pd.to_datetime(
        ["2024-01-01 00:00:00", "2024-01-01 00:00:00", "2024-01-02 12:30:00"]
    )
    return pd.DataFrame(
        {
            "source_ip": ["1", "1", "3"],
            "dest_ip": ["2", "2", "4"],
            "code": ["READ", "WRITE", "READ"],
            "count": [5, 1, 2],
        },
        index=index,
    )


def test_function_code_summary(fc_state):
    summary = get_summary_from_function_codes(fc_state)
    assert summary["start_time"] == "January 01, 2024 00:00:00"
    assert summary["end_time"] == "January 02, 2024 12:30:00"
    assert summary["start_end"] == (
        "January 01, 2024 00:00:00 to January 02, 2024 12:30:00"
    )
    assert summary["total_time"] == "1 days 12:30:00"
    assert summary["unique_timestamps"] == 2
    assert summary["unique_pairs"] == 2
    assert summary["fc_summary"] == "7  READ \n1  WRITE"


def test_function_codes_data_groups_by_conversation(fc_state):
    data = get_function_codes_data(fc_state)
    assert list(data) == ["1 -> 2", "3 -> 4"]
    assert data["1 -> 2"]["protocols"] == ["modbus"]
    assert data["1 -> 2"]["points"] == {
        "2024-01-01T00:00:00.000000Z": {"READ": 5, "WRITE": 1}
    }
    assert data["3 -> 4"]["points"] == {"2024-01-02T12:30:00.000000Z": {"READ": 2}}
    assert data["3 -> 4"]["stats"]["unique_pairs"] == 1


# ------------------------------------------------------------------------ util


def test_build_stats_frame_unweighted():
    frame = pd.DataFrame({"a": [1.0, 2.0, 2.0, 3.0], "b": [1.0, 1.0, 1.0, 1.0]})
    stats = build_stats_frame(frame)
    assert list(stats.index) == [
        "Min",
        "Max",
        "Mean",
        "Med",
        "Mode",
        "Var",
        "Std",
        "Skew",
        "Kurt",
    ]
    assert stats["a"]["Min"] == 1.0 and stats["a"]["Max"] == 3.0
    assert stats["a"]["Mean"] == 2.0 and stats["a"]["Med"] == 2.0
    assert stats["a"]["Mode"] == 2.0
    assert stats["a"]["Var"] == pytest.approx(0.5)
    assert stats["a"]["Std"] == pytest.approx(0.5**0.5)
    assert stats["b"]["Var"] == 0.0


def test_build_stats_frame_weighted_matches_manual_moments():
    frame = pd.DataFrame({"a": [1.0, 3.0]})
    stats = build_stats_frame(frame, weights=np.array([3.0, 1.0]))
    assert stats["a"]["Mean"] == pytest.approx(1.5)
    assert stats["a"]["Var"] == pytest.approx(0.75)  # (3*0.25 + 2.25)/4
    assert stats["a"]["Std"] == pytest.approx(0.75**0.5)
    assert stats["a"]["Skew"] == pytest.approx(
        ((3 * (-0.5) ** 3 + 1.5**3) / 4) / 0.75**1.5
    )
    assert stats["a"]["Kurt"] == pytest.approx(((3 * 0.5**4 + 1.5**4) / 4) / 0.75**2)


def test_build_stats_frame_mode_of_empty_frame_is_nan():
    stats = build_stats_frame(pd.DataFrame({"a": [np.nan, np.nan]}))
    assert np.isnan(stats["a"]["Mode"])


def test_get_start_and_end_for_frames_and_ops():
    frame = pd.DataFrame(
        {"a": [1, 2]}, index=pd.to_datetime(["2024-01-01", "2024-01-03"])
    )
    assert get_start(frame).isoformat().startswith("2024-01-01T00:00:00")
    assert get_end(frame).isoformat().startswith("2024-01-03T00:00:00")
    ops = [("2024-01-02T00:00:00Z",), ("2024-01-01T00:00:00Z",)]
    assert get_start(ops).day == 1 and get_end(ops).day == 2
    assert get_start([]).year == 1970
    assert get_end(frame.iloc[0:0]).year == 1970
