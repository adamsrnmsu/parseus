"""Tests for datastore query construction and push/pull helpers."""

from datetime import datetime, timezone

import pytest

from parseus.datastore import query_api
from parseus.datastore.data import DSData


class FakeClient:
    def __init__(self):
        self.deleted = []
        self.pushed = []

    def delete_by_query(self, index, query):
        self.deleted.append((index, query))

    def bulk_push_docs(self, index, docs):
        self.pushed.append((index, list(docs)))


@pytest.fixture
def client():
    c = FakeClient()
    from copy import deepcopy

    original = c.delete_by_query
    c.delete_by_query = lambda index, query: original(index, deepcopy(query))
    return c


DOCS = [
    {"@timestamp": "2024-01-02", "host": "a"},
    {"@timestamp": "2024-01-01", "host": "b"},
    {"@timestamp": "2024-01-03", "host": "a"},
]


def test_push_deletes_from_earliest_timestamp_then_bulk_pushes(client):
    query_api.push(client, "idx", DOCS)
    assert client.deleted == [
        (
            "idx",
            {"bool": {"must": [{"range": {"@timestamp": {"gte": "2024-01-01"}}}]}},
        )
    ]
    assert client.pushed == [("idx", DOCS)]


def test_push_without_overwrite_only_pushes(client):
    query_api.push(client, "idx", DOCS, overwrite_old=False)
    assert client.deleted == []
    assert client.pushed == [("idx", DOCS)]


def test_push_empty_list_does_not_delete(client):
    query_api.push(client, "idx", [])
    assert client.deleted == []
    assert client.pushed == [("idx", [])]


def test_push_grouped_deletes_per_group_from_group_minimum(client):
    query_api.push(client, "idx", DOCS, grouping="host")
    expected = {
        "a": "2024-01-02",
        "b": "2024-01-01",
    }
    assert len(client.deleted) == 2
    for index, query in client.deleted:
        assert index == "idx"
        phrase, rng = query["bool"]["must"]
        assert len(query["bool"]["must"]) == 2
        assert rng == {
            "range": {"@timestamp": {"gte": expected[phrase["match_phrase"]["host"]]}}
        }
    assert {
        q["bool"]["must"][0]["match_phrase"]["host"] for _, q in client.deleted
    } == {"a", "b"}
    assert client.pushed == [("idx", DOCS)]


# -------------------------------------------------------------------------- pull


def test_pull_wraps_plain_query_and_adds_timerange():
    data = query_api.pull(
        object(),
        "idx",
        {"query": {"term": {"a": 1}}, "return_fields": ["a"]},
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    assert isinstance(data, DSData)
    assert data.index == "idx"
    assert data.es_return_fields == ["a"]
    assert data.sort == {"@timestamp": {"order": "asc"}}
    assert data.query == {
        "bool": {
            "must": [
                {"term": {"a": 1}},
                {
                    "range": {
                        "@timestamp": {
                            "gte": "2024-01-01T00:00:00.000000Z",
                            "lt": "2024-01-02T00:00:00.000000Z",
                        }
                    }
                },
            ]
        }
    }


def test_pull_keeps_bool_query_and_custom_sort_and_does_not_mutate_input():
    config = {"query": {"bool": {"must": []}}}
    data = query_api.pull(object(), "idx", config, sort={"x": 1})
    assert data.query == {"bool": {"must": []}}
    assert data.sort == {"x": 1}
    assert data.query is not config["query"]


def test_pull_with_aggregations():
    data = query_api.pull(object(), "idx", {"aggregations": {"a": {}}})
    assert data.data_type == "aggs"
    assert data.query == {}


# ------------------------------------------------------------------- add_filter


def test_add_filter_no_filters_returns_copy():
    query = {"bool": {"must": []}}
    result = query_api.add_filter(query, None)
    assert result == query and result is not query


def test_add_filter_none_query():
    assert query_api.add_filter(None, {"term": {"a": 1}}) == {
        "bool": {"filter": [{"match_all": {}}, {"term": {"a": 1}}]}
    }


def test_add_filter_wraps_non_bool_query():
    assert query_api.add_filter({"term": {"a": 1}}, {"term": {"b": 2}}) == {
        "bool": {
            "must": [{"term": {"a": 1}}],
            "filter": [{"match_all": {}}, {"term": {"b": 2}}],
        }
    }


def test_add_filter_existing_dict_filter_becomes_list():
    query = {"bool": {"filter": {"term": {"a": 1}}}}
    assert query_api.add_filter(query, [{"x": 1}, {"y": 2}]) == {
        "bool": {"filter": [{"term": {"a": 1}}, {"x": 1}, {"y": 2}]}
    }
    # caller's query untouched
    assert query == {"bool": {"filter": {"term": {"a": 1}}}}


def test_add_filter_existing_list_filter_is_extended():
    query = {"bool": {"filter": [{"term": {"a": 1}}]}}
    assert query_api.add_filter(query, {"x": 1})["bool"]["filter"] == [
        {"term": {"a": 1}},
        {"x": 1},
    ]


# ------------------------------------------------------------- add_must_sub_query


def test_add_must_sub_query_creates_and_appends():
    sub = {"term": {"a": 1}}
    assert query_api.add_must_sub_query({}, sub) == {"bool": {"must": [sub]}}
    assert query_api.add_must_sub_query({"bool": {"must": [{"x": 1}]}}, sub) == {
        "bool": {"must": [{"x": 1}, sub]}
    }
    assert query_api.add_must_sub_query({"bool": {"should": []}}, sub) == {
        "bool": {"should": [], "must": [sub]}
    }


def test_add_must_sub_query_rejects_bad_input():
    with pytest.raises(ValueError, match="query must be a dictionary"):
        query_api.add_must_sub_query([], {})
    with pytest.raises(ValueError, match="sub_query must be a dictionary"):
        query_api.add_must_sub_query({}, [])
    with pytest.raises(ValueError, match="'must' has to be a list"):
        query_api.add_must_sub_query({"bool": {"must": {}}}, {})


def test_build_ip_filter():
    assert query_api.build_ip_filter("") is None
    assert query_api.build_ip_filter("1.2.3.4") == {
        "bool": {
            "minimum_should_match": 1,
            "should": [
                {"match_phrase": {"source.ip": "1.2.3.4"}},
                {"match_phrase": {"destination.ip": "1.2.3.4"}},
            ],
        }
    }
