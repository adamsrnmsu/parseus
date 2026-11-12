"""Characterization tests for the PEAT celery tasks (DB real, PEAT/Redis faked)."""

from types import SimpleNamespace

import pytest
from celery.exceptions import Retry

from parseus.webapp.pei import tasks
from parseus.webapp.pei.models import PeatPull, PeatScan
from parseus.webapp.pei.peat_process import PeatProcessError

pytestmark = pytest.mark.django_db

real_run_peat_job = tasks.run_peat_job


@pytest.fixture
def env(monkeypatch, settings, tmp_path):
    settings.PEAT_PULL_DIR = tmp_path / "pulls"
    settings.PEAT_SCAN_DIR = tmp_path / "scans"
    state = SimpleNamespace(
        jobs=[],
        released=[],
        missing=[],
        acquire=SimpleNamespace(acquired=True, busy_targets=["10.0.0.9"]),
        job_error=None,
    )

    def fake_job(run_obj, **kwargs):
        state.jobs.append((run_obj.pk, kwargs))
        if state.job_error:
            raise state.job_error

    class FakeHeartbeat:
        def __init__(self, owner):
            self.owner = owner

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tasks, "run_peat_job", fake_job)
    monkeypatch.setattr(tasks, "PeatLockHeartbeat", FakeHeartbeat)
    monkeypatch.setattr(tasks, "release_owner_locks", state.released.append)
    monkeypatch.setattr(tasks, "verify_target_locks", lambda owner: state.missing)
    monkeypatch.setattr(tasks, "acquire_target_locks", lambda **kw: state.acquire)
    state.tmp = tmp_path
    return state


def make(model, status="started", run_dir="r1", **kw):
    return model.objects.create(
        status=status, run_dir=run_dir, task_id=f"tid-{model.__name__}", **kw
    )


def reload(obj):
    return obj.__class__.objects.get(pk=obj.pk)


def test_pull_success_marks_done_and_releases(env):
    pull = make(PeatPull, process_id=5)
    assert tasks.peat_pull.run(pull.pk, ["10.0.0.1"], "http://es") is True
    pull = reload(pull)
    assert (pull.status, pull.process_id) == ("done", None)
    (pk, kwargs), = env.jobs
    assert kwargs["method"] == "pull"
    assert kwargs["targets"] == ["10.0.0.1"]
    assert kwargs["database_url"] == "http://es"
    assert kwargs["config_file"] is None
    assert kwargs["run_dir"] == env.tmp / "pulls" / "r1"
    assert env.released == [f"PeatPull:{pull.pk}:tid-PeatPull"]


def test_pull_not_started_skips_job_and_releases(env):
    pull = make(PeatPull, status="stopped")
    assert tasks.peat_pull.run(pull.pk, ["x"], None) is True
    assert env.jobs == []
    assert reload(pull).status == "stopped"
    assert len(env.released) == 1


def test_pull_lock_verification_failure_marks_error(env):
    env.missing = ["peat:lock:ip:1"]
    pull = make(PeatPull)
    tasks.peat_pull.run(pull.pk, ["x"], None)
    assert reload(pull).status == "error"
    assert env.jobs == []
    assert len(env.released) == 1


def test_pull_job_failure_marks_error_but_not_over_stopped(env):
    env.job_error = RuntimeError("boom")
    pull = make(PeatPull, process_id=9)
    tasks.peat_pull.run(pull.pk, ["x"], None)
    pull = reload(pull)
    assert (pull.status, pull.process_id) == ("error", None)



def test_pull_job_failure_does_not_override_stopped(env, monkeypatch):
    stopped = make(PeatPull)

    def stop_then_fail(run_obj, **kw):
        PeatPull.objects.filter(pk=run_obj.pk).update(status="stopped")
        raise RuntimeError("x")

    monkeypatch.setattr(tasks, "run_peat_job", stop_then_fail)
    tasks.peat_pull.run(stopped.pk, ["x"], None)
    assert reload(stopped).status == "stopped"


def test_pull_without_run_dir_errors(env):
    pull = make(PeatPull, run_dir=None)
    tasks.peat_pull.run(pull.pk, ["x"], None)
    assert reload(pull).status == "error"
    assert env.jobs == []


def test_pull_uses_config_file(env, settings):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from parseus.webapp.pei.models import PeatConfig

    settings.MEDIA_ROOT = env.tmp / "media"
    cfg = PeatConfig.objects.create(
        name="c", filename=SimpleUploadedFile("c.yaml", b"hosts: []\n")
    )
    pull = make(PeatPull, config=cfg)
    tasks.peat_pull.run(pull.pk, ["x"], None)
    (_, kwargs), = env.jobs
    assert kwargs["config_file"] == cfg.filename.path


def test_pull_unacquired_busy_without_queue_errors(env):
    env.acquire = SimpleNamespace(acquired=False, busy_targets=["a"])
    pull = make(PeatPull)
    assert tasks.peat_pull.run(pull.pk, ["x"], None, locks_acquired=False) is True
    assert reload(pull).status == "error"
    assert env.jobs == []


def test_pull_unacquired_acquires_then_runs(env):
    pull = make(PeatPull)
    assert tasks.peat_pull.run(pull.pk, ["x"], None, locks_acquired=False) is True
    assert reload(pull).status == "done"
    assert len(env.jobs) == 1


def test_pull_unacquired_busy_queued_retries_and_waits(env):
    env.acquire = SimpleNamespace(acquired=False, busy_targets=["10.0.0.9"])
    pull = make(PeatPull)
    with pytest.raises(Retry):
        tasks.peat_pull.run(
            pull.pk, ["x"], None, queue_if_busy=True, locks_acquired=False
        )
    assert reload(pull).status == "waiting"
    log = (env.tmp / "pulls" / "r1" / "logs" / "console.log").read_text()
    assert "Waiting for PEAT target locks: 10.0.0.9" in log
    assert env.jobs == []


def test_scan_success_passes_intensive_flag(env):
    scan = make(PeatScan)
    tasks.peat_scan.run(scan.pk, ["10.0.0.0/30"], None, intensive_scan=True)
    assert reload(scan).status == "done"
    (_, kwargs), = env.jobs
    assert kwargs["method"] == "scan"
    assert kwargs["intensive_scan"] is True
    assert kwargs["config_file"] is None
    assert kwargs["run_dir"] == env.tmp / "scans" / "r1"


def test_scan_config_overrides_intensive(env, monkeypatch):
    cfg = SimpleNamespace(
        filename=SimpleNamespace(path="/cfg.yaml"), intensive_scan=True
    )
    monkeypatch.setattr(tasks.PeatConfig.objects, "get", lambda pk: cfg)
    scan = make(PeatScan)
    tasks.peat_scan.run(scan.pk, ["x"], None, config_pk=3, intensive_scan=False)
    (_, kwargs), = env.jobs
    assert kwargs["config_file"] == "/cfg.yaml"
    assert kwargs["intensive_scan"] is True


def test_scan_not_started_skips(env):
    scan = make(PeatScan, status="done")
    assert tasks.peat_scan.run(scan.pk, ["x"], None) is True
    assert env.jobs == [] and len(env.released) == 1


def test_scan_generic_failure_marks_error(env):
    env.job_error = ValueError("bad")
    scan = make(PeatScan, process_id=4)
    tasks.peat_scan.run(scan.pk, ["x"], None)
    scan = reload(scan)
    assert (scan.status, scan.process_id) == ("error", None)


def test_scan_process_error_with_no_devices_is_done(env):
    env.job_error = PeatProcessError(1)
    scan = make(PeatScan)
    log_dir = env.tmp / "scans" / "r1" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "console.log").write_text("failed unicast_ip_scan: no hosts are active")
    tasks.peat_scan.run(scan.pk, ["x"], None)
    assert reload(scan).status == "done"
    assert "No supported devices were identified" in (log_dir / "console.log").read_text()


def test_scan_stopped_during_no_devices_check_gets_no_completion_message(env, monkeypatch):
    env.job_error = PeatProcessError(1)
    scan = make(PeatScan)
    log_dir = env.tmp / "scans" / "r1" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "console.log").write_text("failed unicast_ip_scan: no hosts are active")

    def stop_then_report_none(run_dir):
        PeatScan.objects.filter(pk=scan.pk).update(status="stopped")
        return True

    monkeypatch.setattr(tasks, "peat_scan_found_no_devices", stop_then_report_none)
    tasks.peat_scan.run(scan.pk, ["x"], None)
    assert reload(scan).status == "stopped"
    assert "No supported devices were identified" not in (log_dir / "console.log").read_text()


def test_scan_process_error_otherwise_is_error(env):
    env.job_error = PeatProcessError(2)
    scan = make(PeatScan)
    tasks.peat_scan.run(scan.pk, ["x"], None)
    assert reload(scan).status == "error"


def test_scan_process_error_when_stopped_stays_stopped(env, monkeypatch):
    scan = make(PeatScan)

    def stop_then_fail(run_obj, **kw):
        PeatScan.objects.filter(pk=run_obj.pk).update(status="stopped")
        raise PeatProcessError(1)

    monkeypatch.setattr(tasks, "run_peat_job", stop_then_fail)
    tasks.peat_scan.run(scan.pk, ["x"], None)
    assert reload(scan).status == "stopped"


def test_scan_without_run_dir_errors(env):
    scan = make(PeatScan, run_dir=None)
    tasks.peat_scan.run(scan.pk, ["x"], None)
    assert reload(scan).status == "error"


def test_scan_unacquired_busy_queued_times_out(env, settings):
    env.acquire = SimpleNamespace(acquired=False, busy_targets=["a"])
    settings.PEAT_LOCK_QUEUE_TIMEOUT_SECONDS = 1
    scan = make(PeatScan, status="waiting")
    PeatScan.objects.filter(pk=scan.pk).update(created="2000-01-01T00:00:00Z")
    assert (
        tasks.peat_scan.run(scan.pk, ["x"], None, queue_if_busy=True, locks_acquired=False)
        is True
    )
    assert reload(scan).status == "error"
    log = (env.tmp / "scans" / "r1" / "logs" / "console.log").read_text()
    assert "Timed out waiting for PEAT target locks" in log


def test_scan_unacquired_stopped_returns_without_work(env):
    scan = make(PeatScan, status="stopped")
    assert tasks.peat_scan.run(scan.pk, ["x"], None, locks_acquired=False) is True
    assert env.jobs == [] and reload(scan).status == "stopped"


def test_acquire_on_stopped_run_returns_false_without_touching_locks(env):
    scan = make(PeatScan, status="stopped")
    ok = tasks._acquire_queued_locks_or_retry(
        SimpleNamespace(), scan, owner="o", targets=["x"], target_mode="ip", queue_if_busy=False
    )
    assert ok is False and env.released == []


def test_acquire_flips_waiting_to_running(env):
    scan = make(PeatScan, status="waiting")
    ok = tasks._acquire_queued_locks_or_retry(
        SimpleNamespace(), scan, owner="o", targets=["x"], target_mode="ip", queue_if_busy=False
    )
    assert ok is True and reload(scan).status == "running"


def test_acquire_lost_race_releases_locks(env):
    scan = make(PeatScan, status="done")
    ok = tasks._acquire_queued_locks_or_retry(
        SimpleNamespace(), scan, owner="o", targets=["x"], target_mode="ip", queue_if_busy=False
    )
    assert ok is False and env.released == ["o"]


def test_queue_retry_delay_adds_bounded_jitter(settings):
    settings.PEAT_LOCK_QUEUE_RETRY_SECONDS = 10
    settings.PEAT_LOCK_QUEUE_RETRY_JITTER_SECONDS = 0
    assert tasks._queue_retry_delay_seconds() == 10
    settings.PEAT_LOCK_QUEUE_RETRY_JITTER_SECONDS = 3
    assert {tasks._queue_retry_delay_seconds() for _ in range(50)} <= {10, 11, 12, 13}
    settings.PEAT_LOCK_QUEUE_RETRY_SECONDS = 0
    settings.PEAT_LOCK_QUEUE_RETRY_JITTER_SECONDS = 0
    assert tasks._queue_retry_delay_seconds() == 1


# ---- run_peat_job -----------------------------------------------------------


@pytest.fixture
def job(monkeypatch, env):
    state = SimpleNamespace(
        returncode=0, pid=4242, killed=[], commands=[], stop_seen=None, db_status=None
    )

    def fake_subprocess(command, run_dir, *, on_start, should_stop):
        state.commands.append(command)
        on_start(state.pid)
        if state.db_status:
            PeatScan.objects.update(status=state.db_status)
        state.stop_seen = should_stop()
        return state.returncode

    monkeypatch.setattr(tasks, "run_peat_subprocess", fake_subprocess)
    monkeypatch.setattr(tasks, "terminate_process_group", state.killed.append)
    monkeypatch.setattr(tasks, "current_process_host", lambda: "host-1")
    monkeypatch.setattr(tasks, "connection", SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(tasks, "build_peat_command", lambda *a, **k: ["peat", a[0]])
    return state


def run_job(scan, **kw):
    real_run_peat_job(
        scan,
        method="scan",
        targets=["x"],
        run_dir=None,
        database_url=None,
        **kw,
    )


def test_run_peat_job_records_pid_and_host_then_succeeds(job):
    scan = make(PeatScan, status="running")
    run_job(scan)
    scan = reload(scan)
    assert (scan.process_id, scan.process_host) == (4242, "host-1")
    assert job.stop_seen is False
    assert job.commands == [["peat", "scan"]]


def test_run_peat_job_nonzero_exit_raises(job):
    job.returncode = 3
    scan = make(PeatScan, status="running")
    with pytest.raises(PeatProcessError) as excinfo:
        run_job(scan)
    assert excinfo.value.returncode == 3


def test_run_peat_job_stopped_before_start_does_nothing(job):
    scan = make(PeatScan, status="stopped")
    run_job(scan)
    assert job.commands == []


def test_run_peat_job_stopped_during_run_clears_process_and_terminates(job):
    job.db_status = "stopped"
    job.returncode = 1
    scan = make(PeatScan, status="running")
    run_job(scan)  # no PeatProcessError despite the non-zero code
    scan = reload(scan)
    assert (scan.process_id, scan.process_host) == (None, None)
    assert job.stop_seen is True


def test_run_peat_job_kills_pid_when_stopped_before_pid_saved(job, monkeypatch):
    scan = make(PeatScan, status="running")

    def fake_subprocess(command, run_dir, *, on_start, should_stop):
        PeatScan.objects.filter(pk=scan.pk).update(status="stopped")
        on_start(77)
        return 0

    monkeypatch.setattr(tasks, "run_peat_subprocess", fake_subprocess)
    run_job(scan)
    assert job.killed == [77]
    assert reload(scan).process_id is None


def test_get_peat_run_device_types_defaults_to_all():
    assert tasks.get_peat_run_device_types(SimpleNamespace(modules=[])) == ["all"]
    assert tasks.get_peat_run_device_types(SimpleNamespace(modules=["sel"])) == ["sel"]


# ---- heartbeat --------------------------------------------------------------


def test_heartbeat_interval_from_setting_and_ttl(settings, monkeypatch):
    settings.PEAT_LOCK_HEARTBEAT_SECONDS = 7
    assert tasks.PeatLockHeartbeat("o").interval_seconds == 7
    settings.PEAT_LOCK_HEARTBEAT_SECONDS = 0
    monkeypatch.setattr(tasks, "get_lock_ttl_seconds", lambda: 90)
    assert tasks.PeatLockHeartbeat("o").interval_seconds == 30
    monkeypatch.setattr(tasks, "get_lock_ttl_seconds", lambda: 1)
    assert tasks.PeatLockHeartbeat("o").interval_seconds == 1
    monkeypatch.setattr(tasks, "get_lock_ttl_seconds", lambda: 10**6)
    assert tasks.PeatLockHeartbeat("o").interval_seconds == 300


def test_heartbeat_enter_fails_when_locks_missing(monkeypatch):
    monkeypatch.setattr(tasks, "refresh_owner_locks", lambda owner: ["k"])
    with pytest.raises(RuntimeError, match="Unable to refresh"):
        tasks.PeatLockHeartbeat("o").__enter__()


def test_heartbeat_refreshes_periodically_and_stops(settings, monkeypatch):
    import threading

    settings.PEAT_LOCK_HEARTBEAT_SECONDS = 1
    called = threading.Event()
    calls = []

    def refresh(owner):
        calls.append(owner)
        if len(calls) > 1:
            called.set()
        return []

    monkeypatch.setattr(tasks, "refresh_owner_locks", refresh)
    heartbeat = tasks.PeatLockHeartbeat("o")
    heartbeat.interval_seconds = 0.01
    with heartbeat:
        assert called.wait(2)
    assert not heartbeat._thread.is_alive()


@pytest.mark.parametrize("outcome", ["missing", "raises"])
def test_heartbeat_run_stops_on_missing_or_error(monkeypatch, outcome):
    def refresh(owner):
        if outcome == "raises":
            raise OSError("redis down")
        return ["k"]

    monkeypatch.setattr(tasks, "refresh_owner_locks", refresh)
    heartbeat = tasks.PeatLockHeartbeat("o")
    heartbeat.interval_seconds = 0
    heartbeat._run()  # returns instead of looping forever


def test_release_peat_locks_safely_swallows_errors(monkeypatch):
    def boom(owner):
        raise RuntimeError("x")

    monkeypatch.setattr(tasks, "release_owner_locks", boom)
    tasks._release_peat_locks_safely("o")


def test_queue_timeout_exceeded(settings):
    from datetime import datetime, timedelta, timezone

    settings.PEAT_LOCK_QUEUE_TIMEOUT_SECONDS = 0
    old = SimpleNamespace(created=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert tasks._queue_timeout_exceeded(old) is False
    settings.PEAT_LOCK_QUEUE_TIMEOUT_SECONDS = 60
    assert tasks._queue_timeout_exceeded(old) is True
    fresh = SimpleNamespace(created=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert tasks._queue_timeout_exceeded(fresh) is False


def test_peat_scan_found_no_devices(tmp_path):
    assert tasks.peat_scan_found_no_devices(tmp_path) is False  # no log
    log = tmp_path / "logs"
    log.mkdir()
    (log / "console.log").write_text("Scan failed: no results")
    assert tasks.peat_scan_found_no_devices(tmp_path) is True
    (log / "console.log").write_text("no hosts are active\nInvalid scan_type")
    assert tasks.peat_scan_found_no_devices(tmp_path) is False
    (log / "console.log").write_text("something else")
    assert tasks.peat_scan_found_no_devices(tmp_path) is False


def test_peat_results_cleanup(tmp_path):
    target = tmp_path / "results"
    target.mkdir()
    tasks.peat_results_cleanup(str(target))
    assert not target.exists()
    tasks.peat_results_cleanup(str(target))  # already gone: logged, no raise
    tasks.peat_results_cleanup(None)
