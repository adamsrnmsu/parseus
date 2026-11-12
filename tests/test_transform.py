"""
Runs transform preprocessor functions to make sure behaviour is constant/expected.
"""

from parseus.preprocessors.transform import (
    compose,
    one_to_many,
    wrap_generator_function,
)


def duplicate_transformer(doc):
    "Returns two copies of the document"
    return [doc, doc]


def empty_transformer(doc):
    "Returns no documents"
    return []


def modifying_transformer(doc, suffix):
    "Returns a transformed document with a suffix"
    return [f"{doc['name']}_{suffix}"]


def test_one_to_many_basic():
    "Tests one_to_many using the duplicating transformer"
    input_data = [{"name": "doc1"}, {"name": "doc2"}]
    result = list(one_to_many(input_data, duplicate_transformer))
    assert result == [
        {"name": "doc1"},
        {"name": "doc1"},
        {"name": "doc2"},
        {"name": "doc2"},
    ]


def test_one_to_many_empty_input():
    "Tests one_to_many using the duplicating transformer with empty input"
    input_data = []
    result = list(one_to_many(input_data, duplicate_transformer))
    assert result == []


def test_one_to_many_no_output():
    "Tests one_to_many using the empty transformer with actual input"
    input_data = [{"name": "doc1"}, {"name": "doc2"}]
    result = list(one_to_many(input_data, empty_transformer))
    assert result == []


def test_one_to_many_multiple_outputs():
    "Tests one_to_many using the modifying transformer with actual input"
    input_data = [{"name": "doc1"}, {"name": "doc2"}]
    result = list(one_to_many(input_data, modifying_transformer, suffix="v1"))
    assert result == ["doc1_v1", "doc2_v1"]


def test_one_to_many_with_kwargs():
    "Tests one_to_many using the modifying transformer with actual input using kwargs"
    input_data = [{"name": "doc1"}, {"name": "doc2"}]
    result = list(one_to_many(input_data, modifying_transformer, suffix="test"))
    assert result == ["doc1_test", "doc2_test"]


# ------------------------------------------------- wrap_generator_function


def _wrapped(items):
    calls = []

    @wrap_generator_function
    def gen(n):
        calls.append(n)
        yield from range(n)

    return gen(len(items)), calls


def test_wrapped_iterable_can_be_iterated_repeatedly():
    wrapped, calls = _wrapped(range(4))
    assert list(wrapped) == [0, 1, 2, 3]
    assert list(wrapped) == [0, 1, 2, 3]
    assert calls == [4, 4]


def test_wrapped_get_length_counts_and_caches():
    wrapped, calls = _wrapped(range(4))
    assert wrapped.get_length() == 4
    assert wrapped.get_length() == 4
    assert calls == [4]


def test_wrapped_bool():
    assert not _wrapped([])[0]
    wrapped, _ = _wrapped([1])
    assert wrapped
    list(wrapped)  # length is now cached
    assert wrapped
    empty, _ = _wrapped([])
    list(empty)
    assert not empty


def test_wrapped_integer_indexing():
    wrapped, _ = _wrapped(range(5))
    assert wrapped[0] == 0
    assert wrapped[3] == 3
    assert wrapped[-1] == 4
    assert wrapped[-5] == 0


def test_wrapped_slicing():
    wrapped, _ = _wrapped(range(6))
    assert wrapped[1:3] == [1, 2]
    assert wrapped[:2] == [0, 1]
    assert wrapped[::2] == [0, 2, 4]
    assert wrapped[-2:] == [4, 5]
    assert wrapped[:-2] == [0, 1, 2, 3]
    assert wrapped[-4:-1] == [2, 3, 4]


def test_compose_applies_transforms_in_order():
    add = lambda docs, **kw: [d + kw.get("n", 0) for d in docs]
    double = lambda docs, **kw: [d * 2 for d in docs]
    assert compose(add, double)([1, 2], n=1) == [4, 6]
    assert compose(double, add)([1, 2], n=1) == [3, 5]
