"""Tests for scheduled analytics task helpers."""

from types import SimpleNamespace

import pytest

from parseus.interfaces import tasks


class FakeDataStore:
    """Datastore test double that records indexed documents and templates."""

    def __init__(self):
        self.indexed_documents = []
        self.templates = []
        self.events = []

    def ensure_index_template(self, name, index_patterns, mappings):
        self.templates.append((name, index_patterns, mappings))
        self.events.append("template")
        return True

    def index(self, index, document):
        self.indexed_documents.append((index, document))
        self.events.append("index")


class DataStoreFactory:
    """Datastore factory test double that records creation count."""

    def __init__(self):
        self.calls = 0
        self.datastore = FakeDataStore()

    def __call__(self):
        self.calls += 1
        return self.datastore


def test_output_index_name_prefers_analytic_configuration_out_idx():
    analytic_config = SimpleNamespace(out_idx="alerts-config", out_idx_name="alerts-ui")

    assert tasks._output_index_name(analytic_config) == "alerts-config"


def test_output_index_name_supports_ui_out_idx_name():
    analytic_config = SimpleNamespace(out_idx_name="alerts-ui")

    assert tasks._output_index_name(analytic_config) == "alerts-ui"


def test_output_index_name_falls_back_to_alerts():
    analytic_config = SimpleNamespace()

    assert tasks._output_index_name(analytic_config) == "alerts"


def test_push_alerts_uses_configured_out_idx(monkeypatch):
    datastore_factory = DataStoreFactory()
    monkeypatch.setattr(tasks, "get_ds_client", datastore_factory)

    alert = {"event": {"kind": "alert"}}
    pushed_count = tasks.push_alerts(
        SimpleNamespace(out_idx="configured-alerts"),
        [alert],
    )

    assert pushed_count == 1
    assert datastore_factory.calls == 1
    assert len(datastore_factory.datastore.indexed_documents) == 1
    index_name, document = datastore_factory.datastore.indexed_documents[0]
    assert index_name.startswith("configured-alerts-")
    assert document is alert


def test_push_alerts_reuses_datastore_client(monkeypatch):
    datastore_factory = DataStoreFactory()
    monkeypatch.setattr(tasks, "get_ds_client", datastore_factory)

    alerts = [
        {"event": {"kind": "alert"}},
        {"event": {"kind": "alert"}},
    ]
    pushed_count = tasks.push_alerts(SimpleNamespace(out_idx="alerts"), alerts)

    assert pushed_count == 2
    assert datastore_factory.calls == 1
    assert len(datastore_factory.datastore.indexed_documents) == 2


def test_push_alerts_installs_alerts_template_before_first_write(monkeypatch):
    datastore_factory = DataStoreFactory()
    monkeypatch.setattr(tasks, "get_ds_client", datastore_factory)

    tasks.push_alerts(
        SimpleNamespace(out_idx="alerts"),
        [{"event": {"kind": "alert"}}, {"event": {"kind": "alert"}}],
    )

    datastore = datastore_factory.datastore
    assert datastore.templates == [
        (
            tasks.ALERTS_INDEX_TEMPLATE_NAME,
            tasks.ALERTS_INDEX_PATTERNS,
            tasks.ALERTS_INDEX_MAPPINGS,
        )
    ]
    assert datastore.events == ["template", "index", "index"]


def test_push_alerts_returns_zero_when_no_alerts(monkeypatch):
    def fail_get_ds_client():
        raise AssertionError("datastore client should not be created")

    monkeypatch.setattr(tasks, "get_ds_client", fail_get_ds_client)

    assert tasks.push_alerts(SimpleNamespace(out_idx="alerts"), []) == 0
    assert tasks.push_alerts(SimpleNamespace(out_idx="alerts"), None) == 0


def test_push_alerts_rejects_non_dictionary_alerts(monkeypatch):
    datastore_factory = DataStoreFactory()
    monkeypatch.setattr(tasks, "get_ds_client", datastore_factory)

    with pytest.raises(TypeError, match="Alert #0 must be a dictionary"):
        tasks.push_alerts(SimpleNamespace(out_idx="alerts"), ["not-an-alert"])

    assert datastore_factory.calls == 0


def test_fetch_operator_rules_returns_empty_without_alerts_app(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "parseus.webapp.alerts.models":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert tasks._fetch_operator_rules("pkg.MyAnalytic") == []


def test_fetch_operator_rules_queries_by_class_name(monkeypatch):
    import sys
    import types

    calls = []
    module = types.ModuleType("parseus.webapp.alerts.models")
    module.get_sysmon_rules_for_analytic_class = lambda name: calls.append(name) or None
    monkeypatch.setitem(sys.modules, "parseus.webapp.alerts.models", module)
    assert tasks._fetch_operator_rules("pkg.mod.MyAnalytic") == []
    assert calls == ["MyAnalytic"]
    module.get_sysmon_rules_for_analytic_class = lambda name: [name, "rule"]
    assert tasks._fetch_operator_rules(None) == ["", "rule"]
