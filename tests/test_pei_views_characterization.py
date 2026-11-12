"""Characterization tests for pei/views.py helpers and views.

Hermetic: no Elasticsearch, Redis, Celery or network.  Views are called
directly with RequestFactory; external collaborators are monkeypatched.
"""

import io
import json
import zipfile
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import RequestFactory

from parseus.webapp.pei import views
from parseus.webapp.pei.models import PeatConfig, PeatPull, PeatScan

rf = RequestFactory()


def _cfg():
    """Create a config without firing the post_save file-reading signal."""
    return PeatConfig.objects.bulk_create([PeatConfig(name="c", filename="nofile.yml")])[0]


@pytest.fixture(autouse=True)
def _no_broker_or_redis(monkeypatch):
    """Deleting/stopping runs would otherwise wait on RabbitMQ/Redis timeouts."""
    from parseus.webapp.pei import models

    monkeypatch.setattr(models.app.control, "revoke", lambda *a, **k: None)
    monkeypatch.setattr(models, "release_owner_locks", lambda owner: None)


def _json_post(body, path="/x"):
    return rf.post(path, data=json.dumps(body), content_type="application/json")


def _body(response):
    return json.loads(response.content)


# ---------------------------------------------------------------- pure helpers


def test_convert_tuples_to_dict():
    data = {"a": [("k", "v"), ("x", "y", "z")], "b": 5, "c": ("only",)}
    assert views.convert_tuples_to_dict(data) == {
        "a": [{"k": "v"}, ["x", "y", "z"]],
        "b": 5,
        "c": ["only"],
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"),
        (2 * 1024**4, "2.0 TB"),
        (3 * 1024**5, "3072.0 TB"),
        (None, "—"),
        ("junk", "—"),
        ("2048", "2.0 KB"),
    ],
)
def test_human_file_size(value, expected):
    assert views._human_file_size(value) == expected


def test_parse_default_targets():
    assert views.parse_default_targets(["a", " b ", ""]) == ["a", "b"]
    assert views.parse_default_targets("a, b\nc,,") == ["a", "b", "c"]
    assert views.parse_default_targets(None) == []


def test_parse_default_modules(monkeypatch):
    monkeypatch.setattr(
        views.module_api,
        "lookup_names",
        lambda names: {"foo": ["Foo", "FooB"], "bar": ["Bar"]}.get(names[0], []),
    )
    assert views.parse_default_modules(None) == ["all"]
    assert views.parse_default_modules("") == ["all"]
    assert views.parse_default_modules(["foo", "ALL"]) == ["all"]
    assert views.parse_default_modules("foo,bar") == ["Bar", "Foo", "FooB"]
    with pytest.raises(ValidationError) as err:
        views.parse_default_modules(["foo", "nope", "zip"])
    assert "Unknown PEAT module selection: nope, zip" in err.value.messages[0]


def test_get_peat_run_name(monkeypatch):
    assert views.get_peat_run_name("pull", "my run!") == "my run_"
    assert views.get_peat_run_name("scan", "").startswith("peat_scan_")


def _dev(**kw):
    base = dict(ip="", hostname="", serial_port="", label="", pk=1)
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_devices(monkeypatch, devices):
    monkeypatch.setattr(
        views,
        "PeatDevice",
        SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: devices)),
    )


def test_selected_targets_network(monkeypatch):
    _patch_devices(
        monkeypatch,
        [_dev(ip="10.0.0.1", label="A"), _dev(hostname="h.local")],
    )
    assert views.get_selected_config_targets(1, [1, 2]) == (
        ["A: 10.0.0.1", "h.local"],
        ["10.0.0.1", "h.local"],
        "ip",
    )


def test_selected_targets_serial(monkeypatch):
    _patch_devices(monkeypatch, [_dev(serial_port="/dev/ttyS0", label="S")])
    assert views.get_selected_config_targets(1, [1]) == (
        ["S: /dev/ttyS0"],
        ["/dev/ttyS0"],
        "serial",
    )


def test_selected_targets_unsupported_and_mixed(monkeypatch):
    _patch_devices(monkeypatch, [_dev(label="Nope"), _dev(pk=9)])
    with pytest.raises(views.PeatTargetSelectionError, match="Nope, 9"):
        views.get_selected_config_targets(1, [1])
    _patch_devices(monkeypatch, [_dev(ip="1.1.1.1"), _dev(serial_port="/dev/x")])
    with pytest.raises(views.PeatTargetSelectionError, match="mix network"):
        views.get_selected_config_targets(1, [1])


def test_module_defaults(monkeypatch):
    monkeypatch.setattr(views, "module_api", SimpleNamespace(names=["Foo"]))
    monkeypatch.setattr(
        views,
        "generate_simple_config",
        lambda hosts: "hosts:\n- options:\n    k: v\n",
    )
    assert _body(views.get_peat_module_defaults(None, "foo")) == {"k": "v"}
    resp = views.get_peat_module_defaults(None, "zzz")
    assert resp.status_code == 404
    monkeypatch.setattr(views, "generate_simple_config", lambda hosts: "")
    assert _body(views.get_peat_module_defaults(None, "Foo")) == {}


# ------------------------------------------------------------- filesystem


def _make_run(root, summary_name="pull-summary.json", summary=None):
    (root / "summaries").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "summaries" / summary_name).write_text(json.dumps(summary or {}))
    (root / "logs" / "console.log").write_text("hello")
    dev = root / "devices" / "d1"
    dev.mkdir(parents=True)
    (dev / "device-data-summary.json").write_text(
        json.dumps(
            {
                "ip": "1.2.3.4",
                "type": "PLC",
                "description": {"full": "Full desc", "vendor": {"name": "ACME"}},
                "firmware": {"revision": "r2"},
                "hardware": {"id": "hw"},
                "os": {"name": "RTOS"},
                "service": [1, 2],
                "register": [1],
                "related": {"files": [1], "ip": [1, 2], "protocols": []},
            }
        )
    )
    (dev / "extra.txt").write_text("x")


def test_safe_relative_file(tmp_path):
    root = tmp_path.resolve()
    f = root / "a.txt"
    f.write_text("abc")
    info = views._safe_relative_file(root, f)
    assert info["path"] == "a.txt"
    assert info["name"] == "a.txt"
    assert info["size"] == 3
    assert info["size_display"] == "3 B"
    assert "view_url" not in info
    info = views._safe_relative_file(root, f, "pull", 4)
    assert info["view_url"].endswith("/pull/4/?path=a.txt")
    assert info["download_url"].endswith("path=a.txt&download=1")
    assert views._safe_relative_file(root, root / "missing") is None
    other = tmp_path.parent / "outside.txt"
    other.write_text("x")
    assert views._safe_relative_file(root, other) is None


def test_read_device_summary(tmp_path):
    root = tmp_path.resolve()
    _make_run(root)
    d1 = root / "devices" / "d1"
    got = views._read_device_summary(d1, root, "pull", 3)
    assert got["id"] == "1.2.3.4"
    assert got["directory"] == "devices/d1"
    assert got["type"] == "PLC"
    assert got["description"] == "Full desc"
    assert got["vendor"] == "ACME"
    assert got["os"] == "RTOS"
    assert got["firmware"] == "r2"
    assert got["hardware"] == "hw"
    assert got["mac"] == ""
    assert got["counts"] == {
        "files": 2,
        "services": 2,
        "interfaces": 0,
        "events": 0,
        "registers": 1,
        "tags": 0,
        "io": 0,
        "users": 0,
        "related_files": 1,
        "related_ips": 2,
        "related_protocols": 0,
    }
    assert [f["name"] for f in got["top_files"]] == [
        "device-data-summary.json",
        "extra.txt",
    ]
    assert "view_url" in got["top_files"][0]
    # no summary file / non-dict summary
    assert views._read_device_summary(root / "logs", root) is None
    (d1 / "device-data-summary.json").write_text("[1]")
    assert views._read_device_summary(d1, root) is None


def test_read_device_summary_id_fallbacks(tmp_path):
    root = tmp_path.resolve()
    d = root / "dd"
    d.mkdir()
    summary = d / "device-data-summary.json"
    summary.write_text(json.dumps({"label": "L", "ip": "1.1.1.1"}))
    assert views._read_device_summary(d, root)["id"] == "L"
    summary.write_text(json.dumps({"hostname": "h", "serial_port": "p"}))
    assert views._read_device_summary(d, root)["id"] == "h"
    summary.write_text(json.dumps({"serial_port": "p"}))
    assert views._read_device_summary(d, root)["id"] == "p"
    summary.write_text(json.dumps({"description": {"product": "Prod"}, "os": {"full": "F"},
                                   "firmware": {"version": "v"}, "hardware": {"version": "hv"}}))
    got = views._read_device_summary(d, root)
    assert (got["id"], got["description"], got["os"], got["firmware"], got["hardware"]) == (
        "dd", "Prod", "F", "v", "hv")


def test_build_run_file_tree(tmp_path):
    root = tmp_path.resolve()
    _make_run(root)
    tree = views._build_run_file_tree(root, "pull", 9)
    assert tree["total_files"] == 4
    assert tree["total_dirs"] == 4
    assert tree["files_by_section"] == {"summaries": 1, "logs": 1, "devices": 2}
    assert tree["total_size"] == 2 + 5 + 1 + len(
        (root / "devices" / "d1" / "device-data-summary.json").read_bytes()
    )
    assert tree["truncated"] is False
    kinds = {n["path"]: n["type"] for n in tree["nodes"]}
    assert kinds["devices"] == "dir" and kinds["logs/console.log"] == "file"
    node = next(n for n in tree["nodes"] if n["path"] == "logs/console.log")
    assert node["size"] == 5 and node["size_display"] == "5 B"
    assert "path=logs%2Fconsole.log" in node["view_url"]
    assert "download=1" in node["download_url"]
    limited = views._build_run_file_tree(root, limit=3)
    assert len(limited["nodes"]) == 3 and limited["truncated"] is True
    assert limited["total_files"] == 4  # totals still count everything
    assert all("view_url" not in n for n in limited["nodes"])


def test_build_run_file_tree_skips_symlinks(tmp_path):
    root = tmp_path.resolve() / "r"
    root.mkdir()
    (root / "real.txt").write_text("x")
    (root / "link.txt").symlink_to(root / "real.txt")
    tree = views._build_run_file_tree(root)
    assert [n["path"] for n in tree["nodes"]] == ["real.txt"]


def _run_obj(**kw):
    base = dict(run_dir="run1", status="done", name="n", created=None,
                devices=["t1"], modules=["m"], config=None, config_id=None, pk=7)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def run_dirs(settings, tmp_path):
    settings.PEAT_PULL_DIR = str(tmp_path / "pull")
    settings.PEAT_SCAN_DIR = str(tmp_path / "scan")
    (tmp_path / "pull").mkdir()
    (tmp_path / "scan").mkdir()
    return tmp_path


def _outcome(details):
    return (details["outcome"]["severity"], details["outcome"]["title"])


def test_run_details_unavailable_states(run_dirs):
    d = views._get_peat_run_details(_run_obj(run_dir=None), "pull")
    assert _outcome(d) == ("info", "Run data is not available yet.")
    assert d["output_exists"] is False and d["targets"] == ["t1"]
    assert d["tree"]["total_size_display"] == "0 B"
    d = views._get_peat_run_details(_run_obj(status="waiting"), "pull")
    assert _outcome(d) == ("info", "Run is waiting for target locks.")
    d = views._get_peat_run_details(_run_obj(run_dir="../escape"), "pull")
    assert _outcome(d) == ("danger", "Invalid run directory.")
    d = views._get_peat_run_details(_run_obj(run_dir="gone"), "pull")
    assert _outcome(d) == ("warning", "Run files are not available.")


def test_run_details_with_data(run_dirs):
    import datetime

    root = run_dirs / "scan" / "run1"
    _make_run(root, "scan-summary.json",
              {"hosts_verified": [1, 2, 3], "num_hosts_online": 5, "num_hosts_verified": 3})
    cfg = SimpleNamespace(name="cfgname")
    obj = _run_obj(config=cfg, config_id=2, created=datetime.datetime(2024, 1, 2, 3, 4, 5))
    d = views._get_peat_run_details(obj, "scan")
    assert d["config"] == {"id": 2, "name": "cfgname"}
    assert d["created"] == "2024-01-02T03:04:05"
    assert d["output_exists"] and d["summary_available"]
    assert _outcome(d) == ("success", "Run completed with device data.")
    assert [f["path"] for f in d["important_files"]] == [
        "summaries/scan-summary.json", "logs/console.log"]
    assert [x["id"] for x in d["devices"]] == ["1.2.3.4"]
    assert d["counts"] == {
        "targets": 1, "devices": 3, "files": 4, "directories": 4, "logs": 1,
        "summaries": 1, "metadata": 0, "elastic": 0, "hosts_online": 5,
        "hosts_verified": 3,
    }
    assert d["tree"]["total_files"] == 4


def test_run_details_pull_uses_pull_results_and_root_device(run_dirs):
    root = run_dirs / "pull" / "run1"
    root.mkdir()
    (root / "device-data-summary.json").write_text(json.dumps({"ip": "9.9.9.9"}))
    d = views._get_peat_run_details(_run_obj(), "pull")
    assert [x["id"] for x in d["devices"]] == ["9.9.9.9"]
    assert d["summary_available"] is False
    assert d["counts"]["hosts_online"] is None
    (root / "summaries").mkdir()
    (root / "summaries" / "pull-summary.json").write_text(
        json.dumps({"pull_results": [1, 2, 3]}))
    d = views._get_peat_run_details(_run_obj(), "pull")
    assert d["counts"]["devices"] == 3


@pytest.mark.parametrize(
    "method,status,with_dev,expected",
    [
        ("scan", "error", False, ("warning", "No devices were identified.")),
        ("scan", "done", False, ("warning", "No devices found in this run.")),
        ("pull", "done", False, ("info", "No devices found in this run.")),
        ("pull", "stopped", False, ("info", "No devices found in this run.")),
        ("pull", "error", True, ("danger", "Run ended with an error.")),
        ("pull", "stopped", True, ("warning", "Run was stopped.")),
        ("pull", "running", True, ("info", "Run is still in progress.")),
        ("pull", "started", True, ("info", "Run is still in progress.")),
        ("pull", "done", True, ("success", "Run completed with device data.")),
    ],
)
def test_run_details_outcomes(run_dirs, method, status, with_dev, expected):
    root = run_dirs / method / "run1"
    if with_dev:
        _make_run(root)
    else:
        root.mkdir()
    d = views._get_peat_run_details(_run_obj(status=status), method)
    assert _outcome(d) == expected


# ------------------------------------------------------------- DB-backed views


@pytest.mark.django_db
def test_get_run_objects():
    cfg = _cfg()
    PeatPull.objects.create(name="p1", config=cfg, task_id="t1")
    PeatPull.objects.create(name="p2", task_id="t2")
    PeatScan.objects.create(name="s1", task_id="t3")

    def call(method, body):
        return views.get_run_objects(_json_post(body), method)

    assert {r["name"] for r in _body(call("pull", {}))} == {"p1", "p2"}
    assert [r["name"] for r in _body(call("pull", {"config": cfg.pk}))] == ["p1"]
    assert [r["name"] for r in _body(call("pull", {"default_only": True}))] == ["p2"]
    assert [r["name"] for r in _body(call("scan", {}))] == ["s1"]
    with pytest.raises(Http404):
        call("other", {})


@pytest.mark.django_db
def test_results_view_post():
    pull = PeatPull.objects.create(name="p", task_id="t1", run_dir="rd", status="done")
    scan = PeatScan.objects.create(name="s", task_id="t2")
    view = views.PeatResultsView()
    r = view.post(_json_post({"pk": pull.pk, "method": "pull"}))
    assert _body(r) == {"status": "done", "run_dir": "rd"}
    r = view.post(_json_post({"pk": scan.pk, "method": "scan"}))
    assert _body(r) == {"status": "started", "run_dir": None}
    r = view.post(rf.post("/x", data="{bad", content_type="application/json"))
    assert r.status_code == 400
    with pytest.raises(Http404):
        view.post(_json_post({"pk": 1, "method": "zzz"}))
    with pytest.raises(Http404):
        view.post(_json_post({"pk": 9999, "method": "pull"}))


@pytest.mark.django_db
def test_run_file_context_and_peat_run_file(run_dirs):
    pull = PeatPull.objects.create(name="p", task_id="t1", run_dir="run1")
    root = (run_dirs / "pull" / "run1")
    root.mkdir()
    (root / "a.log").write_text("log text")
    (root / "b.bin").write_bytes(b"\x00\x01")
    (root / "c.zzz").write_text("zz")
    (root / "real").write_text("r")
    (root / "link").symlink_to(root / "real")

    def call(path=None, download=False, method="pull", pk=None):
        q = {}
        if path is not None:
            q["path"] = path
        if download:
            q["download"] = "1"
        req = rf.get("/x", q)
        return views.peat_run_file(req, method, pk or pull.pk)

    r = call("a.log")
    assert r["Content-Type"] == "text/x-log" or r["Content-Type"].startswith("text/")
    assert r["X-Content-Type-Options"] == "nosniff"
    assert b"".join(r.streaming_content) == b"log text"
    assert not r.has_header("Content-Disposition") or "attachment" not in r["Content-Disposition"]
    r = call("b.bin")
    assert r["Content-Type"] == "application/octet-stream"
    r = call("c.zzz")
    assert r["Content-Type"] == "application/octet-stream"
    r = call("a.log", download=True)
    assert "attachment" in r["Content-Disposition"] and "a.log" in r["Content-Disposition"]
    r = call("c.zzz", download=True)
    assert r["Content-Type"] == "application/octet-stream"
    for bad in ("", "nope.log", "../x", "."):
        with pytest.raises(Http404):
            call(bad)
    assert b"".join(call("link").streaming_content) == b"r"  # symlink inside root is served
    with pytest.raises(Http404):
        call()
    with pytest.raises(Http404):
        call("a.log", method="zzz")
    with pytest.raises(Http404):
        call("a.log", pk=9999)
    # run dir absent / empty / escaping
    for name, run_dir in [("e", None), ("f", "../x"), ("g", "missing")]:
        obj = PeatScan.objects.create(name=name, task_id=name, run_dir=run_dir)
        with pytest.raises(Http404):
            call("a.log", method="scan", pk=obj.pk)
    obj, got_root = views._get_run_file_context("pull", pull.pk)
    assert obj.pk == pull.pk and got_root == root.resolve()


@pytest.mark.django_db
def test_peat_run_stop(monkeypatch):
    pull = PeatPull.objects.create(name="p", task_id="t1")
    scan = PeatScan.objects.create(name="s", task_id="t2")

    def fake_stop(self):
        self.status = "stopped"
        self.save(update_fields=["status"])

    monkeypatch.setattr(PeatPull, "stop", fake_stop)
    monkeypatch.setattr(PeatScan, "stop", fake_stop)
    r = views.peat_run_stop(_json_post({"pk": pull.pk, "pull_or_scan": "pull"}))
    assert _body(r) == {"message": "success", "status": "stopped"}
    r = views.peat_run_stop(_json_post({"pk": scan.pk, "pull_or_scan": "scan"}))
    assert _body(r)["status"] == "stopped"
    assert views.peat_run_stop(
        rf.post("/x", data="{", content_type="application/json")).status_code == 400
    with pytest.raises(Http404):
        views.peat_run_stop(_json_post({"pk": 1, "pull_or_scan": "x"}))
    with pytest.raises(Http404):
        views.peat_run_stop(_json_post({"pk": 9999, "pull_or_scan": "pull"}))


@pytest.mark.django_db
def test_peat_run_download(run_dirs):
    def call(body):
        return views.peat_run_download(_json_post(body))

    pull = PeatPull.objects.create(name="p", task_id="t1", run_dir="my run!")
    root = run_dirs / "pull" / "my run!"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("AAA")
    (root / "real").write_text("R")
    (root / "link").symlink_to(root / "real")
    r = call({"pk": pull.pk, "pull_or_scan": "pull"})
    assert r.status_code == 200
    assert r["Content-Type"] == "application/zip"
    assert r["Content-Disposition"] == "attachment; filename=my run_.zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["real", "sub/a.txt"]
        assert zf.read("sub/a.txt") == b"AAA"

    # name that cleans to empty falls back
    PeatPull.objects.filter(pk=pull.pk).update(run_dir="  ")
    fallback = run_dirs / "pull" / "  "
    fallback.mkdir()
    (fallback / "f").write_text("x")
    r = call({"pk": pull.pk, "pull_or_scan": "pull"})
    assert r["Content-Disposition"] == "attachment; filename=peat-run.zip"

    scan = PeatScan.objects.create(name="s", task_id="t2", run_dir="sr")
    assert call({"pk": scan.pk, "pull_or_scan": "scan"}).status_code == 404  # no dir
    (run_dirs / "scan" / "sr").mkdir()
    assert call({"pk": scan.pk, "pull_or_scan": "scan"}).status_code == 404  # empty
    PeatScan.objects.filter(pk=scan.pk).update(run_dir=None)
    assert call({"pk": scan.pk, "pull_or_scan": "scan"}).status_code == 404
    PeatScan.objects.filter(pk=scan.pk).update(run_dir="../x")
    r = call({"pk": scan.pk, "pull_or_scan": "scan"})
    assert r.status_code == 400 and _body(r)["message"] == "Invalid run directory."
    assert views.peat_run_download(
        rf.post("/x", data="{", content_type="application/json")).status_code == 400
    with pytest.raises(Http404):
        call({"pk": 1, "pull_or_scan": "x"})


@pytest.mark.django_db
def test_run_view_get_and_delete(run_dirs):
    pull = PeatPull.objects.create(name="p", task_id="t1")
    view = views.PeatPullView()
    d = _body(view.get(rf.get("/x", {"pk": pull.pk})))
    assert d["method"] == "pull" and d["status"] == "started"
    assert _outcome(d) == ("info", "Run data is not available yet.")
    r = view.delete(rf.delete("/x", data=json.dumps({"ids": [pull.pk]}),
                              content_type="application/json"))
    assert r.status_code == 204
    assert PeatPull.objects.count() == 0
    with pytest.raises(Http404):
        view.get(rf.get("/x", {"pk": 12345}))


@pytest.mark.django_db
def test_config_list_delete_and_detail_post():
    cfg = _cfg()
    PeatPull.objects.create(name="p", task_id="t1", config=cfg)
    detail = views.PeatConfigDetailView()
    detail.kwargs = {"pk": cfg.pk}
    detail.request = rf.post("/x")
    assert [p["name"] for p in _body(detail.post(detail.request))] == ["p"]
    r = views.PeatConfigListView().delete(
        rf.delete("/x", data=json.dumps({"ids": [cfg.pk]}), content_type="application/json"))
    assert r.status_code == 204 and PeatConfig.objects.count() == 0


# ------------------------------------------------------------- builder form


def _builder(post, monkeypatch):
    monkeypatch.setattr(
        views, "generate_simple_config",
        lambda devices: "hosts:\n" + "".join(f"- name: {d[1]}\n  ip: {d[0]}\n" for d in devices))
    view = views.PeatBuilderView()
    view.request = rf.post("/x", post)
    view.form_invalid = lambda form: "INVALID"
    instance = SimpleNamespace(pk=5, filename=mock.MagicMock(), save=mock.MagicMock())
    form = mock.MagicMock()
    form.save.return_value = instance
    return view, form, instance


def test_builder_form_valid(monkeypatch):
    view, form, instance = _builder({
        "device-name": ["a", "b"], "device-ip": ["1.1.1.1", "2.2.2.2"],
        "device-module": ["m", "m"], "device-options": ['{"x": 1}', "  "]}, monkeypatch)
    resp = view.form_valid(form)
    assert resp.status_code == 302 and resp.url.endswith("/5")
    name, content = instance.filename.save.call_args.args[:2]
    assert name.startswith("autobuilt_config_") and name.endswith(".yml")
    text = content.read().decode()
    assert "options:\n    x: 1" in text and "name: b" in text
    assert text.count("options") == 1
    assert text.index("name: a") < text.index("ip: 1.1.1.1")  # key order kept
    assert "{" not in text  # block style
    form.save.assert_any_call(commit=False)
    assert instance.filename.save.call_args.kwargs == {"save": False}
    instance.save.assert_called_once()


def test_builder_ignores_options_without_matching_host(monkeypatch):
    view, form, instance = _builder({
        "device-name": ["a"], "device-ip": ["1.1.1.1"],
        "device-module": ["m"], "device-options": ["", '{"x": 1}']}, monkeypatch)
    assert view.form_valid(form).status_code == 302
    assert "options" not in instance.filename.save.call_args.args[1].read().decode()


def test_builder_form_valid_bad_json(monkeypatch):
    view, form, instance = _builder({
        "device-name": ["a"], "device-ip": ["1.1.1.1"],
        "device-module": ["m"], "device-options": ["{bad"]}, monkeypatch)
    assert view.form_valid(form) == "INVALID"
    assert "Device 1 has invalid JSON" in form.add_error.call_args.args[1]
    instance.save.assert_not_called()


# ------------------------------------------------------------- pull / scan post


class _Lock:
    def __init__(self, acquired=True, busy=(), targets=()):
        self.acquired, self.busy_targets, self.targets = acquired, list(busy), list(targets)


@pytest.fixture
def post_env(monkeypatch):
    calls = SimpleNamespace(apply=[], acquire=[], released=[])
    for task in ("peat_pull", "peat_scan"):
        def apply_async(args, task_id=None, _t=task):
            calls.apply.append((_t, args, task_id))
            return SimpleNamespace(id="celery-" + task_id[:4])
        monkeypatch.setattr(views, task, SimpleNamespace(apply_async=apply_async))
    calls.lock = _Lock(True, targets=["9.9.9.9"])
    def acquire(owner, targets, target_mode):
        calls.acquire.append((owner, list(targets), target_mode))
        return calls.lock
    monkeypatch.setattr(views, "acquire_target_locks", acquire)
    monkeypatch.setattr(views, "release_owner_locks", lambda o: calls.released.append(o))
    monkeypatch.setattr(views, "build_owner_token_for_run", lambda o: f"owner-{o.pk}")
    monkeypatch.setattr(
        views, "resolve_lock_targets",
        lambda targets, mode: [SimpleNamespace(target="r-" + t) for t in targets])
    monkeypatch.setattr(views.module_api, "lookup_names", lambda names: [names[0].title()])
    return calls


VIEWS = [("pull", views.PeatPullView, PeatPull, "peat_pull"),
         ("scan", views.PeatScanView, PeatScan, "peat_scan")]


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_default_run_success(post_env, kind, view_cls, model, task):
    r = view_cls().post(_json_post(
        {"cidr_id": "10.0.0.1, 10.0.0.2", "device_types": "foo", "peat_run_name": "run a"}))
    body = _body(r)
    assert body["status"] is True and body["message"].startswith("celery-")
    obj = model.objects.get()
    assert body["pk"] == obj.pk
    assert (obj.name, obj.run_dir, obj.devices, obj.modules, obj.status) == (
        "run a", "run a", ["10.0.0.1", "10.0.0.2"], ["Foo"], "started")
    assert post_env.acquire == [(f"owner-{obj.pk}", ["10.0.0.1", "10.0.0.2"], "ip")]
    t, args, task_id = post_env.apply[0]
    assert t == task and task_id == obj.task_id
    if kind == "pull":
        assert args == (obj.pk, ["9.9.9.9"], None, "ip")
    else:
        assert args == (obj.pk, ["9.9.9.9"], None, None, False, "ip")


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_queue_if_busy(post_env, kind, view_cls, model, task):
    r = view_cls().post(_json_post(
        {"cidr_id": "10.0.0.1", "queue_if_busy": True, "intensive_scan": True}))
    body = _body(r)
    obj = model.objects.get()
    assert body == {"status": True, "message": body["message"], "pk": obj.pk,
                    "queued_for_locks": True}
    assert obj.status == "waiting"
    assert post_env.acquire == []
    args = post_env.apply[0][1]
    if kind == "pull":
        assert args == (obj.pk, ["r-10.0.0.1"], None, "ip", True, False)
    else:
        assert args == (obj.pk, ["r-10.0.0.1"], None, None, True, "ip", True, False)


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_busy_targets(post_env, kind, view_cls, model, task):
    post_env.lock = _Lock(False, busy=["10.0.0.1"])
    r = view_cls().post(_json_post({"cidr_id": "10.0.0.1"}))
    body = _body(r)
    assert body["reason"] == "targets_busy" and body["can_queue"] is True
    assert body["busy_targets"] == ["10.0.0.1"] and body["status"] is False
    assert "already in use: 10.0.0.1" in body["message"]
    assert model.objects.count() == 0 and post_env.apply == []


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_input_errors(post_env, monkeypatch, kind, view_cls, model, task):
    body = _body(view_cls().post(_json_post({"cidr_id": ""})))
    assert body == {"status": False,
                    "message": f"Provide at least one host or CIDR target to {kind}."}
    monkeypatch.setattr(views.module_api, "lookup_names", lambda names: [])
    body = _body(view_cls().post(_json_post({"cidr_id": "1.1.1.1", "device_types": "bad"})))
    assert body["status"] is False and "Unknown PEAT module selection: bad" in body["message"]
    assert model.objects.count() == 0
    # Validation error from the model (name too long)
    body = _body(view_cls().post(_json_post({"cidr_id": "1.1.1.1", "peat_run_name": "x" * 300})))
    assert body["status"] is False and model.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_config_run(post_env, monkeypatch, kind, view_cls, model, task):
    cfg = _cfg()
    monkeypatch.setattr(
        views, "get_selected_config_targets",
        lambda pk, ids: (["Lbl: 10.0.0.5"], ["10.0.0.5"], "ip"))
    post_env.lock = _Lock(False, busy=["r-10.0.0.5"])
    body = _body(view_cls().post(_json_post({"config": cfg.pk, "device_ids": [1]})))
    # busy: label map applies only for config runs; resolved target not in map -> raw
    assert body["reason"] == "targets_busy"
    post_env.lock = _Lock(True, targets=["10.0.0.5"])
    body = _body(view_cls().post(_json_post({"config": cfg.pk, "device_ids": [1]})))
    assert body["status"] is True
    obj = model.objects.get()
    assert obj.devices == ["Lbl: 10.0.0.5"] and obj.config_id == cfg.pk and obj.modules == []

    def boom(pk, ids):
        raise views.PeatTargetSelectionError("mixed!")
    monkeypatch.setattr(views, "get_selected_config_targets", boom)
    body = _body(view_cls().post(_json_post({"config": cfg.pk, "device_ids": [1]})))
    assert body == {"status": False, "message": "mixed!"}


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_push_database(post_env, monkeypatch, kind, view_cls, model, task):
    from django.http import JsonResponse
    monkeypatch.setattr(views, "get_database_status",
                        lambda request: JsonResponse({"status": False, "message": "down"}))
    r = view_cls().post(_json_post({"cidr_id": "1.1.1.1", "peat_run_push": True}))
    assert _body(r) == {"status": False, "message": "down"}
    assert model.objects.count() == 0
    monkeypatch.setattr(views, "get_database_status",
                        lambda request: JsonResponse({"status": True}))
    monkeypatch.setattr(views, "get_database_url", lambda request: "http://es")
    view_cls().post(_json_post({"cidr_id": "1.1.1.1", "peat_run_push": True}))
    assert post_env.apply[0][1][2] == "http://es"


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_lock_error(post_env, monkeypatch, kind, view_cls, model, task):
    def raise_lock(owner, targets, target_mode):
        raise views.PeatLockError("redis down")
    monkeypatch.setattr(views, "acquire_target_locks", raise_lock)
    body = _body(view_cls().post(_json_post({"cidr_id": "1.1.1.1"})))
    assert body == {"status": False, "message": "redis down"}
    assert model.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_unexpected_error(post_env, monkeypatch, run_dirs, kind, view_cls, model, task):
    def boom(args, task_id=None):
        raise RuntimeError("broker gone")
    monkeypatch.setattr(views, task, SimpleNamespace(apply_async=boom))
    body = _body(view_cls().post(_json_post({"cidr_id": "1.1.1.1", "peat_run_name": "err"})))
    assert body == {"status": False, "message": f"Unable to queue PEAT {kind} task."}
    obj = model.objects.get()
    assert obj.status == "error"
    assert post_env.released == [f"owner-{obj.pk}"]
    console = run_dirs / kind / "err" / "logs" / "console.log"
    assert f"Failed to queue PEAT {kind} task: broker gone" in console.read_text()


def test_helper_safety_wrappers(monkeypatch):
    views._release_peat_locks_safely(None)

    def bad(owner):
        raise RuntimeError("x")
    monkeypatch.setattr(views, "release_owner_locks", bad)
    views._release_peat_locks_safely("o")  # swallowed
    views._delete_unscheduled_run(SimpleNamespace(pk=None))
    class Bad:
        pk = 1
        def delete(self):
            raise RuntimeError("x")
    views._delete_unscheduled_run(Bad())  # swallowed
    assert views._format_busy_lock_message([str(i) for i in range(12)]).endswith(
        "9, and 2 more")
    assert views._format_busy_target_display("L: h", "h", "1.1.1.1") == "L: h (1.1.1.1)"
    assert views._format_busy_target_display("L: 1.1.1.1", "1.1.1.1", "1.1.1.1") == "L: 1.1.1.1"


def test_tree_truncation_boundaries(tmp_path):
    root = tmp_path.resolve()
    (root / "a").write_text("1")
    (root / "b").write_text("1")
    assert views._build_run_file_tree(root, limit=2)["truncated"] is False
    assert views._build_run_file_tree(root, limit=100)["truncated"] is False
    assert views._build_run_file_tree(root, limit=1)["truncated"] is True
    assert views._build_run_file_tree(root)["truncated"] is False


def test_safe_relative_file_urls_need_method_and_pk(tmp_path):
    root = tmp_path.resolve()
    (root / "a").write_text("1")
    assert "view_url" not in views._safe_relative_file(root, root / "a", "pull", None)
    assert "view_url" not in views._safe_relative_file(root, root / "a", None, 4)


def test_device_files_skip_symlinks(tmp_path):
    root = tmp_path.resolve()
    d = root / "d"
    d.mkdir()
    (d / "real").write_text("1")
    (d / "link").symlink_to(d / "real")
    assert [f["name"] for f in views._list_device_files(d, root, None, None)] == ["real"]


def test_busy_message_and_display_boundaries():
    assert views._format_busy_lock_message([str(i) for i in range(10)]).endswith("9")
    assert views._format_busy_lock_message([str(i) for i in range(11)]).endswith("and 1 more")
    # display lacks the resolved target but request equals resolved: keep label
    assert views._format_busy_target_display("Label", "h", "h") == "Label"
    assert views._format_busy_target_display("Label", "", "h") == "Label"


def test_viewable_known_type_keeps_mimetype(run_dirs):
    pull = PeatPull(pk=1, run_dir="r")
    (run_dirs / "pull" / "r").mkdir()
    (run_dirs / "pull" / "r" / "d.json").write_text("{}")
    with mock.patch.object(views, "_get_run_file_context",
                           return_value=(pull, (run_dirs / "pull" / "r").resolve())):
        r = views.peat_run_file(rf.get("/x", {"path": "d.json"}), "pull", 1)
    assert r["Content-Type"] == "application/json"


@pytest.mark.django_db
def test_download_zip_starts_at_beginning(run_dirs):
    pull = PeatPull.objects.create(name="p", task_id="t1", run_dir="zr")
    (run_dirs / "pull" / "zr").mkdir()
    (run_dirs / "pull" / "zr" / "f").write_text("x")
    r = views.peat_run_download(_json_post({"pk": pull.pk, "pull_or_scan": "pull"}))
    assert r.content.startswith(b"PK")


@pytest.mark.django_db
@pytest.mark.parametrize("kind,view_cls,model,task", VIEWS)
def test_post_config_without_devices_is_default_run(post_env, kind, view_cls, model, task):
    cfg = _cfg()
    body = _body(view_cls().post(_json_post({"config": cfg.pk, "cidr_id": "1.1.1.1"})))
    assert body["status"] is True
    obj = model.objects.get()
    assert obj.config_id == (cfg.pk if kind == "scan" else None)  # scan keeps the raw config id
    assert obj.devices == ["1.1.1.1"]
    # device_ids without config also default
    model.objects.all().delete()
    body = _body(view_cls().post(_json_post({"device_ids": [1], "cidr_id": "2.2.2.2"})))
    assert model.objects.get().devices == ["2.2.2.2"]


@pytest.mark.django_db
def test_scan_with_unknown_config_reports_validation_error(post_env, monkeypatch):
    monkeypatch.setattr(views, "get_selected_config_targets",
                        lambda pk, ids: (["x"], ["1.1.1.1"], "ip"))
    body = _body(views.PeatScanView().post(_json_post({"config": 99999, "device_ids": [1]})))
    assert body["status"] is False and PeatScan.objects.count() == 0


def test_run_details_outcome_is_not_shared_between_calls(run_dirs):
    first = views._get_peat_run_details(_run_obj(status="waiting"), "pull")
    first["outcome"]["title"] = "mutated"
    second = views._get_peat_run_details(_run_obj(status="waiting"), "pull")
    assert second["outcome"]["title"] == "Run is waiting for target locks."
    first = views._get_peat_run_details(_run_obj(run_dir=None), "pull")
    first["outcome"]["message"] = "mutated"
    second = views._get_peat_run_details(_run_obj(run_dir=None), "pull")
    assert second["outcome"]["message"] != "mutated"
