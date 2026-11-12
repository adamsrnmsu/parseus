"""Characterization tests for analytics views (fakes only: no ES, no registry)."""

import json
from types import SimpleNamespace

import pytest
from django.http import Http404
from django.test import RequestFactory

from parseus.webapp.analytics import views

rf = RequestFactory()


def _a(name, family, requires_fit):
    return SimpleNamespace(name=name, family=family, requires_fit=requires_fit)


ACTIVE = [
    _a("net_sup_b", "net", True),
    _a("net_sup_a", "zeek", True),
    _a("net_uns", "net", False),
    _a("peat_sup", "ot_internals", True),
    _a("peat_uns", "ot_internals", False),
]


class _Tpl:
    def __init__(self, name, sink):
        self.name, self.sink = name, sink

    def render(self, context=None, request=None):
        self.sink.append((self.name, context))
        from django.http import HttpResponse

        return self.name


@pytest.fixture
def listing(monkeypatch):
    rendered = []
    monkeypatch.setattr(
        views.Analytic, "objects", SimpleNamespace(filter=lambda **kw: list(ACTIVE))
    )
    monkeypatch.setattr(views.loader, "get_template", lambda n: _Tpl(n, rendered))
    monkeypatch.setattr(views, "get_data_source_provider", lambda t: object())
    return rendered


@pytest.mark.parametrize(
    "kind,template,names",
    [
        ("NetworkSupervised", "supervised", ["net_sup_a", "net_sup_b"]),
        ("NetworkUnsupervised", "unsupervised", ["net_uns"]),
        ("PeatSupervised", "supervised", ["peat_sup"]),
        ("PeatUnsupervised", "unsupervised", ["peat_uns"]),
    ],
)
def test_analytics_filters_by_type(listing, kind, template, names):
    resp = views.analytics(rf.get("/"), kind)
    assert resp.status_code == 200
    name, ctx = listing[0]
    assert name == f"analytics/{template}_analytics.html"
    assert [a.name for a in ctx["analytics"]] == names
    assert ctx["analytic_type"] == kind and ctx["peat_available"] is True
    assert ctx["families"] == [
        {"family": f} for f in sorted({a.family for a in ctx["analytics"]})
    ]


def test_analytics_comma_list_uses_first_matching_kind(listing):
    views.analytics(rf.get("/"), "PeatUnsupervised,NetworkSupervised")
    name, ctx = listing[0]
    assert name == "analytics/supervised_analytics.html"
    assert [a.name for a in ctx["analytics"]] == ["net_sup_a", "net_sup_b"]


@pytest.mark.parametrize("kind", ["All", "bogus", "", "NetworkSupervisedX"])
def test_analytics_unknown_type_is_404(listing, kind):
    # Only the four kinds linked from analytic_base.html are reachable; 'All'
    # is not linked anywhere and used to crash with UnboundLocalError.
    with pytest.raises(Http404):
        views.analytics(rf.get("/"), kind)
    assert listing == []


def test_analytics_reports_peat_unavailable(listing, monkeypatch):
    monkeypatch.setattr(views, "get_data_source_provider", lambda t: None)
    views.analytics(rf.get("/"), "NetworkSupervised")
    assert listing[0][1]["peat_available"] is False


# ----------------------------------------------------------------- push_results
class _DS:
    url = "http://es:9200"

    def __init__(self, up=True):
        self.up = up

    def ping(self):
        return self.up


def _push(**data):
    return views.push_results(rf.post("/push", data))


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"es_data": "[]"}, "index was empty"),
        ({"outidx": "o"}, "data was empty"),
        ({"outidx": "o", "es_data": '"No results"'}, "data was empty"),
    ],
)
def test_push_results_rejects_empty_input(data, msg):
    resp = _push(**data)
    assert resp.status_code == 400 and resp.content.decode() == msg


def test_push_results_unreachable_host(monkeypatch):
    monkeypatch.setattr(views, "_get_database_client", lambda r: _DS(up=False))
    resp = _push(outidx="o", es_data="[1]")
    assert resp.status_code == 400
    assert (
        resp.content.decode()
        == "Elasticsearch host 'http://es:9200' could not be reached"
    )


def test_push_results_passes_data_through(monkeypatch):
    ds = _DS()
    calls = []
    monkeypatch.setattr(views, "_get_database_client", lambda r: ds)
    monkeypatch.setattr(views, "push_results_to_es", lambda *a: calls.append(a) or None)
    resp = _push(outidx="o", es_data="[1]")
    assert resp.status_code == 200
    assert calls[0][:3] == ("[1]", ds, "o")


def test_push_results_reports_push_error(monkeypatch):
    monkeypatch.setattr(views, "_get_database_client", lambda r: _DS())
    monkeypatch.setattr(views, "push_results_to_es", lambda *a: ValueError("boom"))
    resp = _push(outidx="o", es_data="[1]")
    assert resp.status_code == 400 and "boom" in resp.content.decode()


def test_push_results_requires_post():
    assert views.push_results(rf.get("/push")).status_code == 405


# -------------------------------------------------------------- model actions
class _Qs:
    """Stand-in for Analytic.objects / TrainedModel.objects."""

    def __init__(self, items):
        self.items = items

    def get(self, pk, **kw):
        if str(pk) not in self.items:
            raise LookupError(pk)
        return self.items[str(pk)]


@pytest.fixture
def inputs(monkeypatch):
    monkeypatch.setattr(
        views, "_get_analytic_input_from_request", lambda r, k: {"kind": k}
    )


def test_train_model_reports_each_outcome(monkeypatch, inputs):
    objs = {"1": SimpleNamespace(name="ok"), "2": SimpleNamespace(name="bad")}
    monkeypatch.setattr(views.Analytic, "objects", _Qs(objs))

    def fake_train(obj, user_input, log):
        assert user_input == {"kind": "fit"}
        if obj.name == "bad":
            raise RuntimeError("no data")

    monkeypatch.setattr(views, "train", fake_train)
    resp = views.train_model(rf.post("/t", {"analytic_pks[]": ["1", "2"]}))
    assert json.loads(resp.content) == [
        {"status": "success", "model": "ok"},
        {"status": "failed", "model": "bad", "error": "no data"},
    ]


def test_train_model_unavailable_source_is_400(monkeypatch):
    def deny(r, k):
        raise views.DataSourceUnavailableError("no peat")

    monkeypatch.setattr(views, "_get_analytic_input_from_request", deny)
    resp = views.train_model(rf.post("/t"))
    assert resp.status_code == 400 and resp.content.decode() == "no peat"


@pytest.mark.django_db
def test_predict_unsupervised_renders_results(monkeypatch, inputs):
    monkeypatch.setattr(
        views.Analytic, "objects", _Qs({"7": SimpleNamespace(name="n")})
    )
    monkeypatch.setattr(views, "unsupervised_predict", lambda o, u, log: ["alert"])
    seen = {}
    monkeypatch.setattr(
        views, "render", lambda req, tpl, ctx: seen.update(tpl=tpl, ctx=ctx) or "R"
    )
    assert views.predict_unsupervised(rf.post("/p", {"analytic_pks[]": ["7"]})) == "R"
    assert seen == {
        "tpl": "analytics/analytic_results.html",
        "ctx": {"analytics": {"n": ["alert"]}},
    }


@pytest.mark.django_db
def test_predict_unsupervised_failure_is_400(monkeypatch, inputs):
    monkeypatch.setattr(
        views.Analytic, "objects", _Qs({"7": SimpleNamespace(name="n")})
    )

    def boom(*a):
        raise RuntimeError

    monkeypatch.setattr(views, "unsupervised_predict", boom)
    resp = views.predict_unsupervised(rf.post("/p", {"analytic_pks[]": ["7"]}))
    assert resp.status_code == 400
    assert resp.content.decode() == "Prediction did not work for analytic n"


def test_predict_unsupervised_unavailable_source_is_400(monkeypatch):
    def deny(r, k):
        raise views.DataSourceUnavailableError("no peat")

    monkeypatch.setattr(views, "_get_analytic_input_from_request", deny)
    assert views.predict_unsupervised(rf.post("/p")).status_code == 400


def test_predict_network_unsupervised_requires_database(monkeypatch):
    monkeypatch.setattr(
        views,
        "get_database_status",
        lambda r: SimpleNamespace(
            content=json.dumps({"status": False, "message": "down"})
        ),
    )
    resp = views.predict_network_unsupervised(rf.post("/p"))
    assert resp.status_code == 400 and resp.content.decode() == "down"


def test_predict_network_unsupervised_delegates_when_up(monkeypatch):
    monkeypatch.setattr(
        views,
        "get_database_status",
        lambda r: SimpleNamespace(content=json.dumps({"status": True})),
    )
    monkeypatch.setattr(views, "predict_unsupervised", lambda r: "delegated")
    assert views.predict_network_unsupervised(rf.post("/p")) == "delegated"
    monkeypatch.setattr(views, "predict_unsupervised", lambda r: "peat")
    assert views.predict_peat_unsupervised(rf.post("/p")) == "peat"


def test_predict_model_returns_alerts_or_placeholder(monkeypatch, inputs):
    monkeypatch.setattr(
        views.TrainedModel, "objects", _Qs({"3": SimpleNamespace(pk=3)})
    )
    results = iter([{"outidx": "o", "alerts": []}, {"outidx": "o", "alerts": [1]}])
    monkeypatch.setattr(views, "predict", lambda m, u, log: next(results))
    post = lambda: views.predict_model(rf.post("/pm", {"model_pk": "3"}))
    assert json.loads(post().content) == {"outidx": "o", "alerts": "No results"}
    assert json.loads(post().content) == {"outidx": "o", "alerts": [1]}


def test_predict_model_failure_is_400(monkeypatch, inputs):
    monkeypatch.setattr(
        views.TrainedModel, "objects", _Qs({"3": SimpleNamespace(pk=3)})
    )

    def boom(*a):
        raise ValueError("bad model")

    monkeypatch.setattr(views, "predict", boom)
    resp = views.predict_model(rf.post("/pm", {"model_pk": "3"}))
    assert resp.status_code == 400 and "bad model" in resp.content.decode()


def test_predict_model_unavailable_source_is_400(monkeypatch):
    def deny(r, k):
        raise views.DataSourceUnavailableError("no peat")

    monkeypatch.setattr(views, "_get_analytic_input_from_request", deny)
    assert views.predict_model(rf.post("/pm")).status_code == 400


# --------------------------------------------------- update_analytic_user_input
class _Ui:
    fit_idx_name, run_idx_name, out_idx_name, init_args = "f", "r", "o", "{}"
    saved = False

    def save(self):
        self.saved = True


def test_update_user_input_overrides_only_posted_fields(monkeypatch):
    ui = _Ui()
    monkeypatch.setattr(views.Analytic, "objects", _Qs({"4": SimpleNamespace(ui=ui)}))
    resp = views.update_analytic_user_input(
        rf.post("/u", {"run_idx": "new", "init_args": "{1}"}), 4
    )
    assert resp.status_code == 200 and ui.saved
    assert (ui.fit_idx_name, ui.run_idx_name, ui.out_idx_name, ui.init_args) == (
        "f",
        "new",
        "o",
        "{1}",
    )


def test_update_user_input_unknown_analytic_is_404(monkeypatch):
    monkeypatch.setattr(views.Analytic, "objects", _Qs({}))
    assert views.update_analytic_user_input(rf.post("/u"), 9).status_code == 404


def test_update_user_input_save_failure_is_500(monkeypatch):
    class Bad(_Ui):
        def save(self):
            raise RuntimeError("db")

    monkeypatch.setattr(
        views.Analytic, "objects", _Qs({"4": SimpleNamespace(ui=Bad())})
    )
    assert views.update_analytic_user_input(rf.post("/u"), 4).status_code == 500


def test_update_user_input_requires_post():
    assert views.update_analytic_user_input(rf.get("/u"), 4).status_code == 405


def test_views_only_offer_active_analytics(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        views.Analytic,
        "objects",
        SimpleNamespace(filter=lambda **kw: seen.update(kw) or []),
    )
    monkeypatch.setattr(views.loader, "get_template", lambda n: _Tpl(n, []))
    monkeypatch.setattr(views, "get_data_source_provider", lambda t: None)
    views.analytics(rf.get("/"), "NetworkSupervised")
    assert seen == {"is_active": True}


def test_peat_datasets_lists_ids_or_empty(monkeypatch):
    monkeypatch.setattr(views, "get_data_source_provider", lambda t: None)
    assert json.loads(views.peat_datasets(rf.get("/")).content) == []
    provider = SimpleNamespace(get_datasets=lambda: [{"id": "a"}, {"id": "b"}])
    monkeypatch.setattr(views, "get_data_source_provider", lambda t: provider)
    assert json.loads(views.peat_datasets(rf.get("/")).content) == ["a", "b"]


def test_analytics_supervised_renders_real_template(monkeypatch):
    """The listing template exists and renders the view's context."""
    monkeypatch.setattr(
        views.Analytic, "objects", SimpleNamespace(filter=lambda **kw: list(ACTIVE))
    )
    monkeypatch.setattr(views, "get_data_source_provider", lambda t: object())
    request = rf.get("/")
    request.session = {}  # context processors read request.session
    resp = views.analytics(request, "NetworkSupervised")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "net_sup_a" in html and "peat_uns" not in html
