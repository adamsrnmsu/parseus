"""Characterization tests for how DSData loads and indexes result blocks."""

from datetime import datetime, timezone

import pytest

from parseus.datastore.data import DSData, _Scroll
from parseus.util.datetime_utils import datetime_parser
from tests.test_datastore_scroll_lifecycle import FakeScrollHost


class Host(FakeScrollHost):
    """Scroll host that can also re-run a time-bounded query."""

    def __init__(self, documents, page_size=2):
        super().__init__(documents, page_size)
        self.entry_queries = []

    def get_all_entries(self, index, query=None, sort=None):
        self.entry_queries.append(query)
        return list(self._documents)


def ts(i):
    return f"2024-01-01T00:00:{i:02d}.000Z"


# t0 | t1 t1 | t2 | t3 -> blocks only split between distinct timestamps
DOCS = [{"@timestamp": ts(t), "n": n} for n, t in enumerate([0, 1, 1, 2, 3])]


def test_blocks_never_split_documents_sharing_a_timestamp():
    data = DSData(Host(DOCS), "idx", query={"bool": {}}, block_size=2)
    assert list(data) == DOCS
    assert len(data) == 5
    assert [data[i]["n"] for i in range(5)] == [0, 1, 2, 3, 4]
    assert data[-1] == DOCS[-1]
    assert bool(data)


def test_block_sizes_become_known_after_iterating():
    data = DSData(Host(DOCS), "idx", query={"bool": {}}, block_size=2)
    assert not data.block_sizes_known
    list(data)
    assert data.block_sizes_known


def test_indexing_past_the_end_raises():
    data = DSData(Host(DOCS), "idx", query={"bool": {}}, block_size=2)
    with pytest.raises(IndexError):
        data[10]
    empty = DSData(Host([]), "idx", query={"bool": {}})
    with pytest.raises(IndexError):
        empty[-1]


def test_start_and_end_timestamps():
    data = DSData(Host(DOCS), "idx", query={"bool": {}}, block_size=2)
    assert data.start_timestamp().second == 0
    assert data.end_timestamp().second == 3
    fresh = DSData(Host(DOCS), "idx", query={"bool": {}}, block_size=2)
    assert fresh.end_timestamp().second == 3


def test_end_timestamp_uses_known_block_bounds():
    data = DSData(Host(DOCS), "idx", query={"bool": {}}, block_size=2)
    list(data)
    assert data.end_timestamp().second == 3


def test_empty_dataset_has_no_timestamps():
    data = DSData(Host([]), "idx", query={"bool": {}})
    assert data.start_timestamp() is None
    assert data.end_timestamp() is None
    list(data)
    assert data.end_timestamp() is None
    assert not data


def test_oversized_pages_are_trimmed_to_known_block_size():
    host = Host(DOCS, page_size=2)
    data = DSData(host, "idx", query={"bool": {}}, block_size=2)
    list(data)  # block 0 is known to hold exactly one document
    host._page_size = 10  # the index changed: pages are now bigger
    data._get_block.cache_clear()
    assert data._get_block_no_cache(0) == [DOCS[0]]


def test_unscrolled_later_block_is_reached_by_scrolling_up_to_it():
    data = DSData(Host(DOCS), "idx", query={"bool": {}}, block_size=2)
    assert data._get_block_no_cache(2) == [DOCS[3]]


def test_known_block_without_a_scroll_is_requeried_by_time_bounds():
    host = Host(DOCS)
    query = {
        "bool": {"must": [{"range": {"@timestamp": {"gte": "x"}}}, {"term": {"a": 1}}]}
    }
    data = DSData(host, "idx", query=query, block_size=2)
    list(data)
    data._get_block.cache_clear()
    block = data._get_block_no_cache(1)
    assert block == DOCS  # fake returns everything; the point is the query used
    sent = host.entry_queries[-1]
    must = sent["bool"]["must"]
    assert must[0]["range"]["@timestamp"]["gte"].second == 1
    assert must[0]["range"]["@timestamp"]["lte"].second == 1
    assert must[1] == {"term": {"a": 1}}
    # the stored query is not modified
    assert query["bool"]["must"][0]["range"]["@timestamp"] == {"gte": "x"}


def test_requery_wraps_non_bool_and_missing_queries():
    host = Host(DOCS)
    data = DSData(host, "idx", query={"term": {"a": 1}}, block_size=2)
    list(data)
    data._block_timestamp_bounds[1] = (ts(1), ts(2))
    data._get_block_no_cache(1)
    assert host.entry_queries[-1]["bool"]["must"][0] == {"term": {"a": 1}}
    assert "range" in host.entry_queries[-1]["bool"]["must"][1]

    bare = DSData(host, "idx", query=None, block_size=2)
    list(bare)
    bare._get_block_no_cache(1)
    assert "range" in host.entry_queries[-1]


def test_requery_tolerates_non_list_bool_clauses():
    host = Host(DOCS)
    query = {
        "bool": {
            "must": [{"term": {"a": 1}}, {"range": {"@timestamp": {"gte": "x"}}}],
            "minimum_should_match": 1,
        }
    }
    data = DSData(host, "idx", query=query, block_size=2)
    list(data)
    data._get_block(1)  # primes nothing; just ensure bounds are known
    data._get_block.cache_clear()
    assert data._get_block_no_cache(1) == DOCS
    sent = host.entry_queries[-1]["bool"]
    assert sent["minimum_should_match"] == 1
    assert sent["must"][1]["range"]["@timestamp"] == _block_bounds(data, 1)


def _block_bounds(data, block):
    start, end = data._block_timestamp_bounds[block]
    return {"gte": datetime_parser(start), "lte": datetime_parser(end)}


def test_requery_replaces_dict_form_range_clause_with_block_bounds():
    host = Host(DOCS)
    query = {"bool": {"filter": {"range": {"@timestamp": {"gte": "x"}}}}}
    data = DSData(host, "idx", query=query, block_size=2)
    list(data)
    data._get_block.cache_clear()
    data._get_block_no_cache(1)
    sent = host.entry_queries[-1]["bool"]["filter"]["range"]["@timestamp"]
    assert sent == _block_bounds(data, 1)
    assert query["bool"]["filter"]["range"]["@timestamp"] == {"gte": "x"}


def test_known_empty_block_returns_nothing_without_querying():
    host = Host(DOCS)
    data = DSData(host, "idx", query={"bool": {}}, block_size=2)
    data._block_timestamp_bounds[2] = (None, None)
    assert data._get_block_no_cache(2) == []
    assert host.entry_queries == []


def test_no_new_scroll_is_started_at_the_scroll_limit():
    host = Host(DOCS)
    data = DSData(host, "idx", query={"bool": {}}, block_size=2)
    list(data)
    data._scrolls = [
        _Scroll(f"s{i}", 99, datetime.now(timezone.utc))
        for i in range(DSData.MAX_SCROLLS)
    ]
    starts = host.start_scroll_calls
    data._get_block.cache_clear()
    assert data._get_block_no_cache(0) == DOCS  # re-queried by time bounds
    assert host.start_scroll_calls == starts


# ------------------------------------------------------------------------ aggs


class AggHost:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_aggs(self, index, aggs, query=None):
        import copy

        self.calls.append(copy.deepcopy(aggs))
        return self.pages.pop(0) if self.pages else {}


def test_plain_aggregation_returns_the_aggregation_block():
    host = AggHost([{"aggregations": {"avg": {"value": 3}}}])
    data = DSData(host, "idx", aggs={"avg": {"avg": {"field": "x"}}})
    assert list(data) == [{"avg": {"value": 3}}]
    assert len(data) == 1


def test_composite_aggregation_pages_with_after_key():
    def page(buckets, after=None):
        agg = {"buckets": buckets}
        if after:
            agg["after_key"] = after
        return {"aggregations": {"c": agg}}

    B = [{"k": i} for i in (1, 2, 3)]
    host = AggHost([page(B[:2], "k2"), page(B[2:], "k3"), page([])])
    composite = {"aggs": {"c": {"composite": {"sources": []}}}}
    data = DSData(host, "idx", composite_agg=composite, block_size=2)
    assert list(data) == B
    assert "after" not in host.calls[0]["c"]["composite"]
    assert host.calls[1]["c"]["composite"]["after"] == "k2"
    assert host.calls[2]["c"]["composite"]["after"] == "k3"
    assert len(data) == 3
