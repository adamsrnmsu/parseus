"""Characterization tests for utility_analytics train/predict/merge flows."""

import json
import logging
from types import SimpleNamespace

import pytest

from parseus.analytics.capabilities import ElasticsearchSource, IndexQuery
from parseus.webapp.analytics import utility_analytics as ua
from parseus.webapp.analytics.models import (
    Analytic,
    AnalyticUserInput,
    TrainedModel,
    TrainedModelMetadata,
)

log = logging.getLogger("test")


# ---------------------------------------------------------------- merge inputs
def test_merge_later_wins_and_ignores_empty():
    assert ua._merge_analytic_inputs({"a": 1, "b": 1}, None, {}, {"b": 2}) == {"a": 1, "b": 2}
    assert ua._merge_analytic_inputs() == {}


def test_merge_index_dicts_are_merged_shallowly():
    out = ua._merge_analytic_inputs(
        {"fit_idx": {"index_name": "a", "filter": ["x"]}, "run_idx": {"index_name": "r"}},
        {"fit_idx": {"index_name": "b"}, "out_idx": {"index_name": "o"}},
    )
    assert out == {
        "fit_idx": {"index_name": "b", "filter": ["x"]},
        "run_idx": {"index_name": "r"},
        "out_idx": {"index_name": "o"},
    }


def test_merge_non_dict_index_value_replaces():
    assert ua._merge_analytic_inputs({"fit_idx": {"a": 1}}, {"fit_idx": "raw"}) == {"fit_idx": "raw"}


def test_merge_init_args_merge_and_do_not_alias():
    base = {"init_args": {"a": 1, "b": 1}}
    out = ua._merge_analytic_inputs(base, {"init_args": {"b": 2}})
    assert out["init_args"] == {"a": 1, "b": 2}
    assert base["init_args"] == {"a": 1, "b": 1}
    assert ua._merge_analytic_inputs({"init_args": {"a": 1}}, {"init_args": "str"}) == {
        "init_args": "str"
    }


# ------------------------------------------------------------ _fit_index_query
def test_fit_index_query_variants():
    q = IndexQuery(query={"match_all": {}})
    es = ElasticsearchSource(fit=q)
    assert ua._fit_index_query(SimpleNamespace(data_source=lambda: es)) is q
    assert ua._fit_index_query(SimpleNamespace(data_source=lambda: ElasticsearchSource())) == IndexQuery()
    assert ua._fit_index_query(SimpleNamespace(data_source=lambda: object())) == IndexQuery()
    assert ua._fit_index_query(SimpleNamespace()) == IndexQuery()


# ------------------------------------------------------------ train / predict
class _Ds:
    url = "http://es:9200"


class FakeAnalytic:
    """Minimal analytic double: learned state + run-to-run state."""

    class_name = "FakeAnalytic"
    fit_needed = True
    instances = []

    def __init__(self, log=None, **init_args):
        self.init_args = init_args
        self.learned = {"w": 7}
        self.run_state = {}
        self.imported = None
        self.loaded = None
        self.rule_state = None
        FakeAnalytic.instances.append(self)

    @classmethod
    def requires_fit(cls):
        return cls.fit_needed

    def state_dict(self):
        return self.learned

    def load_state_dict(self, state):
        self.loaded = state

    def apply_rule_state(self, init_args):
        self.rule_state = init_args

    def import_state(self, state):
        self.imported = state

    def export_state(self):
        return self.run_state

    def get_fit_results(self):
        import pandas as pd

        return pd.DataFrame({"col": [1]})

    def data_source(self):
        return ElasticsearchSource(
            fit=IndexQuery(query={"q": 1}, composite_agg={"c": 1}, return_fields=["f"])
        )


@pytest.fixture
def env(monkeypatch):
    FakeAnalytic.fit_needed = True
    FakeAnalytic.instances = []
    cfg = SimpleNamespace(suppression="5m", fit_idx="fit-idx")
    calls = {}
    monkeypatch.setattr(ua, "load_class", lambda path: FakeAnalytic)
    monkeypatch.setattr(ua, "_analytic_config_from_ui", lambda obj, user_input=None: cfg)
    monkeypatch.setattr(ua, "_get_sysmon_rules", lambda name: [])

    def fake_fit(ds_client, analytic, analytic_config):
        calls["fit"] = (ds_client, analytic_config)
        return analytic

    def fake_run(ds_client, analytic, analytic_config):
        calls["run"] = (ds_client, analytic_config)
        return ["alert"]

    monkeypatch.setattr(ua, "fit", fake_fit)
    monkeypatch.setattr(ua, "run", fake_run)
    return SimpleNamespace(calls=calls, cfg=cfg, monkeypatch=monkeypatch)


@pytest.fixture
def analytic_row(db):
    ui = AnalyticUserInput.objects.create(
        init_args={"k": "v"}, fit_idx_name="fi", run_idx_name="ri", out_idx_name="oi"
    )
    return Analytic.objects.create(analytic_class="mod.FakeAnalytic", ui=ui)


def _user_input(**kw):
    return {"user_save": "", "ds_host_url": _Ds(), "fit_start": "s", "fit_end": "e", "ip_filter": None, **kw}


def test_train_saves_model_and_metadata(env, analytic_row):
    ua.train(analytic_row, _user_input(user_save="mine"), log)
    model = TrainedModel.objects.get()
    assert model.name == "FakeAnalytic_mod.FakeAnalytic"
    assert model.instance_name == "FakeAnalytic_mod.FakeAnalytic_mine"
    assert model.analytic == analytic_row and model.analytic_class == "mod.FakeAnalytic"
    assert "<table" in model.drawing_data and "table-striped" in model.drawing_data
    assert model.manifest and bytes(model.artifact)
    meta = TrainedModelMetadata.objects.get(model=model)
    assert meta.es_host == "http://es:9200" and meta.idx == "fit-idx"
    assert (meta.start_date, meta.end_date) == ("s", "e")
    assert meta.init_args == {"k": "v"}
    assert meta.fit_idx == {"fit_idx": "fi"} and meta.run_idx == {"run_idx": "ri"}
    assert meta.return_fields == "['f']" and meta.supervised is True
    assert env.calls["fit"][1] is env.cfg
    assert FakeAnalytic.instances[0].init_args == {"k": "v"}


def test_train_without_user_save_uses_model_name(env, analytic_row):
    ua.train(analytic_row, _user_input(), log)
    model = TrainedModel.objects.get()
    assert model.instance_name == model.name


def test_train_skips_analytics_that_do_not_need_fit(env, analytic_row):
    FakeAnalytic.fit_needed = False
    ua.train(analytic_row, _user_input(), log)
    assert "fit" not in env.calls and not TrainedModel.objects.exists()


def test_train_saves_nothing_when_fit_returns_nothing(env, analytic_row):
    env.monkeypatch.setattr(ua, "fit", lambda **kw: None)
    ua.train(analytic_row, _user_input(), log)
    assert not TrainedModel.objects.exists()


def test_train_without_fit_results_stores_empty_table(env, analytic_row):
    env.monkeypatch.delattr(FakeAnalytic, "get_fit_results")
    ua.train(analytic_row, _user_input(), log)
    assert TrainedModel.objects.get().drawing_data.startswith("<table")


@pytest.fixture
def trained(env, analytic_row):
    ua.train(analytic_row, _user_input(), log)
    return TrainedModel.objects.get()


def test_predict_loads_state_runs_and_persists_changed_run_state(env, trained):
    env.monkeypatch.setattr(
        FakeAnalytic, "export_state", lambda self: {"seen": [1]}
    )
    trained.run_state = {"old": 1}
    out = ua.predict(trained, _user_input(), log)
    assert out == {"outidx": "oi", "alerts": ["alert"]}
    inst = FakeAnalytic.instances[-1]
    assert inst.loaded == {"w": 7}
    assert inst.imported == {"old": 1}
    assert inst.rule_state == {"k": "v", "suppression": "5m"}
    assert TrainedModel.objects.get(pk=trained.pk).run_state == {"seen": [1]}


def test_predict_does_not_write_unchanged_run_state(env, trained):
    saves = []
    env.monkeypatch.setattr(
        TrainedModel, "save", lambda self, **kw: saves.append(kw)
    )
    ua.predict(trained, _user_input(), log)  # export_state {} == run_state {}
    assert saves == []


def test_predict_requires_metadata(env, trained):
    TrainedModelMetadata.objects.all().delete()
    with pytest.raises(ValueError, match="Issue finding relevant Trained Model Info"):
        ua.predict(trained, _user_input(), log)


def test_predict_keeps_explicit_suppression_arg(env, trained):
    TrainedModelMetadata.objects.update(init_args={"suppression": "1h"})
    ua.predict(trained, _user_input(), log)
    assert FakeAnalytic.instances[-1].init_args == {"suppression": "1h"}


def test_unsupervised_predict_persists_changed_state(env, analytic_row):
    analytic_row.ui.run_state = {"a": 1}
    env.monkeypatch.setattr(FakeAnalytic, "export_state", lambda self: {"a": 2})
    out = ua.unsupervised_predict(analytic_row, _user_input(), log)
    assert out == ["alert"]
    inst = FakeAnalytic.instances[-1]
    assert inst.imported == {"a": 1}
    assert inst.init_args == {"k": "v", "suppression": "5m"}
    assert AnalyticUserInput.objects.get(pk=analytic_row.ui.pk).run_state == {"a": 2}


def test_unsupervised_predict_unchanged_state_not_saved(env, analytic_row):
    saves = []
    env.monkeypatch.setattr(
        AnalyticUserInput, "save", lambda self, **kw: saves.append(kw)
    )
    ua.unsupervised_predict(analytic_row, _user_input(), log)
    assert saves == []


# ------------------------------------------------------------------ push / cmd
def test_push_results_to_es_returns_error_on_bad_json():
    err = ua.push_results_to_es("{nope", object(), "o", log)
    assert isinstance(err, json.JSONDecodeError)


def test_push_results_to_es_pushes_documents(monkeypatch):
    seen = {}
    monkeypatch.setattr(ua, "push", lambda **kw: seen.update(kw))
    assert ua.push_results_to_es('[{"a": 1}]', "ds", "idx", log) is None
    assert seen == {"ds_client": "ds", "index": "idx", "document_list": [{"a": 1}]}


def test_populate_command_reports_each_stage(monkeypatch):
    from io import StringIO

    from parseus.webapp.analytics.management.commands import populate_analytics as cmd

    steps = []
    monkeypatch.setattr(cmd.management, "call_command", lambda name: steps.append(name))
    monkeypatch.setattr(cmd, "upload_analytic_config", lambda: steps.append("upload"))
    out = StringIO()
    cmd.Command(stdout=out).handle()
    assert steps == ["migrate", "upload"]
    assert "Finished migrate" in out.getvalue() and "Finished populating analytics" in out.getvalue()


def test_populate_command_stops_on_migrate_error(monkeypatch):
    from io import StringIO

    from parseus.webapp.analytics.management.commands import populate_analytics as cmd

    def boom(name):
        raise RuntimeError("db locked")

    uploaded = []
    monkeypatch.setattr(cmd.management, "call_command", boom)
    monkeypatch.setattr(cmd, "upload_analytic_config", lambda: uploaded.append(1))
    out = StringIO()
    cmd.Command(stdout=out).handle()
    assert "migrate error: db locked" in out.getvalue() and uploaded == []


def test_populate_command_reports_upload_error(monkeypatch):
    from io import StringIO

    from parseus.webapp.analytics.management.commands import populate_analytics as cmd

    def boom():
        raise RuntimeError("bad registry")

    monkeypatch.setattr(cmd.management, "call_command", lambda name: None)
    monkeypatch.setattr(cmd, "upload_analytic_config", boom)
    out = StringIO()
    cmd.Command(stdout=out).handle()
    assert "populate analytics error: bad registry" in out.getvalue()


# ------------------------------------------------------ with_operator_rules
class _Ruled:
    @staticmethod
    def merge_rules(init_args, rules):
        return {**(init_args or {}), "rules": rules}


def test_with_operator_rules_merges_through_analytic(monkeypatch):
    seen = []
    monkeypatch.setattr(ua, "_get_sysmon_rules", lambda name: seen.append(name) or ["r"])
    assert ua.with_operator_rules(_Ruled, {"a": 1}) == {"a": 1, "rules": ["r"]}
    assert seen == ["_Ruled"]


def test_with_operator_rules_passthrough_copy(monkeypatch):
    base = {"a": 1}
    monkeypatch.setattr(ua, "_get_sysmon_rules", lambda name: [])
    out = ua.with_operator_rules(_Ruled, base)
    assert out == base and out is not base
    monkeypatch.setattr(ua, "_get_sysmon_rules", lambda name: ["r"])
    assert ua.with_operator_rules(object, None) == {}


def test_get_sysmon_rules_for_installed_alerts_app(db):
    from parseus.webapp.alerts.models import SysmonRule

    SysmonRule.objects.create(name="n", analytic_type="user", match_value="bob")
    SysmonRule.objects.create(name="off", analytic_type="user", match_value="x", enabled=False)
    rules = ua._get_sysmon_rules("parseus.analytics.SysmonUnapprovedUserAnalytic")
    assert [r["values"] for r in rules] == [["bob"]]
    assert ua._get_sysmon_rules("Other") == []
