"""Characterization tests for reg_state construction and bounding."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from parseus.preprocessors.reg_state import (
    _normalize_timestamp_for_index,
    _parse_initial_state,
    chunks,
    concatenate,
    get_state_at,
    load_regstate,
    reg_ops_to_reg_state,
    reg_state_time_bounds,
    sample_weights,
    to_pandas,
)


def op(sec, addr, value, src="a", dst="b"):
    return (f"2024-01-01T00:00:{sec:02d}.000Z", "modbus", src, dst, addr, value)


def ts(sec):
    return f"2024-01-01T00:00:{sec:02d}.000000Z"


def states(result):
    """Render documents as (seconds, {register: value}) pairs."""
    return [
        (int(d["@timestamp"][17:19]), {k: v for k, v in d.items() if k != "@timestamp"})
        for d in result
    ]


R1, R2, R3 = "a->b:r1", "a->b:r2", "a->b:r3"
OPS = [
    op(0, "r1", 1),
    op(0, "r2", 5),
    op(1, "r1", 1),  # no change: no new document
    op(2, "r1", 2),
    op(2, "r2", "x"),  # non-numeric: ignored
    op(3, "r2", "7"),  # numeric string: accepted
]


# --------------------------------------------------------------- reg_ops_to_reg_state


def test_state_changes_become_documents():
    assert states(reg_ops_to_reg_state(OPS)) == [
        (0, {R1: 1.0, R2: 5.0}),
        (2, {R1: 2.0, R2: 5.0}),
        (3, {R1: 2.0, R2: 7.0}),
    ]


def test_empty_and_all_non_numeric_ops_yield_nothing():
    assert list(reg_ops_to_reg_state([])) == []
    assert list(reg_ops_to_reg_state([op(0, "r1", "x")])) == []


def test_single_op_yields_single_document():
    assert states(reg_ops_to_reg_state([op(5, "r1", 1)])) == [(5, {R1: 1.0})]


def test_iterable_can_be_walked_twice():
    result = reg_ops_to_reg_state(OPS)
    assert states(result) == states(result)


def test_regex_prefilter_in_list():
    result = reg_ops_to_reg_state(OPS, column_pre_filter=[r".*r1"])
    assert states(result) == [(0, {R1: 1.0}), (2, {R1: 2.0})]


def test_all_prefilters_must_match():
    result = reg_ops_to_reg_state(
        OPS, column_pre_filter=[r".*->.*", lambda c: c.endswith("r2")]
    )
    assert states(result) == [(0, {R2: 5.0}), (3, {R2: 7.0})]


def test_nested_prefilter_lists_are_flattened():
    result = reg_ops_to_reg_state(OPS, column_pre_filter=[[r".*r1"], (r".*->b:.*",)])
    assert states(result) == [(0, {R1: 1.0}), (2, {R1: 2.0})]


def test_single_regex_string_prefilter():
    result = reg_ops_to_reg_state(OPS, column_pre_filter=r".*r1")
    assert states(result) == [(0, {R1: 1.0}), (2, {R1: 2.0})]


def test_single_callable_prefilter():
    result = reg_ops_to_reg_state(OPS, column_pre_filter=lambda c: c.endswith("r2"))
    assert states(result) == [(0, {R2: 5.0}), (3, {R2: 7.0})]


def test_initial_state_dict_seeds_state():
    result = reg_ops_to_reg_state(OPS, initial_state={R3: 9.0, R1: 0})
    assert states(result) == [
        (0, {R3: 9.0, R1: 1.0, R2: 5.0}),
        (2, {R3: 9.0, R1: 2.0, R2: 5.0}),
        (3, {R3: 9.0, R1: 2.0, R2: 7.0}),
    ]


def test_initial_state_is_filtered_by_prefilter():
    result = reg_ops_to_reg_state(
        OPS, initial_state={R3: 9.0, "x->y:z": 4.0}, column_pre_filter=["a->b:r[13]"]
    )
    assert states(result) == [
        (0, {R3: 9.0, R1: 1.0}),
        (2, {R3: 9.0, R1: 2.0}),
    ]


def test_initial_state_taken_from_a_reg_state_at_first_op_time():
    earlier = pd.DataFrame(
        {R3: [1.0, 2.0]},
        index=pd.to_datetime(["2023-12-31 23:59:00", "2024-01-01 00:00:00"]),
    )
    result = reg_ops_to_reg_state([op(5, "r1", 1)], initial_state=earlier)
    assert states(result) == [(5, {R3: 2.0, R1: 1.0})]


def test_name_map_function_renames_columns():
    result = reg_ops_to_reg_state(OPS, name_map_function=lambda t: t[0] + t[2])
    assert states(result) == [
        (0, {"ar1": 1.0, "ar2": 5.0}),
        (2, {"ar1": 2.0, "ar2": 5.0}),
        (3, {"ar1": 2.0, "ar2": 7.0}),
    ]


def test_expiration_with_default_names_never_fires():
    # Expiry is tracked per (src, dst, addr) but compared against renamed
    # columns, so with the default name map nothing ever expires.
    ops = [op(0, "r1", 1), op(1, "r2", 2), op(10, "r2", 3), op(20, "r1", 4)]
    assert states(reg_ops_to_reg_state(ops, value_expiration_sec=5)) == [
        (0, {R1: 1.0}),
        (1, {R1: 1.0, R2: 2.0}),
        (10, {R1: 1.0, R2: 3.0}),
        (20, {R1: 4.0, R2: 3.0}),
    ]


def _identity_rows(ops, **kwargs):
    docs = reg_ops_to_reg_state(
        ops, value_expiration_sec=5, name_map_function=lambda t: t, **kwargs
    )
    r1, r2 = ("a", "b", "r1"), ("a", "b", "r2")
    return [(d["@timestamp"], d.get(r1), d.get(r2)) for d in docs]


def test_expiration_with_identity_names_emits_blanked_document():
    ops = [op(0, "r1", 1), op(4, "r2", 2), op(7, "r2", 3)]
    # r1 was last seen at t=0, so at t=7 it has expired as of t=5
    assert (ts(5), None, 2.0) in _identity_rows(ops)


def test_expiration_with_several_stale_registers_orders_by_last_update():
    # r2 went quiet before r1 did, so r2 expires first despite sorting later
    ops = [op(0, "r2", 2), op(1, "r1", 1), op(10, "r1", 3)]
    r1, r2 = ("a", "b", "r1"), ("a", "b", "r2")
    rows = [
        (d["@timestamp"], d.get(r1), d.get(r2))
        for d in reg_ops_to_reg_state(
            ops, value_expiration_sec=5, name_map_function=lambda t: t
        )
    ]
    assert (ts(5), 1.0, None) in rows
    assert (ts(6), None, None) in rows
    assert rows.index((ts(5), 1.0, None)) < rows.index((ts(6), None, None))
    assert len(rows) == len(set(rows))


def test_presence_op_refreshes_every_register_of_that_device_pair():
    ops = [op(0, "r1", 1), op(4, "PRESENT", 1), op(8, "r2", 2)]
    # without the t=4 refresh r1 (last seen t=0) would have expired by t=8
    assert all(row[1] == 1.0 for row in _identity_rows(ops))


# ------------------------------------------------------------ reg_state_time_bounds


def doc(sec, **regs):
    return {"@timestamp": ts(sec), **regs}


DOCS = [doc(0, a=1.0), doc(10, a=2.0, b=1.0), doc(20, a=3.0, b=2.0)]
A1 = {"a": 1.0}
A2B1 = {"a": 2.0, "b": 1.0}
A3B2 = {"a": 3.0, "b": 2.0}


def bounded(**kwargs):
    return states(reg_state_time_bounds(DOCS, **kwargs))


def test_no_bounds_echoes_input():
    assert bounded() == [(0, A1), (10, A2B1), (20, A3B2)]


def test_start_inside_data_echoes_previous_state_at_start():
    assert bounded(start_timestamp=ts(5)) == [(5, A1), (10, A2B1), (20, A3B2)]
    assert bounded(start_timestamp=ts(15)) == [(15, A2B1), (20, A3B2)]


def test_start_on_first_document_emits_empty_state_first():
    assert bounded(start_timestamp=ts(0)) == [(0, {}), (0, A1), (10, A2B1), (20, A3B2)]


def test_end_truncates_and_pads_final_state():
    assert bounded(end_timestamp=ts(15)) == [(0, A1), (10, A2B1), (15, A2B1)]
    assert bounded(end_timestamp=ts(20)) == [(0, A1), (10, A2B1), (20, A3B2)]
    assert bounded(end_timestamp=ts(25)) == [
        (0, A1),
        (10, A2B1),
        (20, A3B2),
        (25, A3B2),
    ]


def test_start_and_end():
    assert bounded(start_timestamp=ts(5), end_timestamp=ts(15)) == [
        (5, A1),
        (10, A2B1),
        (15, A2B1),
    ]


def test_start_equal_to_end_is_allowed():
    assert bounded(start_timestamp=ts(10), end_timestamp=ts(10)) == [
        (10, A1),
        (10, A2B1),
    ]


def test_start_after_end_is_an_error():
    with pytest.raises(ValueError, match="start_timestamp"):
        bounded(start_timestamp=ts(9), end_timestamp=ts(3))


def test_initial_state_dict_applies_from_start():
    assert bounded(initial_state={"z": 9.0}) == [(0, A1), (10, A2B1), (20, A3B2)]
    assert bounded(initial_state={"z": 9.0}, start_timestamp=ts(5)) == [
        (5, {"a": 1.0, "z": 9.0}),
        (10, {"a": 2.0, "z": 9.0, "b": 1.0}),
        (20, {"a": 3.0, "z": 9.0, "b": 2.0}),
    ]


def test_initial_state_with_start_and_end():
    assert bounded(
        initial_state={"z": 9.0}, start_timestamp=ts(5), end_timestamp=ts(15)
    ) == [
        (5, {"a": 1.0, "z": 9.0}),
        (10, {"a": 2.0, "z": 9.0, "b": 1.0}),
        (15, {"a": 2.0, "z": 9.0, "b": 1.0}),
    ]


def test_initial_state_from_reg_state_frame_uses_start_time():
    frame = pd.DataFrame(
        {"z": [1.0, 2.0]},
        index=pd.to_datetime(["2023-12-31 00:00:00", "2024-01-01 00:00:12"]),
    )
    assert bounded(initial_state=frame, start_timestamp=ts(15)) == [
        (15, {"a": 2.0, "b": 1.0, "z": 2.0}),
        (20, {"a": 3.0, "b": 2.0, "z": 2.0}),
    ]


def test_start_after_all_documents_yields_last_state_at_start():
    assert bounded(start_timestamp=ts(25)) == [(25, A3B2)]
    assert bounded(start_timestamp=ts(25), initial_state={"z": 9.0}) == [
        (25, {"a": 3.0, "b": 2.0, "z": 9.0})
    ]


def test_empty_input_with_bounds():
    empty = lambda **kw: states(reg_state_time_bounds([], **kw))
    assert empty(start_timestamp=ts(5), end_timestamp=ts(8)) == [(5, {}), (8, {})]
    assert empty(start_timestamp=ts(5)) == [(5, {})]
    assert empty(end_timestamp=ts(8)) == [(8, {})]
    assert empty(start_timestamp=ts(5), initial_state={"q": 1}) == [(5, {"q": 1})]
    assert empty() == []


# -------------------------------------------------------------------- get_state_at


@pytest.fixture
def frame():
    return pd.DataFrame(
        {"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]},
        index=pd.to_datetime(
            ["2024-01-01 00:00:00", "2024-01-01 00:00:10", "2024-01-01 00:00:20"]
        ),
    )


def test_state_at_frame_exact_between_before_and_after(frame):
    assert get_state_at(frame, ts(10)) == {"a": 2.0, "b": 5.0}
    assert get_state_at(frame, ts(15)) == {"a": 2.0, "b": 5.0}
    assert get_state_at(frame, ts(59)) == {"a": 3.0, "b": 6.0}
    before = get_state_at(frame, "2023-12-31T00:00:00.000Z")
    assert list(before) == ["a", "b"] and all(np.isnan(v) for v in before.values())


def test_state_at_first_row_and_single_row_frames(frame):
    assert get_state_at(frame, ts(0)) == {"a": 1.0, "b": 4.0}
    assert get_state_at(frame, ts(5)) == {"a": 1.0, "b": 4.0}
    single = frame.iloc[:1]
    assert get_state_at(single, ts(30)) == {"a": 1.0, "b": 4.0}


def test_state_at_empty_frame_is_all_nan():
    empty = pd.DataFrame({"a": []}, index=pd.DatetimeIndex([]))
    assert np.isnan(get_state_at(empty, ts(1))["a"])


def test_state_at_tz_aware_frame_index(frame):
    aware = frame.tz_localize("UTC")
    assert get_state_at(aware, ts(15)) == {"a": 2.0, "b": 5.0}


def test_state_at_document_list():
    assert get_state_at(DOCS, ts(15)) == A2B1
    assert get_state_at(DOCS, ts(20)) == A3B2
    assert get_state_at(DOCS, ts(0)) == A1
    assert get_state_at(DOCS, "2023-01-01T00:00:00Z") == {}


def test_state_at_document_without_timestamp_is_returned_whole():
    assert get_state_at([{"a": 1}], ts(5)) == {"a": 1}


# ------------------------------------------------- _parse_initial_state / normalize


def test_parse_initial_state(frame):
    assert _parse_initial_state(None, datetime.now()) == {}
    assert _parse_initial_state({"a": 1}, datetime.now()) == {"a": 1}
    when = datetime(2024, 1, 1, 0, 0, 12, tzinfo=timezone.utc)
    assert _parse_initial_state(frame, when) == {"a": 2.0, "b": 5.0}


def test_normalize_timestamp_for_index(frame):
    naive = _normalize_timestamp_for_index(ts(5), frame.index)
    assert naive == pd.Timestamp("2024-01-01 00:00:05") and naive.tzinfo is None
    aware = _normalize_timestamp_for_index(ts(5), frame.tz_localize("UTC").index)
    assert str(aware.tz) == "UTC"
    aware_index = frame.tz_localize("UTC").index
    already = _normalize_timestamp_for_index(pd.Timestamp(ts(5)), aware_index)
    assert already == aware
    non_dt = _normalize_timestamp_for_index(ts(5), pd.Index([1, 2]))
    assert non_dt.tzinfo is not None
    assert (
        _normalize_timestamp_for_index(ts(5), pd.DatetimeIndex([])).tzinfo is not None
    )


# ------------------------------------------------------------- to_pandas / load


def test_to_pandas_builds_time_indexed_frame():
    frame = to_pandas(DOCS)
    assert list(frame.columns) == ["a", "b"]
    assert list(frame.index) == [
        pd.Timestamp("2024-01-01 00:00:00"),
        pd.Timestamp("2024-01-01 00:00:10"),
        pd.Timestamp("2024-01-01 00:00:20"),
    ]
    assert frame["a"].tolist() == [1.0, 2.0, 3.0]
    assert np.isnan(frame["b"].iloc[0])


def test_to_pandas_back_populate_fills_leading_nans():
    frame = to_pandas(DOCS, back_populate=True)
    assert frame["b"].tolist() == [1.0, 1.0, 2.0]


def test_to_pandas_passes_dataframe_through():
    frame = pd.DataFrame({"a": [1.0, np.nan]})
    assert to_pandas(frame) is frame
    assert to_pandas(pd.DataFrame({"a": [np.nan, 2.0, 3.0]}), back_populate=True)[
        "a"
    ].tolist() == [2.0, 2.0, 3.0]


def test_to_pandas_empty_input():
    assert to_pandas([]).empty
    assert to_pandas(iter([])).empty


def test_to_pandas_without_timestamps_keeps_default_index():
    frame = to_pandas([{"a": 1}, {"a": 2}])
    assert frame["a"].tolist() == [1, 2]


def test_load_regstate_end_to_end():
    frame = load_regstate(OPS, start_timestamp=ts(1), end_timestamp=ts(5))
    assert frame.index.tolist() == [
        pd.Timestamp("2024-01-01 00:00:01"),
        pd.Timestamp("2024-01-01 00:00:02"),
        pd.Timestamp("2024-01-01 00:00:03"),
        pd.Timestamp("2024-01-01 00:00:05"),
    ]
    assert frame[R1].tolist() == [1.0, 2.0, 2.0, 2.0]
    assert frame[R2].tolist() == [5.0, 5.0, 7.0, 7.0]


def test_load_regstate_rejects_unimplemented_options():
    with pytest.raises(NotImplementedError):
        load_regstate(OPS, reduce_name=True)
    with pytest.raises(NotImplementedError):
        load_regstate(OPS, cache=True)
    with pytest.raises(NotImplementedError):
        load_regstate(OPS, cache_path="x")


# --------------------------------------------------------------- misc helpers


def test_sample_weights():
    frame = pd.DataFrame(
        {"a": [1, 2, 3]},
        index=pd.to_datetime(
            ["2024-01-01 00:00:00", "2024-01-01 00:00:10", "2024-01-01 00:00:25"]
        ),
    )
    assert sample_weights(frame).tolist() == [10.0, 15.0, 0.0]
    assert sample_weights(frame.iloc[0:0]).empty


def test_chunks_splits_iterator():
    assert [list(c) for c in chunks(iter(range(5)), size=2)] == [[0, 1], [2, 3], [4]]


def test_concatenate_preserves_relative_time():
    one = pd.DataFrame(
        {"a": [1, 2]}, index=pd.to_datetime(["2024-01-01", "2024-01-02"])
    )
    two = pd.DataFrame(
        {"a": [3, 4]}, index=pd.to_datetime(["2030-01-01", "2030-01-05"])
    )
    assert concatenate([one]) is one
    joined = concatenate([one, two])
    assert joined["a"].tolist() == [1, 2, 3, 4]
    assert joined.index[2] == pd.Timestamp("2024-01-02")
    assert joined.index[3] == pd.Timestamp("2024-01-06")


def test_concatenate_chains_three_frames_by_relative_time():
    def frame(start, values):
        index = pd.to_datetime(start) + pd.to_timedelta([0, 10], unit="s")
        return pd.DataFrame({"a": values}, index=index)

    joined = concatenate(
        [
            frame("2024-01-01", [1, 2]),
            frame("2030-01-01", [3, 4]),
            frame("2040-01-01", [5, 6]),
        ]
    )
    assert joined["a"].tolist() == [1, 2, 3, 4, 5, 6]
    assert joined.index[1] == pd.Timestamp("2024-01-01 00:00:10")
    assert joined.index[2] == pd.Timestamp("2024-01-01 00:00:10")
    assert joined.index[3] == pd.Timestamp("2024-01-01 00:00:20")
    assert joined.index[5] == pd.Timestamp("2024-01-01 00:00:30")
