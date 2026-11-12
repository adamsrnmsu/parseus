"""Tests for PEAT signal handlers."""

import pytest

from parseus.webapp.pei import models, signals

pytestmark = pytest.mark.django_db


@pytest.fixture
def cleanup(monkeypatch, settings, tmp_path):
    settings.PEAT_PULL_DIR = tmp_path / "pulls"
    settings.PEAT_SCAN_DIR = tmp_path / "scans"
    calls = []
    monkeypatch.setattr(signals, "peat_results_cleanup", lambda **kw: calls.append(kw))
    monkeypatch.setattr(models.PeatRun, "stop", lambda self: calls.append("stop"))
    return calls, tmp_path


@pytest.mark.parametrize(
    "model, folder", [(models.PeatPull, "pulls"), (models.PeatScan, "scans")]
)
def test_delete_stops_run_then_cleans_its_directory(cleanup, model, folder):
    calls, tmp = cleanup
    run = model.objects.create(run_dir="r1", task_id="t")
    run.delete()
    assert calls == ["stop", {"result_path": str(tmp / folder / "r1")}]


def test_delete_without_run_dir_cleans_nothing(cleanup):
    calls, _ = cleanup
    models.PeatScan.objects.create(task_id="t").delete()
    assert calls == ["stop", {"result_path": None}]
