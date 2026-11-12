"""
Test functions from parseus.utils package to make sure behaviour is constant/expected.
"""

from datetime import datetime, timezone

import numpy as np
import pytest

from parseus.util import datetime_utils, lazy_iteration, numerical


@pytest.mark.parametrize(
    "dt, expected",
    [
        # standard millisecond‐precision example from the docstring,
        # but note that %f produces 6 digits (microseconds)
        (
            datetime(2017, 8, 28, 19, 37, 43, 354000),
            "2017-08-28T19:37:43.354000Z",
        ),
        # exercise zero‐padded microseconds
        (
            datetime(2021, 12, 31, 23, 59, 59, 1),
            "2021-12-31T23:59:59.000001Z",
        ),
    ],
)
def test_timestamp_input_formats_correctly(dt, expected):
    """
    When a datetime is provided, make_zulu_time should
    ignore kwargs and simply strftime it with %Y-%m-%dT%H:%M:%S.%fZ.
    """
    result = datetime_utils.make_zulu_time(timestamp=dt)
    assert result == expected


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (
            {
                "year": 2020,
                "month": 1,
                "day": 2,
                "hour": 3,
                "minute": 4,
                "second": 5,
                "microsecond": 6,
            },
            "2020-01-02T03:04:05.000006Z",
        ),
        # omit time fields entirely → defaults to midnight and zero micros
        (
            {"year": 2000, "month": 12, "day": 31},
            "2000-12-31T00:00:00.000000Z",
        ),
    ],
)
def test_kwargs_input_constructs_and_formats(kwargs, expected):
    """
    When timestamp is None and kwargs are provided, the function
    should build a datetime(**kwargs) and format it the same way.
    """
    result = datetime_utils.make_zulu_time(**kwargs)
    assert result == expected


def test_no_args_returns_none():
    """
    If neither timestamp nor kwargs are given, make_zulu_time should return None.
    """
    assert datetime_utils.make_zulu_time() is None


def test_timestamp_takes_precedence_over_kwargs():
    """
    Even if kwargs are passed, a non‐None timestamp must win.
    """
    dt = datetime(1999, 9, 9, 9, 9, 9, 999999)
    # these bogus kwargs should be ignored
    result = datetime_utils.make_zulu_time(timestamp=dt, year=2000, month=1, day=1)
    assert result == "1999-09-09T09:09:09.999999Z"


def test_invalid_kwargs_raises_type_error():
    """
    If timestamp is None but kwargs are missing required fields,
    datetime(**kwargs) will raise a TypeError and that should bubble up.
    """
    with pytest.raises(TypeError):
        # missing month and day
        datetime_utils.make_zulu_time(year=2025)


def test_datetime_parser_none_input_returns_none():
    """If you pass in None, you get back None."""
    assert datetime_utils.datetime_parser(None) is None


def test_datetime_parser_datetime_input_idempotent():
    """
    If you pass in a datetime, you get the same object back.
    """
    now = datetime.now(tz=timezone.utc)
    result = datetime_utils.datetime_parser(now)
    # identity check to be sure it's not a copy
    assert result is now


@pytest.mark.parametrize(
    "input_val, expected",
    [
        # string of milliseconds since epoch → treated as ms
        (
            "1609459200000",
            datetime.fromtimestamp(1609459200000 / 1000, tz=timezone.utc),
        ),
        # integer of milliseconds since epoch → treated as ms
        (1609459200000, datetime.fromtimestamp(1609459200000 / 1000, tz=timezone.utc)),
    ],
)
def test_datetime_parser_numeric_string_and_int_ms_timestamps(input_val, expected):
    """
    Numeric strings or ints that, when divided by 1000, give a UTC date
    after 1970-02-01 should be treated as millisecond timestamps.
    """
    result = datetime_utils.datetime_parser(input_val)
    assert result == expected


def test_datetime_parser_small_numeric_string_fallback_to_seconds():
    """
    A small numeric string (ms/1000 < 1970-02-01) must be
    reinterpreted as seconds, not ms.
    """
    small = "1000"
    # since 1000/1000 = 1s → 1970-01-01T00:00:01
    expected = datetime.fromtimestamp(int(small), tz=timezone.utc)
    result = datetime_utils.datetime_parser(small)
    assert result == expected


def test_datetime_parser_float_treated_as_seconds_since_epoch():
    """
    Floats are always treated as seconds since the epoch.
    """
    f = 1609459200.0  # corresponds to 2021-01-01T00:00:00Z
    expected = datetime.fromtimestamp(f, tz=timezone.utc)
    result = datetime_utils.datetime_parser(f)
    assert result == expected


@pytest.mark.parametrize(
    "dt_str, expected",
    [
        # 3-digit fractional seconds → 354 microseconds
        (
            "2017-08-28T19:37:43.354Z",
            datetime(2017, 8, 28, 19, 37, 43, 354000, timezone.utc),
        ),
        # 6-digit fractional seconds → exactly 1 microsecond
        ("2021-01-01T00:00:00.000001Z", datetime(2021, 1, 1, 0, 0, 0, 1, timezone.utc)),
    ],
)
def test_datetime_parser_strings_with_up_to_six_fractional_digits(dt_str, expected):
    """
    Standard Z-formatted strings with 1 to 6 microsecond digits
    should parse directly.
    """
    result = datetime_utils.datetime_parser(dt_str)
    assert result == expected


def test_datetime_parser_string_with_more_than_six_fractional_digits_gets_truncated():
    """
    If the fractional part is longer than 6 digits, it should be
    truncated to 6 digits before parsing.
    """
    # original has 7 fractional digits "1234567"
    long_frac = "2017-08-28T19:37:43.1234567Z"
    # after truncation we expect microsecond = 123456
    expected = datetime(2017, 8, 28, 19, 37, 43, 123456, timezone.utc)
    result = datetime_utils.datetime_parser(long_frac)
    assert result == expected


@pytest.mark.parametrize("bad_str", ["not-a-date"])
def test_datetime_parser_malformed_string_raises_value_error(bad_str):
    """
    Strings that aren’t in the expected Zulu format should bubble
    up the ValueError from datetime.strptime.
    """
    with pytest.raises(ValueError):
        datetime_utils.datetime_parser(bad_str)


def test_datetime_parser_unhandled_type_returns_none():
    """
    Types that aren’t None, datetime, str, int, or float fall
    through and return None.
    """
    assert datetime_utils.datetime_parser([]) is None
    assert datetime_utils.datetime_parser({"foo": "bar"}) is None


def test_flatten_empty_iterable():
    """
    Empty iterable passed lazy_iteration.flatten yields nothing.
    """
    assert list(lazy_iteration.flatten([])) == []


@pytest.mark.parametrize(
    "input_iterable, expected",
    [
        # 1) single‐level list → same elements
        ([1, 2, 3], [1, 2, 3]),
        # 2) nested lists
        ([[1, 2], 3, [4, [5, 6]]], [1, 2, 3, 4, 5, 6]),
        # 3) mix of list and tuple
        ([(1, 2), [3, 4], 5], [1, 2, 3, 4, 5]),
        # 4) nested tuples
        (((1,), 2, (3, (4,))), [1, 2, 3, 4]),
        # 5) strings inside a list are atomic
        (["a", "bc", ["d", "e"]], ["a", "bc", "d", "e"]),
    ],
)
def test_flatten_list_and_tuple_variants(input_iterable, expected):
    "Test that lazy_iteration.flatten deals properly with list and tuple being passed."
    result = list(lazy_iteration.flatten(input_iterable))
    assert result == expected


def test_flatten_string_as_top_level_iterable():
    """
    Test that lazy_iteration.flatten if passed a bare string,
    iterates over its characters.
    """
    assert list(lazy_iteration.flatten("xyz")) == ["x", "y", "z"]


def test_flatten_sets_yields_all_members_but_order_unstable():
    """
    Test that lazy_iteration.flatten flatten sets,
    but because their iteration order is not guaranteed
    we compare via a set plus length check.
    """
    inp = [{1, 2}, 3, {4, 5}]
    out = list(lazy_iteration.flatten(inp))
    # we expect exactly these five elements, in any order
    assert set(out) == {1, 2, 3, 4, 5}
    assert len(out) == 5


def test_flatten_nested_set_tuple_and_string():
    """
    Test that lazy_iteration.flatten works with a mix of set, tuple, nested list,
     and string elements:
     - sets and tuples get flattened
     - strings (even inside lists) are atomic
    """
    inp = [
        {"a", "b"},
        ("c", "d"),
        "ef",
        [{"g"}],
    ]
    out = list(lazy_iteration.flatten(inp))
    # we don't care about the order of 'a','b','g' from sets
    assert set(out) == {"a", "b", "c", "d", "ef", "g"}
    assert len(out) == 6


def test_flatten_non_iterable_objects_pass_through():
    """
    Test that lazy_iteration.flatten works by handling anything that's not
    list/tuple/set (and not a string) by yielding it as-is.
    """

    class Foo:
        pass

    foo = Foo()
    inp = [foo, [foo], ((foo,),)]
    # should yield foo three times
    assert list(lazy_iteration.flatten(inp)) == [foo, foo, foo]


def test_flatten_generator_input():
    """
    Test that lazy_iteration.flatten works with top‐level iterable
    being a generator.
    """

    def gen():
        yield [1, 2]
        yield (3, 4)

    assert list(lazy_iteration.flatten(gen())) == [1, 2, 3, 4]


def test_robust_log_adjust_values_below_threshold_return_same_values():
    """
    Test numerical.robust_log_adjust to handle inputs whose
    absolute value <= log_threshold
    Output should equal the input (no log‐adjustment), and dtype should be float64.
    """
    arr = np.array([-10.0, -5.0, 0.0, 5.0, 10.0])
    result = numerical.robust_log_adjust(arr, log_threshold=10.0)
    np.testing.assert_array_equal(result, arr)
    assert result.dtype == np.float64


@pytest.mark.parametrize(
    "x, thr, base, expected",
    [
        # 1) single value just above threshold with default base e:
        (10.0 + np.e - 1.0, 10.0, np.e, 10.0 + 1.0),
        # 2) negative value just below -threshold:
        (-(10.0 + np.e - 1.0), 10.0, np.e, -(10.0 + 1.0)),
        # 3) custom threshold=5 and log_base=10:
        (5.0 + 100.0 - 1.0, 5.0, 10.0, 5.0 + 2.0),
        # 4) zero threshold (everything goes through the log), base e:
        (np.e - 1.0, 0.0, np.e, 1.0),
    ],
)
def test_robust_log_adjust_values_above_threshold_and_varied_parameters(
    x, thr, base, expected
):
    """
    Test numerical.robust_log_adjust to handle |x| > log_threshold,
    output should be: sign(x) * (log_threshold + log((|x|-log_threshold)+1)/log_base)
    Verify a few parameter combinations.
    """
    arr = np.array([x])
    result = numerical.robust_log_adjust(arr, log_threshold=thr, log_base=base)
    # we expect exactly one element
    assert result.shape == (1,)
    # use allclose since floating‐point log may not be bit‐exact
    assert np.allclose(result[0], expected, rtol=1e-7, atol=0)


def test_robust_log_adjust_multidimensional_array_preserves_shape_and_transforms():
    """
    Test numerical.robust_log_adjust to handle with a 2×2 array
    verify that it keeps its shape and only the >threshold element is log‐adjusted.
    """
    arr = np.array([[0.0, 1.0], [2.0, 20.0]])
    thr = 10.0
    result = numerical.robust_log_adjust(arr, log_threshold=thr)
    assert result.shape == arr.shape
    # manually compute what happens at [1,1]
    expected_11 = thr + np.log((20.0 - thr) + 1.0) / np.log(np.e)
    expected = np.array([[0.0, 1.0], [2.0, expected_11]])
    np.testing.assert_allclose(result, expected, rtol=1e-7)


def test_robust_log_adjust_nan_and_infinite_values_propagate():
    """
    Test numerical.robust_log_adjust to handle
    nan → nan, +inf → +inf, -inf → -inf
    """
    arr = np.array([np.nan, np.inf, -np.inf])
    result = numerical.robust_log_adjust(arr)
    # nan‐positions compare True for isnan
    assert np.isnan(result[0])
    assert np.isposinf(result[1])
    assert np.isneginf(result[2])


def test_robust_log_adjust_integer_input_promotes_to_float_and_transforms():
    """
    Test numerical.robust_log_adjust to handle integer‐typed input, float64 output.
    Also, below‐threshold ints come through unchanged, above‐threshold get logged.
    """
    arr = np.array([0, 5, 15], dtype=int)
    result = numerical.robust_log_adjust(arr, log_threshold=10)
    # dtype promotion
    assert result.dtype == np.float64
    # build expected: first two same, last = 10 + log((15-10)+1) / ln(e)
    expected_last = 10.0 + np.log((15.0 - 10.0) + 1.0) / np.log(np.e)
    expected = np.array([0.0, 5.0, expected_last])
    np.testing.assert_allclose(result, expected, rtol=1e-7)


class DummyWithValues:
    """
    Simple stand in for a Pandas-like object that has a .values attribute.
    """

    def __init__(self, values):
        self.values = values


def test_nanaverage_no_weights_uses_nanmean_and_ignores_nans():
    """
    Test numerical.nanaverage for the case of a simple numpy
    array with a nan in it.
    """
    data = np.array([1.0, np.nan, 3.0, 4.0])
    # baseline using numpy directly
    expected = np.nanmean(data)
    result = numerical.nanaverage(data)
    assert result == expected


@pytest.mark.parametrize(
    "axis, keepdims",
    [
        (0, False),
        (1, True),
    ],
)
def test_nanaverage_no_weights_with_axis_and_keepdims(axis, keepdims):
    """
    Test numerical.nanaverage for multidimensional arrays with
    nans and compare with numpy's version
    """
    data = np.array([[1.0, np.nan, 3.0], [4.0, 5.0, np.nan]])
    # call numpy directly for expected
    expected = np.nanmean(data, axis=axis, keepdims=keepdims)
    result = numerical.nanaverage(data, axis=axis, keepdims=keepdims)
    # shapes must match
    assert result.shape == expected.shape
    # values must match, including NaNs in same positions
    np.testing.assert_allclose(result, expected, equal_nan=True)


def test_nanaverage_data_as_pandas_like_object_converted_to_array():
    """
    Test numerical.nanaverage with a pandas-like object.
    """
    arr = np.array([10.0, np.nan, 30.0])
    dummy = DummyWithValues(arr)
    # should behave exactly like np.nanmean(arr)
    expected = np.nanmean(arr)
    result = numerical.nanaverage(dummy)
    assert result == expected


def test_nanaverage_weights_without_axis_1d_weighted_average():
    """
    Test numerical.nanaverage for case involving weights
    """
    data = np.array([1.0, 2.0, np.nan, 4.0])
    weights = np.array([1.0, 0.5, 2.0, 1.0])
    # manual weighted average ignoring NaNs
    expected = 2.4
    result = numerical.nanaverage(data, weights=weights)
    assert np.allclose(result, expected, equal_nan=True)


def test_nanaverage_weights_as_pandas_like_object_converted_to_array():
    """
    Test numerical.nanaverage with pandas-like object and weights.
    """
    data = np.array([1.0, 2.0, np.nan, 4.0])
    w = np.array([2.0, 2.0, 2.0, 2.0])
    data_dummy = DummyWithValues(data)
    weights_dummy = DummyWithValues(w)
    # weighted average with all weights = 2 is same as unweighted
    expected = 7 / 3
    result = numerical.nanaverage(data_dummy, weights=weights_dummy)
    assert result == expected


def test_nanaverage_weights_sum_zero_produces_nan():
    """
    Test numerical.nanaverage for case of zero weights
    """
    data = np.array([1.0, 2.0, 3.0])
    zero_weights = np.zeros_like(data)
    result = numerical.nanaverage(data, weights=zero_weights)
    # 0/0 -> nan
    assert np.isnan(result)


def test_nanaverage_unbroadcastable_weights_raises_value_error():
    """
    Test numerical.nanaverage for passing additional kwargs,
    verify that they are passed correctly.
    """
    data = np.array([[1.0, 2.0], [3.0, 4.0]])
    # this length‐3 weights array cannot broadcast to shape (2,2)
    bad_weights = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        numerical.nanaverage(data, weights=bad_weights)


def test_nanaverage_kwargs_passed_to_nanmean_and_nansum_dtype():
    """
    Test numerical.nanaverage for passing additional kwargs,
    verify that they are passed correctly.
    """
    data = np.array([[1, 2], [3, 4]], dtype=int)
    # request float32 output by passing dtype to nanmean (no weights)
    expected = np.nanmean(data, dtype=np.float32)
    result = numerical.nanaverage(data, dtype=np.float32)
    assert result.dtype == np.float32
    assert result == expected
    # now with weights: dtype goes to nansum
    weights = np.ones_like(data, dtype=int)
    # weighted sum and weight sum with dtype
    ws = np.nansum(data * weights, dtype=np.float32)  # 10.0 (float32)
    wsum = np.nansum(weights, dtype=np.float32)  #  4.0 (float32)
    expected_wavg = ws / wsum
    result_w = numerical.nanaverage(data, weights=weights, dtype=np.float32)
    assert np.allclose(result_w, expected_wavg)
