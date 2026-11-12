"""Characterization tests for PEAT models (PeatRun.stop, config/device helpers)."""

from types import SimpleNamespace

import pytest
import yaml
from kombu.exceptions import OperationalError

from parseus.webapp.pei import models
from parseus.webapp.pei.models import PeatConfig, PeatDevice, PeatScan

pytestmark = pytest.mark.django_db


@pytest.fixture
def stopenv(monkeypatch, settings, tmp_path):
    settings.PEAT_SCAN_DIR = tmp_path / "scans"
    settings.PEAT_PULL_DIR = tmp_path / "pulls"
    env = SimpleNamespace(
        revoked=[], killed=[], released=[], kill_error=None, revoke_error=None,
        release_error=None, tmp=tmp_path,
    )

    def revoke(task_id, terminate):
        assert terminate is False
        if env.revoke_error:
            raise env.revoke_error
        env.revoked.append(task_id)

    def terminate(pid):
        if env.kill_error:
            raise env.kill_error
        env.killed.append(pid)

    def release(owner):
        if env.release_error:
            raise env.release_error
        env.released.append(owner)

    monkeypatch.setattr(models.app.control, "revoke", revoke)
    monkeypatch.setattr(models, "terminate_process_group", terminate)
    monkeypatch.setattr(models, "release_owner_locks", release)
    monkeypatch.setattr(models, "current_process_host", lambda: "here")
    return env


def scan(status="running", task_id="tid", **kw):
    return PeatScan.objects.create(status=status, task_id=task_id, run_dir="r", **kw)


def console(env):
    return (env.tmp / "scans" / "r" / "logs" / "console.log").read_text()


def test_stop_running_without_process_revokes_but_keeps_locks(stopenv):
    run = scan()
    run.stop()
    assert PeatScan.objects.get(pk=run.pk).status == "stopped"
    assert stopenv.revoked == ["tid"]
    assert stopenv.released == []
    assert "Stop requested by user" in console(stopenv)
    assert "PEAT run stopped." in console(stopenv)


def test_stop_started_run_releases_locks(stopenv):
    run = scan(status="started")
    run.stop()
    assert stopenv.released == [f"PeatScan:{run.pk}:tid"]
    assert stopenv.revoked == ["tid"]


def test_stop_local_process_terminates_clears_and_releases(stopenv):
    run = scan(process_id=321, process_host="here")
    run.stop()
    fresh = PeatScan.objects.get(pk=run.pk)
    assert stopenv.killed == [321]
    assert stopenv.released == [f"PeatScan:{run.pk}:tid"]
    assert (fresh.process_id, fresh.process_host) == (None, None)


def test_stop_process_without_host_is_treated_local(stopenv):
    run = scan(process_id=5, process_host=None)
    run.stop()
    assert stopenv.killed == [5]


def test_stop_remote_process_is_left_to_owner(stopenv):
    run = scan(process_id=321, process_host="elsewhere")
    run.stop()
    fresh = PeatScan.objects.get(pk=run.pk)
    assert stopenv.killed == []
    assert stopenv.released == []
    assert (fresh.process_id, fresh.process_host) == (321, "elsewhere")
    assert fresh.status == "stopped"


def test_stop_missing_process_counts_as_finished(stopenv):
    stopenv.kill_error = ProcessLookupError()
    run = scan(process_id=9)
    run.stop()
    assert stopenv.released == [f"PeatScan:{run.pk}:tid"]
    assert PeatScan.objects.get(pk=run.pk).process_id is None


def test_stop_failed_termination_keeps_locks(stopenv):
    stopenv.kill_error = OSError("nope")
    run = scan(process_id=9)
    run.stop()
    assert stopenv.released == []
    assert PeatScan.objects.get(pk=run.pk).process_id is None


def test_stop_inactive_run_releases_locks_without_revoking(stopenv):
    run = scan(status="done")
    run.stop()
    assert stopenv.revoked == []
    assert stopenv.released == [f"PeatScan:{run.pk}:tid"]
    assert PeatScan.objects.get(pk=run.pk).status == "done"
    assert "Stop requested" not in (
        console(stopenv) if (stopenv.tmp / "scans" / "r").exists() else ""
    )


def test_stop_without_task_id_releases_nothing(stopenv):
    run = scan(status="started", task_id=None)
    run.stop()
    assert stopenv.revoked == [] and stopenv.released == []
    assert PeatScan.objects.get(pk=run.pk).status == "stopped"


def test_stop_survives_broker_and_release_failures(stopenv):
    stopenv.revoke_error = OperationalError("down")
    stopenv.release_error = RuntimeError("redis")
    run = scan(status="started")
    run.stop()
    assert PeatScan.objects.get(pk=run.pk).status == "stopped"


# ---- _append_console_message -------------------------------------------------


def test_append_console_message_pull_and_scan(stopenv):
    models.PeatPull.objects.create(run_dir="p1", task_id="a")._append_console_message("hi")
    assert (stopenv.tmp / "pulls" / "p1" / "logs" / "console.log").read_text() == "\nhi\n"
    run = scan()
    run._append_console_message("one")
    run._append_console_message("two")
    assert console(stopenv) == "\none\n\ntwo\n"


def test_append_console_message_noop_without_run_dir(stopenv):
    run = PeatScan.objects.create(task_id="x")
    run._append_console_message("hi")
    assert not (stopenv.tmp / "scans").exists()


def test_append_console_message_rejects_path_escape(stopenv):
    run = PeatScan.objects.create(task_id="x", run_dir="../../escape")
    run._append_console_message("hi")
    assert not (stopenv.tmp / "escape").exists()
    assert not (stopenv.tmp.parent / "escape").exists()


def test_append_console_message_unknown_method_is_noop(stopenv):
    fake = SimpleNamespace(run_dir="r", get_method=lambda: "other")
    assert models.PeatRun._append_console_message(fake, "hi") is None


def test_peatrun_helpers():
    run = PeatScan(run_dir="d", status="done")
    assert run.get_type() == "PeatScan"
    assert str(run) == "Directory: d; Status: done"


# ---- PeatDevice / PeatConfig -------------------------------------------------


def test_device_identifiers_and_config_data():
    dev = PeatDevice(
        label="L", comment="c", ip="1.2.3.4", hostname="h", mac="aa", serial_port="/dev/x",
        name="n", peat_module="sel", options={"a": 1},
    )
    assert dev._get_identifiers() == {
        "ip": "1.2.3.4", "hostname": "h", "mac": "aa", "serial_port": "/dev/x", "name": "n",
    }
    assert dev.get_config_data() == {
        "label": "L", "comment": "c", "identifiers": dev._get_identifiers(),
        "peat_module": "sel", "options": {"a": 1},
    }
    bare = PeatDevice(label="L", comment="c", options={})
    assert bare._get_identifiers() == {}
    assert "options" not in bare.get_config_data()


def test_config_roundtrip_file_and_hosts(settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.MEDIA_ROOT = tmp_path
    body = b"hosts:\n- label: A\n  identifiers: {ip: 10.0.0.1}\n  peat_module: sel\n"
    cfg = PeatConfig.objects.create(
        name="cfg", author="me", debug=2, filename=SimpleUploadedFile("c.yaml", body)
    )
    assert [h.label for h in cfg.hosts.all()] == ["A"]
    data = yaml.safe_load(open(cfg.filename.path, encoding="utf-8"))
    assert data["metadata"]["name"] == "cfg"
    assert data["debug"] == 2 and data["assume_online"] is False
    assert data["hosts"][0]["identifiers"] == {"ip": "10.0.0.1"}
    # deleting the only host removes the hosts key from the file on next save
    cfg.hosts.all().delete()
    cfg.save()
    assert "hosts" not in yaml.safe_load(open(cfg.filename.path, encoding="utf-8"))


def test_validate_yaml_file_rejects_bad_yaml():
    from io import BytesIO

    from django.core.exceptions import ValidationError

    models.validate_yaml_file(BytesIO(b"a: 1"))
    with pytest.raises(ValidationError):
        models.validate_yaml_file(BytesIO(b"a: [1"))
