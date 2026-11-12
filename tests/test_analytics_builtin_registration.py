"""Behavior tests for built-in analytic registration and fit_or_load."""

import logging

from parseus.analytics import builtin_analytics
from parseus.analytics.base import Analytic


def test_register_imports_every_module_and_survives_failures(monkeypatch, caplog):
    imported = []

    def fake_import(name):
        imported.append(name)
        if name == builtin_analytics._ANALYTIC_MODULES[1]:
            raise ImportError("boom")

    monkeypatch.setattr(builtin_analytics.importlib, "import_module", fake_import)
    with caplog.at_level(logging.ERROR, logger="parseus.analytics"):
        builtin_analytics.register()
    assert imported == list(builtin_analytics._ANALYTIC_MODULES)
    assert "Failed to import built-in analytic module %s" % imported[1] in caplog.text


class Recording(Analytic):
    needs_fit = True

    def __init__(self):
        super().__init__(pull_duration=1)
        self.calls = []

    @classmethod
    def requires_fit(cls):
        return cls.needs_fit

    def fit(self, *sets):
        self.calls.append(("fit", sets))

    def save(self, file_path):
        self.calls.append(("save", file_path))

    def load(self, file_path):
        self.calls.append(("load", file_path))

    def _run(self, start, end, transformed_documents, state, **other_documents):
        return []


def test_fit_or_load_loads_existing_file(tmp_path):
    path = tmp_path / "m.joblib"
    path.write_text("x")
    analytic = Recording()
    analytic.fit_or_load(str(path), [1])
    assert analytic.calls == [("load", str(path))]


def test_fit_or_load_fits_resolving_callbacks_then_saves(tmp_path):
    path = str(tmp_path / "missing.joblib")
    analytic = Recording()
    analytic.fit_or_load(path, [1], lambda: [2, 3])
    assert analytic.calls == [("fit", ([1], [2, 3])), ("save", path)]


def test_fit_or_load_skips_fit_when_not_required(tmp_path):
    path = str(tmp_path / "missing.joblib")

    class NoFit(Recording):
        needs_fit = False

    analytic = NoFit()
    analytic.fit_or_load(path, lambda: 1 / 0)
    assert analytic.calls == [("save", path)]
