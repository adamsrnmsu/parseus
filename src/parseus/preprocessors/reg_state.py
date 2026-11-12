"""
Reads in from a file or from elastic database, and creates a pandas dataframe.
The pandas dataframe represents the "state" of the network over time.
"""

import re
import time
from datetime import timedelta
from functools import lru_cache
from itertools import chain, islice
from sys import getsizeof

import numpy as np
import pandas as pd

from parseus.preprocessors import LOGGER
from parseus.preprocessors.reg_ops import ADDRESS, DESTINATION, SOURCE, TIMESTAMP, VALUE
from parseus.preprocessors.transform import wrap_generator_function
from parseus.util.datetime_utils import datetime_parser, make_zulu_time
from parseus.util.lazy_iteration import flatten


def load_regstate(
    docs_or_file,
    back_populate=False,
    start_timestamp=None,
    end_timestamp=None,
    reduce_name=False,
    column_pre_filter=None,
    initial_state=None,
    cache=False,
    cache_path=None,
):
    """
    Create a Pandas dataframe and then convert each register operation in the
    set into a pd.DataFrame row

    :param docs_or_file: Either a list of dictionaries containing the register
        operations, or the path to the register operations JSON file
    :param back_populate: True to replace all NaNs with their next value, or
        False to leave them as NaN
    :param start_timestamp: The timestamp at which to start saving data
    :param end_timestamp: The timestamp at which to stop saving data
    :param reduce_name: True to reduce the names with simple_name() or False to
        leave them as-is
    :param column_pre_filter: A callback or regex to determine which columns
        are kept and which are discarded, applied before the name simplification
        (if applicable). If not specified, all columns are kept. It can also be
        a list of such filters, where a register must pass all filters
    :param initial_state: A dictionary containing the initial state of the
        registers
    :param cache: True to create or use a cache file for this reg_state
    :param cache_path: If 'cache' is True, the path to the cache file. If it
        does not exist, it will be created. Otherwise, it will be read.
    :return: The Pandas dataframe that represents the network's state over time
    """

    # check the unimplemented parameters
    if reduce_name:
        raise NotImplementedError("'reduce_name'")
    if cache or cache_path:
        raise NotImplementedError("'cache'")

    reg_ops = docs_or_file

    # Lazily convert regops into a reg_state objects
    state_documents = reg_ops_to_reg_state(
        reg_ops, column_pre_filter=column_pre_filter, initial_state=initial_state
    )
    state_documents = reg_state_time_bounds(
        state_documents,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        initial_state=initial_state,
    )
    # Now iterates through docs which will call block from elastic search (if necessary),
    # transform data.
    return to_pandas(state_documents, back_populate=back_populate)


def _parse_initial_state(initial_state, target_time):
    if initial_state is None:
        return {}
    if not hasattr(initial_state, "items") or isinstance(initial_state, pd.DataFrame):
        # it must be another reg_state
        initial_state = get_state_at(initial_state, target_time)
    return initial_state


def _normalize_timestamp_for_index(timestamp, index):
    """
    Normalizes a timestamp so it can be compared to a pandas DatetimeIndex.

    ``datetime_parser`` assumes naive datetimes are UTC and returns timezone-aware
    values. Some reg_state DataFrames intentionally store a timezone-naive UTC
    index, so convert the lookup timestamp to match the index before doing pandas
    comparisons/searches.
    """
    timestamp = pd.Timestamp(datetime_parser(timestamp))

    if not isinstance(index, pd.DatetimeIndex) or index.empty:
        return timestamp

    if index.tz is None:
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("UTC").tz_localize(None)
        return timestamp

    if timestamp.tzinfo is None:
        return timestamp.tz_localize(index.tz)

    return timestamp.tz_convert(index.tz)


def _default_name_map(column):
    return "%s->%s:%s" % column


def _as_filter_list(column_pre_filter):
    """Normalize a filter, or (nested) list of filters, to a flat list."""
    if callable(column_pre_filter) or isinstance(column_pre_filter, str):
        return [column_pre_filter]
    return list(flatten(column_pre_filter))


def _apply_pre_filters(column_pre_filter, columns):
    """Return the sorted columns that pass every pre-filter."""
    if not column_pre_filter:
        return sorted(columns)
    matching = [
        set(filter(f if callable(f) else re.compile(f).match, columns))
        for f in _as_filter_list(column_pre_filter)
    ]
    return sorted(set.intersection(*matching))


def _dedupe_by_timestamp(documents):
    """Drop a document whose timestamp equals the next document's."""
    kept = [
        doc
        for doc, next_doc in zip(documents, documents[1:])
        if doc["@timestamp"] != next_doc["@timestamp"]
    ]
    return kept + documents[-1:]


class _RegStateBuilder:
    """Folds register operations into a stream of state documents."""

    def __init__(
        self, column_pre_filter, initial_state, value_expiration_sec, name_map_function
    ):
        self.column_pre_filter = column_pre_filter
        self.initial_state = initial_state
        self.expiration_sec = value_expiration_sec
        self.name_map = lru_cache(maxsize=32768)(name_map_function or _default_name_map)
        self.current_state = {}
        self.reg_last_updated = {}  # map register -> last update timestamp
        self.last_time_updated = None
        self.parsed_time = None
        self._parsed_cache = None  # (raw timestamp, parsed timestamp)
        self._accepted = {}  # original column -> passes the pre-filters

    def _parse_time(self, raw):
        if not self._parsed_cache or raw != self._parsed_cache[0]:
            self._parsed_cache = (raw, datetime_parser(raw))
        return self._parsed_cache[1]

    def _accepts(self, original_col):
        if original_col not in self._accepted:
            self._accepted[original_col] = bool(
                _apply_pre_filters(
                    self.column_pre_filter, [self.name_map(original_col)]
                )
            )
        return self._accepted[original_col]

    def _load_initial_state(self):
        initial = _parse_initial_state(self.initial_state, self.parsed_time)
        keep = set(_apply_pre_filters(self.column_pre_filter, initial))
        self.current_state = {k: float(v) for k, v in initial.items() if k in keep}
        self.initial_state = None

    def _state_doc(self, timestamp):
        return dict(self.current_state, **{"@timestamp": make_zulu_time(timestamp)})

    def _expire(self):
        """Blank out stale registers, returning the documents that say so."""
        expired = {
            k: v
            for k, v in self.reg_last_updated.items()
            if self.current_state.get(k) is not None
            and (self.parsed_time - v).total_seconds() > self.expiration_sec
        }
        documents = []
        for register, reg_update in sorted(expired.items(), key=lambda x: x[1]):
            self.current_state[self.name_map(register)] = None
            documents.append(
                self._state_doc(reg_update + timedelta(seconds=self.expiration_sec))
            )
        return _dedupe_by_timestamp(documents) if documents else []

    def _refresh_device(self, original_col):
        """A device answered on a protocol: none of its registers have expired."""
        for this_col in self.reg_last_updated:
            if this_col[:2] == original_col[:2]:
                self.reg_last_updated[this_col] = self.parsed_time

    def _apply_change(self):
        """Apply a changed value, returning documents emitted by the change."""
        emitted = self._expire() if self.expiration_sec is not None else []
        if self.parsed_time > self.last_time_updated:
            # the time has moved forward - emit the old state
            emitted.append(self._state_doc(self.last_time_updated))
            self.last_time_updated = self.parsed_time
        return emitted

    def feed(self, doc):
        """Consume one register operation, returning documents to emit."""
        original_col = (doc[SOURCE], doc[DESTINATION], doc[ADDRESS])
        if not self._accepts(original_col):
            return []
        value = doc[VALUE]
        if isinstance(value, str) and not value.isnumeric():
            # ignore non-numeric values for now
            return []

        self.parsed_time = self._parse_time(doc[TIMESTAMP])
        if self.last_time_updated is None:
            self.last_time_updated = self.parsed_time
        if self.initial_state is not None:
            self._load_initial_state()

        emitted = []
        col = self.name_map(original_col)
        if self.current_state.get(col) != float(value):
            emitted = self._apply_change()
            self.current_state[col] = float(value)
        self.reg_last_updated[original_col] = self.parsed_time
        if doc[ADDRESS] == "PRESENT" and self.expiration_sec is not None:
            self._refresh_device(original_col)
        return emitted

    def finish(self):
        """Return the final state document(s)."""
        documents = []
        if self.last_time_updated is not None:
            documents.append(self._state_doc(self.last_time_updated))
        if self.parsed_time is not None and self.parsed_time != self.last_time_updated:
            documents.append(self._state_doc(self.parsed_time))
        return documents


@wrap_generator_function
def reg_ops_to_reg_state(
    reg_ops,
    column_pre_filter=None,
    initial_state=None,
    value_expiration_sec=None,
    name_map_function=None,
    **kwargs,
):
    builder = _RegStateBuilder(
        column_pre_filter, initial_state, value_expiration_sec, name_map_function
    )
    for doc in reg_ops:
        yield from builder.feed(doc)
    yield from builder.finish()


def _frame_state_at(reg_state, timestamp):
    timestamp = _normalize_timestamp_for_index(timestamp, reg_state.index)

    if reg_state.shape[0] == 0 or timestamp < reg_state.index[0]:
        # no state is available or this is before the beginning
        return {column: np.nan for column in reg_state.columns}

    if timestamp in reg_state.index:
        return reg_state.loc[timestamp, :].to_dict()

    index = max(0, reg_state.index.searchsorted(timestamp) - 1)
    return reg_state.iloc[index].to_dict()


def _documents_state_at(reg_state, timestamp):
    timestamp = make_zulu_time(timestamp)
    result = {}
    for item in reg_state:
        if "@timestamp" not in item:
            return item
        if item["@timestamp"] > timestamp:
            break
        result = item
    return {k: v for k, v in result.items() if k != "@timestamp"}


def get_state_at(reg_state, timestamp):
    """
    Gets the state at any time during a capture

    :param reg_state: The reg_state frame or document list
    :param timestamp: The timestamp at which to retrieve the state. If it does
        not exist in the reg_state, the previous known state is returned
    :return: A dictionary of register => value
    """
    timestamp = datetime_parser(timestamp)
    if isinstance(reg_state, pd.DataFrame):
        return _frame_state_at(reg_state, timestamp)
    # it must be a reg_state document list
    return _documents_state_at(reg_state, timestamp)


def _zulu_bounds(start_timestamp, end_timestamp):
    """Convert the bounds to zulu strings and check they are ordered."""
    start, end = (
        make_zulu_time(datetime_parser(x)) for x in (start_timestamp, end_timestamp)
    )
    if None not in (start, end) and start > end:
        raise ValueError("'start_timestamp' must be <= 'end_timestamp'")
    return start, end


def _start_document(last_item, initial_state, start_timestamp):
    """Build the document that pins the state at ``start_timestamp``.

    :return: The document and the (resolved) initial state
    """
    if initial_state is not None:
        initial_state = _parse_initial_state(initial_state, start_timestamp)
    document = dict(
        last_item, **(initial_state or {}), **{"@timestamp": start_timestamp}
    )
    return document, initial_state


def _end_documents(last_item, end_timestamp):
    """Documents needed to pad the series out to ``end_timestamp``."""
    if end_timestamp is None:
        return []
    if last_item and datetime_parser(last_item["@timestamp"]) >= datetime_parser(
        end_timestamp
    ):
        return []
    return [dict(last_item, **{"@timestamp": end_timestamp})]


def _bounded_states(reg_state_list, start_timestamp, end_timestamp, initial_state):
    need_to_echo_start = start_timestamp is not None
    last_item = {}
    for item in reg_state_list:
        if start_timestamp is not None and item["@timestamp"] < start_timestamp:
            # ignore this
            last_item = dict(
                item,
                **_parse_initial_state(
                    initial_state, datetime_parser(item["@timestamp"])
                ),
            )
            continue
        if need_to_echo_start:
            last_item, initial_state = _start_document(
                last_item, initial_state, start_timestamp
            )
            yield last_item
            need_to_echo_start = False
        if end_timestamp is not None and item["@timestamp"] > end_timestamp:
            # we're done now
            break

        last_item = dict(last_item, **item)
        yield last_item

    # if we never echoed the start, do it now
    if need_to_echo_start:
        last_item, initial_state = _start_document(
            last_item, initial_state, start_timestamp
        )
        yield last_item

    yield from _end_documents(last_item, end_timestamp)


@wrap_generator_function
def reg_state_time_bounds(
    reg_state_list, start_timestamp=None, end_timestamp=None, initial_state=None
):
    """
    Bounds a reg_state document list by a start and end timestamp

    :param reg_state_list: The reg_state document list or iterable
    :param start_timestamp: The timestamp to start the resulting document list
    :param end_timestamp: The timestamp to end the resulting document list
    :param initial_state: The state that the registers could take on at the
        start. This could either be a dictionary of register => initial value,
        or a reg_state object (list of reg_state documents or pd.DataFrame)
        which has multiple states, and the relevant one will be used.
    :return: A new iterable whose first document has timestamp 'start_timestamp'
        and whose last document has timestamp 'end_timestamp'
    """
    if start_timestamp is None and end_timestamp is None and initial_state is None:
        # no change. just echo back what we had before
        return iter(reg_state_list)
    start, end = _zulu_bounds(start_timestamp, end_timestamp)
    return _bounded_states(reg_state_list, start, end, initial_state)


def _documents_to_frame(state_document_list):
    """Build the time-indexed frame, or None if there are no documents."""
    document_iterator = iter(state_document_list)
    try:
        chunk = next(document_iterator)
    except StopIteration:
        LOGGER.warning("Received and empty list or generator")
        return None
    result = pd.DataFrame([chunk])
    start_time = time.time()
    for chunk in chunks(document_iterator, size=2000):
        result = pd.concat([result, pd.DataFrame(chunk)])
    LOGGER.info(f"Dataframe size: {getsizeof(result)}, Shape: {result.shape}")
    LOGGER.debug(f"Took {time.time()-start_time} to chunk through data")
    LOGGER.debug(f"Column List: {result.columns}")
    if "@timestamp" in result.columns:
        result.set_index(
            pd.DatetimeIndex(result["@timestamp"]).tz_localize(None), inplace=True
        )
    return result[sorted(set(result.columns).difference({"@timestamp"}))]


def _back_populate(result):
    for column in result.columns:
        first_valid = result[column].first_valid_index()
        if first_valid is not None:
            result.loc[:first_valid, column] = result.loc[first_valid, column]


def to_pandas(state_document_list, back_populate=False):
    """
    Converts a reg_state_list to a Pandas DataFrame

    :param state_document_list: The list of reg_state documents
    :param back_populate: Whether to fill NaN values at the beginning of the
        reg_state with the first non-NaN values they took on
    :return: The Pandas DataFrame that represents this reg_state
    """
    if isinstance(state_document_list, pd.DataFrame):
        result = state_document_list
    else:
        result = _documents_to_frame(state_document_list)
        if result is None:
            return pd.DataFrame()

    if back_populate:
        _back_populate(result)

    return result


def sample_weights(reg_state):
    """
    Creates a new series for the reg_state dataframe where each row is given a
    sample weight: the number of seconds during which this row was the current
    configuration (the difference between the current time and the next
    timestamp in the reg_state dataframe)

    :param reg_state: The reg_state dataframe
    :return: A series with the same index as reg_state, where each item is the
        sample weight of a row
    """
    if reg_state.shape[0] == 0:
        return pd.Series([], [], name="weights", dtype="float64")
    values = reg_state.index.values.astype("datetime64[ns]")
    deltas = list(values[1:] - values[:-1]) + [0]

    # pylint is broken and thinks 'np.maximum' doesn't return anything
    # pylint: disable=assignment-from-no-return
    deltas = np.maximum(np.array(deltas, dtype=float) / 1e9, 0)

    return pd.Series(deltas, reg_state.index, name="weights")


def chunks(iterator, size=10):
    """
    Takes an iterator and wraps it with a generator that gives back
    size number of elements for each iteration.
    :param iterator: The iterator to walk through
    :param size: The number of elements
    """
    for first in iterator:
        yield chain([first], islice(iterator, size - 1))


def concatenate(state_list):
    """
    Concatenates a list of one or more reg_states together. The absolute
    timestamps are not preserved for reg_states 1, ..., n-1, but the relative
    time differences between each state within an individual reg_state are
    preserved

    :param state_list: An iterable of reg_state DataFrames
    :return: A new DataFrame containing the concatenated states
    """
    state_list = list(state_list)
    if len(state_list) == 1:
        return state_list[0]
    relative_frames = [state_list[0]]
    for state in state_list[1:]:
        time_diff = state.index[0] - relative_frames[-1].index[-1]
        new_index = [value - time_diff for value in state.index]
        relative_frames.append(
            pd.DataFrame(data=state.values, columns=state.columns, index=new_index)
        )
    concatenated = pd.concat(relative_frames)
    return concatenated
