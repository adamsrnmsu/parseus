from parseus.preprocessors.reg_state import reg_ops_to_reg_state


def _op(sec, addr, value, src="a", dst="b"):
    return (f"2024-01-01T00:00:{sec:02d}Z", "t", src, dst, addr, value)


def _run(ops, **kw):
    return list(reg_ops_to_reg_state(ops, value_expiration_sec=10, **kw))


def _assert_ordered(docs):
    stamps = [d["@timestamp"] for d in docs]
    assert stamps == sorted(stamps)


OPS = [_op(0, "r1", 1), _op(1, "r2", 2), _op(30, "r2", 3)]


def test_expiry_fires_with_default_name_map():
    docs = _run(OPS)
    assert any(d.get("a->b:r1", 0) is None for d in docs)
    _assert_ordered(docs)


def test_expiry_fires_with_identity_name_map_and_in_order():
    docs = _run(OPS, name_map_function=lambda x: x)
    assert any(d.get(("a", "b", "r1"), 0) is None for d in docs)
    _assert_ordered(docs)


def test_expiry_timestamps_precede_later_doc_and_follow_earlier():
    docs = _run(OPS)
    exp = [d for d in docs if d.get("a->b:r1", 0) is None][0]
    assert exp["@timestamp"].startswith("2024-01-01T00:00:10")
    assert exp["a->b:r2"] == 2  # r2 (updated at 1s) not yet expired


def test_refreshed_register_does_not_expire():
    ops = [_op(0, "r1", 1), _op(5, "r1", 1), _op(12, "r2", 2), _op(14, "r2", 3)]
    docs = _run(ops)
    assert all(d.get("a->b:r1") == 1 for d in docs)
    _assert_ordered(docs)
