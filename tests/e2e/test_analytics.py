"""Smoke tests for the Analytics pages: index, the four analytic type pages, the
trained-models list and the analytic description / settings / metadata modals.

Training and prediction are mocked at the view boundary (no real models are fit).
"""

import re
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from playwright.sync_api import expect

from parseus.webapp.analytics.models import Analytic, TrainedModel, TrainedModelMetadata
from parseus.webapp.analytics.utility_analytics import upload_analytic_config
from tests.globals import DATASTORE

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

ERROR_TOAST = ".toast-header.bg-danger"
SUCCESS_TOAST = ".toast:has(.toast-header.bg-success)"
VIEWS = "parseus.webapp.analytics.views"
NEW_COMM = "NewCommunicationsAnalytic"


@pytest.fixture
def analytics_db():
    """The Analytic rows ``populate_analytics`` seeds in dev."""
    upload_analytic_config()


@pytest.fixture
def trained_model(analytics_db):
    analytic = Analytic.objects.get(analytic_class__endswith=NEW_COMM)
    model = TrainedModel.objects.create(
        name=analytic.name,
        family=analytic.family,
        instance_name="e2e_trained_model",
        artifact=b"",
        analytic=analytic,
        analytic_class=analytic.analytic_class,
        drawing_data="plain drawing text",
    )
    TrainedModelMetadata.objects.create(
        model=model, instance_name=model.instance_name, idx="e2e-packets-*"
    )
    return model


@pytest.fixture
def peat_provider():
    provider = SimpleNamespace(
        get_datasets=lambda: [{"id": "run_a"}, {"id": "run_b"}],
        resolve=lambda name: f"/peat/{name}",
    )
    with mock.patch(f"{VIEWS}.get_data_source_provider", return_value=provider):
        yield provider


def check_clean(page, problems):
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def open_family(page, family):
    page.click(f"button[data-bs-target='#collapse_{family}']")
    expect(page.locator(f"#collapse_{family}")).to_be_visible()


def test_index_links(live_server, page, problems):
    page.goto(f"{live_server.url}/analytics/")
    expect(page.get_by_text("Welcome", exact=True)).to_be_visible()
    for text in (
        "Peat Supervised Analytics",
        "Peat Unsupervised Analytics",
        "Network Supervised Analytics",
        "Network Unsupervised Analytics",
    ):
        expect(page.get_by_role("link", name=text, exact=True)).to_be_visible()
    check_clean(page, problems)

    page.get_by_role("link", name="Network Supervised Analytics").click()
    expect(page).to_have_url(f"{live_server.url}/analytics/analytics/NetworkSupervised")
    expect(page.get_by_text("Supervised Analytic Models")).to_be_visible()


def test_network_supervised_train(live_server, page, problems, analytics_db):
    trained = []

    def fake_train(obj, user_input, logger):
        trained.append((obj.name, user_input))

    page.goto(f"{live_server.url}/analytics/analytics/NetworkSupervised")
    expect(page.locator("#analytics_list")).to_be_visible()
    expect(page.locator("#train_dates")).to_be_visible()
    expect(page.locator("#trained_models_box")).to_be_visible()
    expect(page.locator("#data_box")).to_be_visible()
    # Supervised, non-PEAT analytics only: three families, 6 analytics.
    for family in ("network_anomalies", "ot_network", "scpi_analytics"):
        expect(page.locator(f"#heading_{family}")).to_be_visible()
    expect(page.locator("#heading_ot_internals")).to_have_count(0)
    expect(page.locator("#heading_host_anomalies")).to_have_count(0)
    check_clean(page, problems)

    open_family(page, "network_anomalies")
    expect(page.locator("#train_btn")).to_be_disabled()
    page.locator(f"#{NEW_COMM}-{NEW_COMM}-check").check()
    expect(page.locator("#train_btn")).to_be_enabled()

    with (
        mock.patch(f"{VIEWS}.train", side_effect=fake_train),
        page.expect_response("**/analytics/train_model/") as resp,
    ):
        page.click("#train_btn")
    assert resp.value.ok
    expect(page.locator(SUCCESS_TOAST)).to_contain_text(f"Successfully trained {NEW_COMM}")
    assert [name for name, _ in trained] == [NEW_COMM]
    expect(page.locator(ERROR_TOAST)).to_have_count(0)


def test_network_unsupervised_predict_and_push(live_server, page, problems, analytics_db):
    out_index = f"e2e-analytics-out-{uuid.uuid4().hex[:8]}"
    alerts = [{"@timestamp": "2024-01-01T00:00:00Z", "message": "unapproved device 10.9.9.9"}]
    page.goto(f"{live_server.url}/analytics/analytics/NetworkUnsupervised")
    expect(page.get_by_text("NetworkUnsupervised Analytic Models")).to_be_visible()
    expect(page.locator("#data_box")).to_be_visible()
    expect(page.locator("#heading_host_anomalies")).to_be_visible()
    expect(page.locator("#heading_network_anomalies")).to_have_count(0)
    check_clean(page, problems)

    open_family(page, "host_anomalies")
    expect(page.locator("#train_btn")).to_be_disabled()
    page.locator("#SysmonUnapprovedIpAnalytic-SysmonUnapprovedIpAnalytic-check").check()
    expect(page.locator("#train_btn")).to_be_enabled()

    with (
        mock.patch(f"{VIEWS}.unsupervised_predict", return_value=alerts) as predict,
        page.expect_response("**/predict_network_unsupervised/") as resp,
    ):
        page.click("#train_btn")
    assert resp.value.ok, resp.value.text()
    predict.assert_called_once()
    expect(page.locator(SUCCESS_TOAST)).to_contain_text("Successfully predicted")
    page.click("button[data-bs-target='#collapse_SysmonUnapprovedIpAnalytic']")
    expect(page.locator(".unsupervised-alert")).to_contain_text("unapproved device")

    # Push the results to a throwaway index.
    page.fill("#outgoing_idx", out_index)
    try:
        with page.expect_response("**/push_results/") as push:
            page.click("#push_unsupervised_results_btn")
        assert push.value.ok, push.value.text()
        expect(page.locator(SUCCESS_TOAST).last).to_contain_text("pushed results")
    finally:
        DATASTORE.delete_index(out_index)
    expect(page.locator(ERROR_TOAST)).to_have_count(0)


def test_peat_supervised(live_server, page, problems, analytics_db, peat_provider):
    trained = []
    page.goto(f"{live_server.url}/analytics/analytics/PeatSupervised")
    expect(page.get_by_text("Supervised Analytic Models")).to_be_visible()
    expect(page.locator("#heading_ot_internals")).to_be_visible()
    expect(page.locator("#heading_network_anomalies")).to_have_count(0)
    expect(page.locator("#trainSelect option")).to_have_count(3)  # prompt + 2 folders
    check_clean(page, problems)

    open_family(page, "ot_internals")
    page.locator("#PeatDeviceChangeAnalytic-PeatDeviceChangeAnalytic-check").check()
    expect(page.locator("#train_btn")).to_be_disabled()  # no folder chosen yet
    page.select_option("#trainSelect", "run_a")
    expect(page.locator("#train_btn")).to_be_enabled()

    with (
        mock.patch(f"{VIEWS}.train", side_effect=lambda o, ui, lg: trained.append(ui)),
        page.expect_response("**/analytics/train_model/") as resp,
    ):
        page.click("#train_btn")
    assert resp.value.ok
    assert trained[0]["fit_idx"] == {"index_name": "/peat/run_a"}
    expect(page.locator(SUCCESS_TOAST)).to_contain_text("Successfully trained")


def test_peat_unsupervised(live_server, page, problems, analytics_db, peat_provider):
    page.goto(f"{live_server.url}/analytics/analytics/PeatUnsupervised")
    expect(page.get_by_text("PeatUnsupervised Analytic Models")).to_be_visible()
    expect(page.locator("#trainSelect option")).to_have_count(3)
    expect(page.locator("#data_box")).to_be_visible()
    check_clean(page, problems)

    open_family(page, "ot_internals")
    page.locator("#PeatAuthEnumerationAnalytic-PeatAuthEnumerationAnalytic-check").check()
    page.select_option("#trainSelect", "run_b")
    expect(page.locator("#train_btn")).to_be_enabled()

    with (
        mock.patch(f"{VIEWS}.unsupervised_predict", return_value=["auth burst"]),
        page.expect_response("**/predict_peat_unsupervised/") as resp,
    ):
        page.click("#train_btn")
    assert resp.value.ok, resp.value.text()
    expect(page.locator(SUCCESS_TOAST)).to_contain_text("Successfully predicted")
    page.click("button[data-bs-target='#collapse_PeatAuthEnumerationAnalytic']")
    expect(page.locator(".unsupervised-alert")).to_contain_text("auth burst")


def test_analytic_description_modal(live_server, page, problems, analytics_db):
    page.goto(f"{live_server.url}/analytics/analytics/NetworkSupervised")
    open_family(page, "network_anomalies")
    modal = page.locator(f"#{NEW_COMM}-{NEW_COMM}-modal")
    expect(modal).to_be_hidden()
    page.locator(f"#{NEW_COMM}-{NEW_COMM}-modal").locator(
        "xpath=preceding::i[contains(@class,'bi-question-circle')][1]"
    ).click()
    expect(modal).to_be_visible()
    expect(modal.locator(".modal-title")).to_have_text(NEW_COMM)
    expect(modal.get_by_role("heading", name="Model")).to_be_visible()
    check_clean(page, problems)


def test_analytic_settings_modal_saves(live_server, page, problems, analytics_db):
    page.goto(f"{live_server.url}/analytics/analytics/NetworkSupervised")
    open_family(page, "network_anomalies")
    modal = page.locator(f"#{NEW_COMM}-{NEW_COMM}-ui-modal")
    page.locator(f"#{NEW_COMM}-{NEW_COMM}-modal").locator(
        "xpath=preceding::i[contains(@class,'bi-sliders')][1]"
    ).click()
    expect(modal).to_be_visible()
    modal.locator("input[name=run_idx]").fill("e2e-run-idx-*")
    with page.expect_response("**/analytic_user_input/**") as resp:
        modal.get_by_role("button", name="Save").click()
    assert resp.value.ok
    expect(page.locator(SUCCESS_TOAST)).to_contain_text("updated analytic's settings")
    expect(modal).to_be_hidden()
    ui = Analytic.objects.get(analytic_class__endswith=NEW_COMM).ui
    assert ui.run_idx_name == "e2e-run-idx-*"
    check_clean(page, problems)


def test_trained_models_list_and_metadata_modal(live_server, page, problems, trained_model):
    page.goto(f"{live_server.url}/analytics/analytics/NetworkSupervised")
    row = page.locator(f"#trained-model-{trained_model.pk}-line")
    expect(row.locator("button.model-btn")).to_have_text("e2e_trained_model")
    check_clean(page, problems)

    # Selecting a model marks it active.
    row.locator("button.model-btn").click()
    expect(row.locator("button.model-btn")).to_have_class(re.compile("active"))

    row.locator(".bi-info-circle").click()
    modal = page.locator(f"[id='{trained_model.pk}-meta-modal']")
    expect(modal).to_be_visible()
    expect(modal.locator("code")).to_have_text("plain drawing text")
    expect(modal.get_by_role("row", name="Index e2e-packets-*")).to_be_visible()
    expect(modal.get_by_role("row", name=f"Analytic {NEW_COMM}")).to_be_visible()


def test_trained_models_fragment(live_server, page, problems, trained_model):
    page.goto(f"{live_server.url}/analytics/get_models/NetworkSupervised")
    expect(page.locator("button.model-btn")).to_have_text("e2e_trained_model")
    assert not problems, problems


def test_network_supervised_predict(live_server, page, problems, trained_model):
    page.goto(f"{live_server.url}/analytics/analytics/NetworkSupervised")
    page.locator(f"#trained-model-{trained_model.pk}-line button.model-btn").click()
    expect(page.locator("#predict_btn")).to_be_enabled()
    data = {"alerts": [{"msg": "odd flow"}], "outidx": "e2e-alerts"}
    with (
        mock.patch(f"{VIEWS}.predict", return_value=data) as predict,
        page.expect_response("**/predict_model/") as resp,
    ):
        page.click("#predict_btn")
    assert resp.value.ok, resp.value.text()
    predict.assert_called_once()
    expect(page.locator("#data_box code")).to_contain_text("odd flow")
    expect(page.locator("#outgoing_idx")).to_have_value("e2e-alerts")
    expect(page.locator(ERROR_TOAST)).to_have_count(0)


def test_delete_trained_model(live_server, page, problems, trained_model):
    page.goto(f"{live_server.url}/analytics/analytics/NetworkSupervised")
    row = page.locator(f"#trained-model-{trained_model.pk}-line")
    row.locator(".bi-x-lg").click()
    modal = page.locator("#delete-model-modal")
    expect(modal).to_be_visible()
    expect(modal.locator("input[name=delete-this-model]")).to_have_value("e2e_trained_model")
    with page.expect_response("**/train_model/delete/**") as resp:
        modal.locator("#confirm-delete-model").click()
    assert resp.value.ok
    assert not TrainedModel.objects.filter(pk=trained_model.pk).exists()
    expect(row).to_have_count(0)
    assert not [p for p in problems if p.startswith("pageerror")], problems
