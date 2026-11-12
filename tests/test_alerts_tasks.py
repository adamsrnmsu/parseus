"""Characterization tests for alerts maintenance tasks (hermetic: no ES/Redis)."""

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from parseus.webapp.alerts import tasks


# ---------------------------------------------------------------- ftp cleanup
def _ftp(tmp_path, names, listed):
    ftp = tmp_path / "ftp"
    ftp.mkdir()
    for name in names:
        (ftp / name).write_text("x")
    log = tmp_path / "completed.log"
    log.write_text("\n".join(listed) + "\n")
    return ftp, log


def test_cleanup_deletes_listed_files_and_counts_missing(tmp_path):
    ftp, log = _ftp(tmp_path, ["a.log", "b.log", "keep.log"], ["a.log", "/x/y/b.log", "gone.log", ""])
    out = tasks.cleanup_ftp_files(str(ftp), str(log))
    assert out == {
        "ok": True, "dry_run": False, "processed_entries": 3,
        "deleted": 2, "missing": 1, "errors": 0,
    }
    assert sorted(p.name for p in ftp.iterdir()) == ["keep.log"]


def test_cleanup_dry_run_deletes_nothing(tmp_path):
    ftp, log = _ftp(tmp_path, ["a.log"], ["a.log"])
    out = tasks.cleanup_ftp_files(str(ftp), str(log), dry_run=True)
    assert out["deleted"] == 0 and out["dry_run"] is True and out["ok"] is True
    assert (ftp / "a.log").exists()


def test_cleanup_directory_entry_is_an_error(tmp_path):
    ftp, log = _ftp(tmp_path, [], ["sub"])
    (ftp / "sub").mkdir()
    out = tasks.cleanup_ftp_files(str(ftp), str(log))
    assert out["errors"] == 1 and out["ok"] is False and out["deleted"] == 0
    assert (ftp / "sub").is_dir()


def test_cleanup_unlink_failure_is_an_error(tmp_path, monkeypatch):
    ftp, log = _ftp(tmp_path, ["a.log"], ["a.log"])

    def boom(self, *a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr(tasks.Path, "unlink", boom)
    out = tasks.cleanup_ftp_files(str(ftp), str(log))
    assert out["errors"] == 1 and out["deleted"] == 0 and out["ok"] is False


def test_cleanup_with_no_log_returns_empty_summary(tmp_path):
    out = tasks.cleanup_ftp_files(str(tmp_path), str(tmp_path / "missing.log"))
    assert out == {
        "ok": True, "dry_run": False, "processed_entries": 0,
        "deleted": 0, "missing": 0, "errors": 0,
    }


def test_cleanup_uses_settings_defaults(tmp_path, settings):
    ftp, log = _ftp(tmp_path, ["a.log"], ["a.log"])
    settings.FTP_CLEANUP_DIR = str(ftp)
    settings.FTP_COMPLETED_LOG_PATH = str(log)
    assert tasks.cleanup_ftp_files()["deleted"] == 1


def test_load_completed_list_strips_blank_lines(tmp_path):
    log = tmp_path / "l"
    log.write_text(" a \n\n  \nb\n")
    assert tasks._load_completed_list(log) == ["a", "b"]


def test_load_completed_list_unreadable_returns_empty(tmp_path):
    assert tasks._load_completed_list(tmp_path) == []  # a directory: OSError


# ------------------------------------------------------------------ logrotate
def _run(returncode, out="o\n", err="e\n"):
    def fake(cmd, capture_output, text, check):
        fake.cmd = cmd
        return SimpleNamespace(returncode=returncode, stdout=out, stderr=err)

    return fake


def test_run_logrotate_success(monkeypatch):
    fake = _run(0)
    monkeypatch.setattr(subprocess, "run", fake)
    out = tasks.run_logrotate()
    assert out == {"ok": True, "returncode": 0, "stdout": "o", "stderr": "e"}
    assert fake.cmd[:2] == ["logrotate", "-v"]
    assert fake.cmd[2].endswith("config/logrotate.conf")


def test_run_logrotate_failure_reports_rc(monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", _run(3, out="", err="bad\n"))
    with caplog.at_level(logging.ERROR, logger=tasks.logger.name):
        out = tasks.run_logrotate()
    assert out["ok"] is False and out["returncode"] == 3 and out["stderr"] == "bad"
    assert "logrotate failed (rc=3): bad" in caplog.text


# ------------------------------------------------------------------ freshness
NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize(
    "age,expected",
    [(5, "ok"), (100, "warning"), (500, "critical"), (-50, "ok")],
)
def test_classify_freshness(age, expected):
    status, seconds = tasks._classify_freshness(
        latest_event_at=datetime.now(timezone.utc) - timedelta(seconds=age),
        warn_after_seconds=60,
        critical_after_seconds=300,
    )
    assert status == expected
    assert seconds >= 0


def test_classify_freshness_missing_uses_given_status():
    kw = dict(warn_after_seconds=1, critical_after_seconds=2)
    assert tasks._classify_freshness(latest_event_at=None, **kw) == ("critical", None)
    assert tasks._classify_freshness(
        latest_event_at=None, missing_data_status="warning", **kw
    ) == ("warning", None)


class _FakeDS:
    def __init__(self, response):
        self.response = response

    def search(self, **kw):
        self.kw = kw
        return self.response


@pytest.mark.parametrize(
    "response,expected",
    [
        ({}, None),
        ({"hits": {"hits": []}}, None),
        ({"hits": {"hits": [{"_source": {}}]}}, None),
        ({"hits": {"hits": [{}]}}, None),
    ],
)
def test_query_latest_timestamp_none_cases(monkeypatch, response, expected):
    monkeypatch.setattr(tasks, "get_ds_client", lambda: _FakeDS(response))
    assert tasks._query_latest_timestamp("idx-*") is expected


def test_query_latest_timestamp_parses_hit(monkeypatch):
    ds = _FakeDS({"hits": {"hits": [{"_source": {"@timestamp": "2024-01-02T03:04:05Z"}}]}})
    monkeypatch.setattr(tasks, "get_ds_client", lambda: ds)
    got = tasks._query_latest_timestamp("idx-*")
    assert got.year == 2024 and got.hour == 3
    assert ds.kw["index"] == "idx-*" and ds.kw["body"]["size"] == 1


# ------------------------------------------------------------ ingestor health
class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, k, v):
        self.store[k] = v


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(tasks, "redis_client", r)
    return r


def _evaluate(**kw):
    args = dict(
        service="svc", index_pattern="i-*", warn_after_seconds=60,
        critical_after_seconds=300, missing_data_status="critical",
    )
    args.update(kw)
    return tasks._evaluate_ingestor_health(**args)


def test_evaluate_ok_event_is_written_to_redis(monkeypatch, fake_redis):
    latest = datetime.now(timezone.utc) - timedelta(seconds=10)
    monkeypatch.setattr(tasks, "_query_latest_timestamp", lambda index_pattern: latest)
    out = _evaluate()
    assert out["ok"] is True and out["status"] == "ok" and out["service"] == "svc"
    assert out["latest_event_at"] == latest.isoformat()
    assert out["message"].startswith("Latest event is ")
    assert out["redis_key"] == f"{tasks.INGESTOR_HEALTH_REDIS_PREFIX}:svc"
    stored = json.loads(fake_redis.store[out["redis_key"]])
    assert stored["status"] == "ok" and "redis_key" not in stored


def test_evaluate_warning_is_still_ok(monkeypatch, fake_redis, caplog):
    latest = datetime.now(timezone.utc) - timedelta(seconds=100)
    monkeypatch.setattr(tasks, "_query_latest_timestamp", lambda index_pattern: latest)
    with caplog.at_level(logging.WARNING, logger=tasks.logger.name):
        out = _evaluate()
    assert out["status"] == "warning" and out["ok"] is True
    assert "Ingestor health svc" in caplog.text


def test_evaluate_missing_data_follows_missing_status(monkeypatch, fake_redis):
    monkeypatch.setattr(tasks, "_query_latest_timestamp", lambda index_pattern: None)
    crit = _evaluate()
    assert (crit["status"], crit["ok"], crit["latest_event_at"]) == ("critical", False, None)
    assert crit["message"] == "No matching events found"
    warn = _evaluate(missing_data_status="warning")
    assert (warn["status"], warn["ok"]) == ("warning", True)


def test_evaluate_query_failure_is_recorded_as_error(monkeypatch, fake_redis):
    def boom(index_pattern):
        raise RuntimeError("es down")

    monkeypatch.setattr(tasks, "_query_latest_timestamp", boom)
    out = _evaluate()
    assert out["ok"] is False and out["status"] == "error"
    assert out["message"] == "health check failed: es down"
    assert out["age_seconds"] is None and out["latest_event_at"] is None
    assert out["redis_key"] in fake_redis.store


def test_service_checks_use_their_thresholds(monkeypatch, fake_redis):
    monkeypatch.setattr(tasks, "_query_latest_timestamp", lambda index_pattern: None)
    both = tasks.check_ingestor_health()
    assert both["logstash"]["status"] == "critical"
    assert both["gpib"]["status"] == "warning"
    assert both["logstash"]["index_pattern"] == tasks.LOGSTASH_HEALTH_INDEX_PATTERN
    assert both["gpib"]["index_pattern"] == tasks.GPIB_HEALTH_INDEX_PATTERN
