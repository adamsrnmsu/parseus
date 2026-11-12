"""Tests for scheduled web analytics task checkpoint behavior."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from parseus.webapp.analytics import tasks as analytics_tasks


class FakeRedis:
    """Small Redis test double with the commands used by analytics tasks."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.set_calls = []
        self.delete_calls = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = str(value)
        self.set_calls.append((key, value, nx, ex))
        return True

    def delete(self, key):
        self.delete_calls.append(key)
        self.store.pop(key, None)


class FakeAnalytic:
    """Analytic class test double loaded by name from the scheduled task."""


class FakeUi:
    def get_analytic_cfg(self):
        return {"run_idx": {"index_name": "events-*"}}


class FakeAnalyticManager:
    def __init__(self):
        self.get_calls = []

    def get(self, **lookup):
        # ``Analytic.name`` is a property, so the runner must filter on a real
        # DB column (``analytic_class``), never on ``name``.
        assert set(lookup) == {"analytic_class"}
        self.get_calls.append(lookup)
        return SimpleNamespace(ui=FakeUi())


def _patch_runner_dependencies(monkeypatch, fake_redis, *, end, has_data=True):
    monkeypatch.setattr(analytics_tasks, "redis_client", fake_redis)
    monkeypatch.setattr(
        analytics_tasks,
        "_load_analytic_class",
        lambda analytic_str: FakeAnalytic,
    )
    monkeypatch.setattr(analytics_tasks, "get_ds_client", lambda: object())
    monkeypatch.setattr(
        analytics_tasks,
        "schedule_run_index",
        lambda analytic: "events-*",
    )
    monkeypatch.setattr(
        analytics_tasks,
        "source_has_data",
        lambda ds_client, run_index: has_data,
    )
    monkeypatch.setattr(
        analytics_tasks,
        "schedule_timestamp_range",
        lambda analytic: (datetime(2026, 1, 1, tzinfo=timezone.utc), end),
    )
    monkeypatch.setattr(
        analytics_tasks,
        "Analytic",
        SimpleNamespace(objects=FakeAnalyticManager()),
    )


def test_analytic_runner_initializes_checkpoint_and_skips_first_run(monkeypatch):
    """Missing checkpoints should not backfill historical data by default."""
    end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    fake_redis = FakeRedis()
    run_calls = []
    monkeypatch.delenv("ANALYTICS_BACKFILL_ON_FIRST_RUN", raising=False)
    _patch_runner_dependencies(monkeypatch, fake_redis, end=end)
    monkeypatch.setattr(
        analytics_tasks,
        "run_and_push_analytics",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert run_calls == []
    assert fake_redis.get("FakeAnalytic:last_run_end_ts") == str(end.timestamp())
    assert fake_redis.get("FakeAnalytic:run_lock") is None


def test_analytic_runner_can_backfill_on_first_run_when_enabled(monkeypatch):
    """Operators can opt in to historical backfill for first scheduled runs."""
    end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    fake_redis = FakeRedis()
    run_calls = []
    monkeypatch.setenv("ANALYTICS_BACKFILL_ON_FIRST_RUN", "true")
    _patch_runner_dependencies(monkeypatch, fake_redis, end=end)
    monkeypatch.setattr(
        analytics_tasks,
        "run_and_push_analytics",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert len(run_calls) == 1
    analytic, = run_calls[0][0]
    assert analytic is FakeAnalytic
    assert run_calls[0][1]["user_input"]["run_start"] is None
    assert run_calls[0][1]["user_input"]["run_end"] == end
    assert fake_redis.get("FakeAnalytic:last_run_end_ts") == str(end.timestamp())
    assert fake_redis.get("FakeAnalytic:run_lock") is None


def test_analytic_runner_advances_checkpoint_after_success(monkeypatch):
    """Existing checkpoints advance only after a successful analytics run."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    fake_redis = FakeRedis({"FakeAnalytic:last_run_end_ts": str(start.timestamp())})
    run_calls = []
    _patch_runner_dependencies(monkeypatch, fake_redis, end=end)
    monkeypatch.setattr(
        analytics_tasks,
        "run_and_push_analytics",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert len(run_calls) == 1
    assert run_calls[0][1]["user_input"]["run_start"] == start
    assert run_calls[0][1]["user_input"]["run_end"] == end
    assert fake_redis.get("FakeAnalytic:last_run_end_ts") == str(end.timestamp())
    assert fake_redis.get("FakeAnalytic:run_lock") is None


def test_analytic_runner_does_not_advance_checkpoint_after_failure(monkeypatch):
    """Failed analytic windows should retry instead of being skipped forever."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    fake_redis = FakeRedis({"FakeAnalytic:last_run_end_ts": str(start.timestamp())})
    _patch_runner_dependencies(monkeypatch, fake_redis, end=end)

    def fail_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(analytics_tasks, "run_and_push_analytics", fail_run)

    with pytest.raises(RuntimeError, match="boom"):
        analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert fake_redis.get("FakeAnalytic:last_run_end_ts") == str(start.timestamp())
    assert fake_redis.get("FakeAnalytic:run_lock") is None


def test_analytic_runner_reinitializes_invalid_checkpoint_without_backfill(monkeypatch):
    """Malformed Redis checkpoints should not trigger accidental full backfills."""
    end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    fake_redis = FakeRedis({"FakeAnalytic:last_run_end_ts": "not-a-timestamp"})
    run_calls = []
    _patch_runner_dependencies(monkeypatch, fake_redis, end=end)
    monkeypatch.setattr(
        analytics_tasks,
        "run_and_push_analytics",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert run_calls == []
    assert fake_redis.get("FakeAnalytic:last_run_end_ts") == str(end.timestamp())
    assert fake_redis.get("FakeAnalytic:run_lock") is None


def test_analytic_runner_skips_when_another_run_holds_lock(monkeypatch):
    """Overlapping scheduled runs for the same analytic should not execute."""
    fake_redis = FakeRedis({"FakeAnalytic:run_lock": "existing-token"})
    schedule_calls = []
    monkeypatch.setattr(analytics_tasks, "redis_client", fake_redis)
    monkeypatch.setattr(
        analytics_tasks,
        "schedule_timestamp_range",
        lambda analytic: schedule_calls.append(analytic),
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert schedule_calls == []
    assert fake_redis.get("FakeAnalytic:run_lock") == "existing-token"


def test_analytic_runner_skips_when_source_missing_or_empty(monkeypatch):
    """A missing/empty source index should not initialize or advance a checkpoint."""
    end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    fake_redis = FakeRedis()
    run_calls = []
    schedule_calls = []
    _patch_runner_dependencies(monkeypatch, fake_redis, end=end, has_data=False)
    monkeypatch.setattr(
        analytics_tasks,
        "schedule_timestamp_range",
        lambda analytic: schedule_calls.append(analytic),
    )
    monkeypatch.setattr(
        analytics_tasks,
        "run_and_push_analytics",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert run_calls == []
    assert schedule_calls == []
    assert fake_redis.get("FakeAnalytic:last_run_end_ts") is None
    assert fake_redis.get("FakeAnalytic:run_lock") is None


def test_analytic_runner_does_not_advance_checkpoint_when_source_becomes_empty(
    monkeypatch,
):
    """An existing checkpoint must be preserved when the source is unavailable."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    fake_redis = FakeRedis({"FakeAnalytic:last_run_end_ts": str(start.timestamp())})
    run_calls = []
    _patch_runner_dependencies(monkeypatch, fake_redis, end=end, has_data=False)
    monkeypatch.setattr(
        analytics_tasks,
        "run_and_push_analytics",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert run_calls == []
    assert fake_redis.get("FakeAnalytic:last_run_end_ts") == str(start.timestamp())
    assert fake_redis.get("FakeAnalytic:run_lock") is None


def test_analytic_runner_skips_when_no_time_has_elapsed(monkeypatch):
    """end == checkpoint means no new data: nothing runs."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake_redis = FakeRedis({"FakeAnalytic:last_run_end_ts": str(start.timestamp())})
    run_calls = []
    _patch_runner_dependencies(monkeypatch, fake_redis, end=start)
    monkeypatch.setattr(
        analytics_tasks, "run_and_push_analytics", lambda *a, **k: run_calls.append(1)
    )

    analytics_tasks.analytic_runner.run("FakeAnalytic")

    assert run_calls == []
    assert fake_redis.get("FakeAnalytic:last_run_end_ts") == str(start.timestamp())
