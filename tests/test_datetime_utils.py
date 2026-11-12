"""Characterization tests for datetime parsing and formatting."""

from datetime import datetime, timedelta, timezone

import pytest

from parseus.util.datetime_utils import datetime_parser, make_zulu_time

UTC = timezone.utc


def test_make_zulu_time_from_datetime_and_kwargs():
    assert make_zulu_time(datetime(2017, 8, 28, 19, 37, 43, 354000)) == (
        "2017-08-28T19:37:43.354000Z"
    )
    assert make_zulu_time(year=2020, month=2, day=3) == "2020-02-03T00:00:00.000000Z"
    assert make_zulu_time() is None


@pytest.mark.parametrize("empty", [None, "", 0, 0.0])
def test_falsy_input_is_none(empty):
    assert datetime_parser(empty) is None


def test_unsupported_type_is_none():
    assert datetime_parser([1, 2]) is None


def test_naive_datetime_assumed_utc():
    assert datetime_parser(datetime(2024, 1, 2, 3, 4, 5)) == datetime(
        2024, 1, 2, 3, 4, 5, tzinfo=UTC
    )


def test_aware_datetime_converted_to_utc():
    eastern = timezone(timedelta(hours=-5))
    parsed = datetime_parser(datetime(2024, 1, 2, 3, 0, tzinfo=eastern))
    assert parsed == datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "value",
    [1700000000123, "1700000000123"],
)
def test_millisecond_epoch(value):
    assert datetime_parser(value) == datetime(
        2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC
    )


@pytest.mark.parametrize("value", [1700000000, "1700000000"])
def test_second_epoch(value):
    # too small to be milliseconds past Jan 1970, so read as seconds
    assert datetime_parser(value) == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_float_is_epoch_seconds():
    assert datetime_parser(1700000000.5) == datetime(
        2023, 11, 14, 22, 13, 20, 500000, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "2017-08-28T19:37:43.354Z",
            datetime(2017, 8, 28, 19, 37, 43, 354000, tzinfo=UTC),
        ),
        ("  2017-08-28T19:37:43Z  ", datetime(2017, 8, 28, 19, 37, 43, tzinfo=UTC)),
        ("2017-08-28T19:37:43", datetime(2017, 8, 28, 19, 37, 43, tzinfo=UTC)),
        ("2017-08-28 19:37:43", datetime(2017, 8, 28, 19, 37, 43, tzinfo=UTC)),
        ("2017-08-28T19:37", datetime(2017, 8, 28, 19, 37, tzinfo=UTC)),
        ("2017-08-28", datetime(2017, 8, 28, tzinfo=UTC)),
        ("2017-08-28T19:37:43+02:00", datetime(2017, 8, 28, 17, 37, 43, tzinfo=UTC)),
        (
            "2017-08-28T19:37:43.1234567Z",
            datetime(2017, 8, 28, 19, 37, 43, 123456, tzinfo=UTC),
        ),
    ],
)
def test_iso_strings(text, expected):
    parsed = datetime_parser(text)
    assert parsed == expected
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "text,expected",
    [
        # fromisoformat rejects unpadded fields; the legacy formats accept them
        ("2024-1-5", datetime(2024, 1, 5, tzinfo=UTC)),
        ("2024-1-5T3:4", datetime(2024, 1, 5, 3, 4, tzinfo=UTC)),
        ("2024-1-5T3:4:5", datetime(2024, 1, 5, 3, 4, 5, tzinfo=UTC)),
        ("2024-1-5T3:4:5Z", datetime(2024, 1, 5, 3, 4, 5, tzinfo=UTC)),
        ("2024-1-5T3:4:5.25Z", datetime(2024, 1, 5, 3, 4, 5, 250000, tzinfo=UTC)),
        (
            "2024-1-5T3:4:5.1234567Z",
            datetime(2024, 1, 5, 3, 4, 5, 123456, tzinfo=UTC),
        ),
    ],
)
def test_legacy_formats(text, expected):
    assert datetime_parser(text) == expected


@pytest.mark.parametrize("bad", ["not a date", "2024/01/01", "2024-13-45"])
def test_unparseable_string_raises(bad):
    with pytest.raises(ValueError, match="does not match any of the expected formats"):
        datetime_parser(bad)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2024-01-01", "2024-01-01"),
        ("2024-01-01T00:00:00.", "2024-01-01T00:00:00."),
        ("t.12345", "t.12345"),
        ("t.123456", "t.123456"),
        ("t.1234567", "t.123456"),
        ("t.123456789Z", "t.123456Z"),
        ("a.1234567b.9999999", "a.123456b.9999999"),
    ],
)
def test_truncate_fraction_keeps_six_digits(text, expected):
    from parseus.util.datetime_utils import _truncate_fraction

    assert _truncate_fraction(text) == expected


def test_epoch_exactly_one_month_past_1970_is_read_as_seconds():
    # 2678400000 ms is exactly 1970-02-01; only strictly later counts as ms
    assert datetime_parser(2678400000) == datetime.fromtimestamp(2678400000, tz=UTC)
    assert datetime_parser(2678400001).year == 1970
