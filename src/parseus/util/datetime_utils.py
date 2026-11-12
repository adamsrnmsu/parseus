"Utilities for managing datetimes"

from datetime import datetime, timezone

DATETIME_FMT_STR = "%Y-%m-%dT%H:%M:%S.%fZ"


def make_zulu_time(timestamp: datetime = None, **kwargs) -> str:
    """
    Converts either a datetime object or a list of date parameters to a
    string in the same format as accepted by Elasticsearch, e.g. "2017-08-28T19:37:43.354Z".

    :param datetime timestamp: A datetime object. If not specified, the fields in `kwargs`
                               will be used.
    :param kwargs: Passed to the constructor of `datetime.datetime`. Only used if
                   :code:`timestamp is None`.
    :return: zulu time example - 2017-08-28T19:37:43.354Z
    """
    # (year, month=01, day=01, hour=00, min=00,second=00, millisecond=000):
    if timestamp is None and not kwargs:
        # no input was passed
        return None

    if timestamp:
        return timestamp.strftime(DATETIME_FMT_STR)

    return datetime(**kwargs).strftime(DATETIME_FMT_STR)


_LEGACY_FORMATS = [
    DATETIME_FMT_STR,
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _as_utc(dt: datetime) -> datetime:
    """Return ``dt`` in UTC, assuming naive datetimes are already UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_epoch(value) -> datetime:
    n = int(value)
    dt_ms = datetime.fromtimestamp(n / 1000, tz=timezone.utc)
    if dt_ms > datetime(1970, 2, 1, tzinfo=timezone.utc):
        # assume it's a timestamp with milliseconds
        return dt_ms

    # it must not have milliseconds/microseconds, so return the time from the
    # timestamp as-is
    return datetime.fromtimestamp(n, tz=timezone.utc)


def _truncate_fraction(text: str) -> str:
    """Cut fractional seconds to the 6 digits that ``%f`` supports."""
    if "." not in text:
        return text
    dot = text.index(".")
    digits = 0
    # stop at first non-digit (e.g., 'Z')
    while dot + 1 + digits < len(text) and text[dot + 1 + digits].isdigit():
        digits += 1
    if digits <= 6:
        return text
    return text[: dot + 1 + 6] + text[dot + 1 + digits :]


def _parse_datetime_string(text: str) -> datetime:
    s = text.strip()

    # Allow ISO8601 with trailing Z by converting to an offset that fromisoformat understands
    # e.g. "2017-08-28T19:37:43.354Z" -> "2017-08-28T19:37:43.354+00:00"
    s_iso = s[:-1] + "+00:00" if s.endswith("Z") else s

    # First try the modern parser (handles offsets like +00:00)
    try:
        return _as_utc(datetime.fromisoformat(s_iso))
    except ValueError:
        pass

    # Fall back to legacy format list (assume strings without offset are UTC)
    s_try = _truncate_fraction(s)
    for fmt in _LEGACY_FORMATS:
        try:
            return _as_utc(datetime.strptime(s_try, fmt))
        except ValueError:
            continue
    raise ValueError(
        f"Date string '{text}' does not match any of the expected formats."
    )


def datetime_parser(datetime_str: str) -> datetime:
    """
    Parses a string representation of a datetime (e.g. "2017-08-28T19:37:43.354Z")
    into a datetime object.

    This is the inverse of :func:`make_zulu_time`.

    :param str datetime_str: The string representation of the datetime object. It can
                             also be any other object that can be parsed into a
                             `datetime.datetime` object, such as a unix timestamp or
                             `numpy.datetime64`.
    :return: The parsed `datetime.datetime`
    """
    if not datetime_str:
        return None

    if isinstance(datetime_str, datetime):
        return _as_utc(datetime_str)

    if (isinstance(datetime_str, str) and datetime_str.isnumeric()) or isinstance(
        datetime_str, int
    ):
        return _parse_epoch(datetime_str)

    if isinstance(datetime_str, float):
        return datetime.fromtimestamp(datetime_str, tz=timezone.utc)

    if isinstance(datetime_str, str):
        return _parse_datetime_string(datetime_str)

    return None  # not parseable
