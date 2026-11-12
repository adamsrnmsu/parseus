from parseus.datastore.query_api import add_must_sub_query

SUB = {"term": {"a": 1}}


def test_existing_bool_gets_sub_query():
    q = {"bool": {"must": [{"match": {"x": "y"}}]}}
    assert add_must_sub_query(q, SUB) == {"bool": {"must": [{"match": {"x": "y"}}, SUB]}}
    assert q == {"bool": {"must": [{"match": {"x": "y"}}]}}


def test_non_bool_query_is_preserved_alongside_sub_query():
    q = {"match": {"x": "y"}}
    assert add_must_sub_query(q, SUB) == {"bool": {"must": [q, SUB]}}


def test_empty_query():
    assert add_must_sub_query({}, SUB) == {"bool": {"must": [SUB]}}
