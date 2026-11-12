"""Characterization tests for the PEAT Redis lock helpers (no live Redis)."""

import json
import pytest

from parseus.webapp.pei import peat_locks as locks


class FakeRedis:
    """Minimal stand-in for redis.Redis recording eval calls."""

    def __init__(self, store=None, eval_result=None):
        self.store = dict(store or {})
        self.eval_result = eval_result if eval_result is not None else []
        self.eval_calls = []
        self.deleted = []

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)

    def eval(self, script, numkeys, *args):
        self.eval_calls.append((script, numkeys, args))
        return self.eval_result


def _metadata(owner, **extra):
    return json.dumps({"owner": owner, **extra})


def test_build_owner_token_joins_parts():
    assert locks.build_owner_token("PeatScan", 7, "abc") == "PeatScan:7:abc"


@pytest.mark.parametrize("args", [("", 1, "t"), ("M", 0, "t"), ("M", 1, "")])
def test_build_owner_token_requires_all_parts(args):
    with pytest.raises(locks.PeatLockError):
        locks.build_owner_token(*args)


def test_build_owner_token_for_run_uses_class_name():
    class PeatScan:
        pk = 3
        task_id = "tid"

    assert locks.build_owner_token_for_run(PeatScan()) == "PeatScan:3:tid"


def test_get_owner_metadata_returns_valid_dict():
    client = FakeRedis({locks.metadata_key_for_owner("o"): _metadata("o", x=1)})
    assert locks.get_owner_metadata("o", client=client) == {"owner": "o", "x": 1}


def test_get_owner_metadata_missing_returns_none():
    assert locks.get_owner_metadata("o", client=FakeRedis()) is None


def test_get_owner_metadata_invalid_json_returns_none():
    client = FakeRedis({locks.metadata_key_for_owner("o"): "{not json"})
    assert locks.get_owner_metadata("o", client=client) is None


@pytest.mark.parametrize("raw", ["[1, 2]", json.dumps({"owner": "someone-else"})])
def test_get_owner_metadata_rejects_unexpected_shape(raw):
    client = FakeRedis({locks.metadata_key_for_owner("o"): raw})
    assert locks.get_owner_metadata("o", client=client) is None


def test_resolve_lock_targets_ip_mode(monkeypatch):
    monkeypatch.setattr(locks, "hosts_to_ips", lambda hosts: ["10.0.0.1", " ", "10.0.0.2"])
    result = locks.resolve_lock_targets(["a", " "], "ip")
    assert [t.target for t in result] == ["10.0.0.1", "10.0.0.2"]
    assert result[0].key == "peat:lock:ip:10.0.0.1"
    assert result[0].target_mode == "ip"


def test_resolve_lock_targets_serial_mode_and_string_input(monkeypatch):
    seen = []
    monkeypatch.setattr(
        locks, "port_nums_to_addresses", lambda ports: seen.append(ports) or ["/dev/ttyS1"]
    )
    result = locks.resolve_lock_targets("1", "serial")
    assert seen == [["1"]]
    assert result[0].key == "peat:lock:serial:/dev/ttyS1"


def test_resolve_lock_targets_rejects_empty_input():
    with pytest.raises(locks.PeatLockError, match="At least one"):
        locks.resolve_lock_targets(["", "  "], "ip")


def test_resolve_lock_targets_rejects_unknown_mode():
    with pytest.raises(locks.PeatLockError, match="Unsupported"):
        locks.resolve_lock_targets(["x"], "broadcast")


def test_resolve_lock_targets_rejects_nothing_resolved(monkeypatch):
    monkeypatch.setattr(locks, "hosts_to_ips", lambda hosts: [])
    with pytest.raises(locks.PeatLockError, match="No concrete"):
        locks.resolve_lock_targets(["x"], "ip")


def test_acquire_target_locks_success(monkeypatch):
    monkeypatch.setattr(locks, "hosts_to_ips", lambda hosts: ["10.0.0.1"])
    client = FakeRedis(eval_result=[])
    result = locks.acquire_target_locks(
        owner="o", targets=["x"], target_mode="ip", ttl_seconds=60, client=client
    )
    assert result.acquired is True
    assert result.targets == ["10.0.0.1"]
    assert result.lock_keys == ["peat:lock:ip:10.0.0.1"]
    assert result.busy_targets == []
    _, numkeys, args = client.eval_calls[0]
    assert numkeys == 1
    assert args[:3] == ("peat:lock:ip:10.0.0.1", "o", 60)
    assert json.loads(args[4])["targets"] == ["10.0.0.1"]


def test_acquire_target_locks_maps_busy_keys_to_targets(monkeypatch):
    monkeypatch.setattr(locks, "hosts_to_ips", lambda hosts: ["10.0.0.1", "10.0.0.2"])
    client = FakeRedis(eval_result=["peat:lock:ip:10.0.0.2"])
    result = locks.acquire_target_locks(
        owner="o", targets=["x"], target_mode="ip", ttl_seconds=60, client=client
    )
    assert result.acquired is False
    assert result.busy_targets == ["10.0.0.2"]
    assert result.busy_lock_keys == ["peat:lock:ip:10.0.0.2"]


def test_release_target_locks_counts_released():
    client = FakeRedis(eval_result=2)
    assert locks.release_target_locks("o", ["k1", "", "k2"], client=client) == 2
    _, numkeys, args = client.eval_calls[0]
    assert numkeys == 2
    assert args == ("k1", "k2", "o", locks.metadata_key_for_owner("o"))


def test_release_owner_locks_without_metadata_deletes_key():
    client = FakeRedis()
    assert locks.release_owner_locks("o", client=client) == 0
    assert client.deleted == [locks.metadata_key_for_owner("o")]
    assert client.eval_calls == []


def test_release_owner_locks_uses_metadata_lock_keys():
    client = FakeRedis(
        {locks.metadata_key_for_owner("o"): _metadata("o", lock_keys=["k1"])},
        eval_result=1,
    )
    assert locks.release_owner_locks("o", client=client) == 1
    assert client.eval_calls[0][2][0] == "k1"


def test_verify_target_locks_without_metadata_reports_metadata_key():
    assert locks.verify_target_locks("o", client=FakeRedis()) == [
        locks.metadata_key_for_owner("o")
    ]


def test_verify_target_locks_uses_metadata_keys():
    client = FakeRedis(
        {locks.metadata_key_for_owner("o"): _metadata("o", lock_keys=["k1", "k2"])},
        eval_result=["k2"],
    )
    assert locks.verify_target_locks("o", client=client) == ["k2"]
    assert client.eval_calls[0][1] == 2


def test_verify_target_locks_explicit_keys():
    client = FakeRedis(eval_result=[])
    assert locks.verify_target_locks("o", ["k1"], client=client) == []
    assert client.eval_calls[0][2] == ("k1", "o")


def test_verify_target_locks_empty_keys_reports_metadata_key():
    assert locks.verify_target_locks("o", [""], client=FakeRedis()) == [
        locks.metadata_key_for_owner("o")
    ]


def test_verify_target_locks_metadata_without_keys():
    client = FakeRedis({locks.metadata_key_for_owner("o"): _metadata("o")})
    assert locks.verify_target_locks("o", client=client) == [
        locks.metadata_key_for_owner("o")
    ]


def test_refresh_owner_locks_without_metadata():
    assert locks.refresh_owner_locks("o", ttl_seconds=5, client=FakeRedis()) == [
        locks.metadata_key_for_owner("o")
    ]


def test_refresh_owner_locks_metadata_without_keys():
    client = FakeRedis({locks.metadata_key_for_owner("o"): _metadata("o", lock_keys=[""])})
    assert locks.refresh_owner_locks("o", ttl_seconds=5, client=client) == [
        locks.metadata_key_for_owner("o")
    ]


def test_refresh_owner_locks_returns_missing_from_script():
    client = FakeRedis(
        {locks.metadata_key_for_owner("o"): _metadata("o", lock_keys=["k1"])},
        eval_result=["k1"],
    )
    assert locks.refresh_owner_locks("o", ttl_seconds=5, client=client) == ["k1"]
    _, numkeys, args = client.eval_calls[0]
    assert numkeys == 1
    assert args == ("k1", "o", 5, locks.metadata_key_for_owner("o"))


def test_owned_targets():
    client = FakeRedis({locks.metadata_key_for_owner("o"): _metadata("o", targets=[1, "b"])})
    assert locks.owned_targets("o", client=client) == ["1", "b"]
    assert locks.owned_targets("zz", client=FakeRedis()) == []


def test_get_lock_ttl_seconds(settings):
    settings.PEAT_LOCK_TTL_SECONDS = 42
    assert locks.get_lock_ttl_seconds() == 42
    settings.PEAT_LOCK_TTL_SECONDS = 0
    with pytest.raises(locks.PeatLockError):
        locks.get_lock_ttl_seconds()


def test_get_redis_client_requires_broker(settings):
    settings.CELERY_BROKER_URL = ""
    with pytest.raises(locks.PeatLockError):
        locks.get_redis_client()
