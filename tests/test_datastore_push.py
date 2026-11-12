"""Tests for query_api.push delete-then-push behaviour."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from parseus.datastore.query_api import push

DOCS = [
    {"@timestamp": "2024-01-02T00:00:00Z", "host": "a"},
    {"@timestamp": "2024-01-01T00:00:00Z", "host": "a"},
    {"@timestamp": "2024-01-05T00:00:00Z", "host": "b"},
]


def _client(ok=True):
    c = MagicMock()
    c.bulk_push_docs.return_value = ok
    return c


def test_overwrite_deletes_from_min_timestamp_then_pushes():
    c = _client()
    push(c, "idx", DOCS)
    c.delete_by_query.assert_called_once_with(
        "idx",
        {"bool": {"must": [{"range": {"@timestamp": {"gte": "2024-01-01T00:00:00Z"}}}]}},
    )
    c.bulk_push_docs.assert_called_once_with("idx", DOCS)
    names = [m[0] for m in c.mock_calls]
    assert names.index("delete_by_query") < names.index("bulk_push_docs")


def test_grouped_delete_uses_independent_queries():
    c = _client()
    push(c, "idx", DOCS, grouping="host")
    queries = {
        q[0][1]["bool"]["must"][0]["match_phrase"]["host"]: q[0][1]
        for q in c.delete_by_query.call_args_list
    }
    assert queries["a"]["bool"]["must"][1] == {
        "range": {"@timestamp": {"gte": "2024-01-01T00:00:00Z"}}
    }
    assert queries["b"]["bool"]["must"][1] == {
        "range": {"@timestamp": {"gte": "2024-01-05T00:00:00Z"}}
    }
    assert all(len(q["bool"]["must"]) == 2 for q in queries.values())


def test_no_overwrite_does_not_delete():
    c = _client()
    push(c, "idx", DOCS, overwrite_old=False)
    c.delete_by_query.assert_not_called()
    c.bulk_push_docs.assert_called_once()


def test_failed_bulk_push_raises():
    with pytest.raises(RuntimeError):
        push(_client(ok=False), "idx", DOCS)


def test_input_documents_unchanged():
    docs = deepcopy(DOCS)
    push(_client(), "idx", docs, grouping="host")
    assert docs == DOCS
