"""Smoke tests for the PEAT (pei) pages: home, configs, hosts, default runs, run details.

No PEAT run is ever started: the Celery ``apply_async`` calls and the Redis target
locks are patched at the view boundary, and run output is seeded under ``tmp_path``.
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from django.core.files.base import ContentFile
from playwright.sync_api import expect

from parseus.webapp.pei.models import PeatConfig, PeatPull, PeatScan

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

# toastError() in project/static/js/toast.js renders <div class="toast-header bg-danger">.
ERROR_TOAST = ".toast-header.bg-danger"

CONFIG_YAML = {
    "hosts": [
        {
            "label": "plc-one",
            "comment": "first plc",
            "identifiers": {"ip": "10.0.0.5"},
            "peat_module": "controllogix",
        },
        {
            "label": "plc-two",
            "comment": "second plc",
            "identifiers": {"ip": "10.0.0.6"},
            "peat_module": "controllogix",
        },
    ]
}

# The log page opens a websocket served by the ASGI app; the live server is WSGI, so
# swap in a socket that never connects instead of letting it fail with console errors.
FAKE_WEBSOCKET = """
window.WebSocket = class { constructor() { this.readyState = 0; }
                           send() {} close() { this.readyState = 3; } };
window.WebSocket.OPEN = 1;
"""


@pytest.fixture(autouse=True)
def _peat_storage(settings, tmp_path):
    """Keep uploaded configs and run output under ``tmp_path``."""
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.PEAT_PULL_DIR = tmp_path / "pulls"
    settings.PEAT_SCAN_DIR = tmp_path / "scans"
    for directory in (settings.PEAT_PULL_DIR, settings.PEAT_SCAN_DIR):
        directory.mkdir()


@pytest.fixture
def browser_errors(page, problems):
    """HTTP and JS errors (see ``problems``), with the fake WebSocket installed."""
    page.add_init_script(FAKE_WEBSOCKET)
    return problems


def assert_clean(page, errors):
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not errors, errors


def make_config(name="e2e-config", hosts=CONFIG_YAML):
    config = PeatConfig(name=name, description="seeded", author="e2e")
    config.filename.save("e2e.yml", ContentFile(yaml.dump(hosts).encode()), save=False)
    config.save()
    return config


@pytest.fixture
def config():
    return make_config()


def seed_run_files(root, method, device="plc-one"):
    """Write the files PEAT leaves behind for a finished run."""
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "console.log").write_text("PEAT finished the run\n")
    (root / "summaries").mkdir()
    (root / "summaries" / f"{method}-summary.json").write_text(
        json.dumps({"num_hosts_online": 1, "num_hosts_verified": 1})
    )
    (root / "devices" / device).mkdir(parents=True)
    (root / "devices" / device / "device-data-summary.json").write_text(
        json.dumps({"label": device, "ip": "10.0.0.5", "type": "PLC"})
    )


@pytest.fixture
def scan_run(settings):
    run = PeatScan.objects.create(
        name="seeded-scan",
        run_dir="seeded-scan",
        status="done",
        devices=["10.0.0.5"],
        modules=["all"],
    )
    seed_run_files(settings.PEAT_SCAN_DIR / run.run_dir, "scan")
    return run


@pytest.fixture
def pull_run(settings, config):
    run = PeatPull.objects.create(
        name="seeded-pull",
        run_dir="seeded-pull",
        status="done",
        devices=["plc-one: 10.0.0.5"],
        config=config,
    )
    seed_run_files(settings.PEAT_PULL_DIR / run.run_dir, "pull")
    return run


@pytest.fixture
def no_peat_run():
    """Stand-in for the lock + Celery boundary so submitting never starts PEAT."""
    locks = SimpleNamespace(acquired=True, targets=["10.0.0.5"], busy_targets=[])
    result = MagicMock(id="fake-task-id")
    with (
        patch("parseus.webapp.pei.views.acquire_target_locks", return_value=locks),
        patch("parseus.webapp.pei.views.peat_scan.apply_async", return_value=result) as scan,
        patch("parseus.webapp.pei.views.peat_pull.apply_async", return_value=result) as pull,
    ):
        yield SimpleNamespace(scan=scan, pull=pull)


def test_home(live_server, page, browser_errors):
    page.goto(f"{live_server.url}/peat/")

    expect(page.get_by_role("heading", name="How would you like to run PEAT?")).to_be_visible()
    expect(page.get_by_role("heading", name="Use configurations")).to_be_visible()
    page.get_by_role("link", name="Use configurations").click()
    expect(page).to_have_url(f"{live_server.url}/peat/config/")
    expect(page.get_by_role("heading", name="Manage PEAT configurations")).to_be_visible()
    assert_clean(page, browser_errors)


def test_config_list(live_server, page, browser_errors, config):
    page.goto(f"{live_server.url}/peat/config/")

    expect(page.get_by_role("heading", name="Manage PEAT configurations")).to_be_visible()
    row = page.locator("tbody tr")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("seeded")
    row.get_by_role("link", name="View Details").click()
    expect(page).to_have_url(f"{live_server.url}/peat/config/details/{config.pk}")
    expect(page.get_by_role("heading", name="e2e-config")).to_be_visible()
    assert_clean(page, browser_errors)


def test_config_list_delete(live_server, page, browser_errors, config):
    page.goto(f"{live_server.url}/peat/config/")

    remove = page.locator("#delete-button")
    expect(remove).to_be_disabled()
    page.check("input[name=config_ids]")
    expect(remove).to_be_enabled()
    remove.click()

    expect(page.get_by_text("No PEAT configurations yet.")).to_be_visible()
    assert not PeatConfig.objects.exists()
    assert_clean(page, browser_errors)


def test_config_detail_scan(live_server, page, browser_errors, config, no_peat_run):
    page.goto(f"{live_server.url}/peat/config/details/{config.pk}")

    expect(page.get_by_role("heading", name="e2e-config")).to_be_visible()
    expect(page.locator("#device-table-body tr")).to_have_count(2)
    expect(page.locator("#selected-host-count")).to_have_text("2 hosts selected")
    page.locator("input[name=device_ids]").nth(1).uncheck()
    expect(page.locator("#selected-host-count")).to_have_text("1 host selected")

    page.click("#scan-button")
    page.fill("#peat-run-name", "from-the-test")
    page.uncheck("#peat-run-push")  # pushing needs a reachable results database
    page.click("#peat-run-button")

    expect(page.locator(".toast-header.bg-success")).to_be_visible()
    expect(page.locator("#scan-history-count")).to_have_text("1")
    scan = PeatScan.objects.get()
    assert (scan.name, scan.config_id, scan.devices) == (
        "from-the-test",
        config.pk,
        ["plc-one: 10.0.0.5"],
    )
    no_peat_run.scan.assert_called_once()
    assert_clean(page, browser_errors)


def test_config_detail_pull_history(live_server, page, browser_errors, pull_run):
    page.goto(f"{live_server.url}/peat/config/details/{pull_run.config_id}")

    expect(page.locator("#pull-history-count")).to_have_text("1")
    row = page.locator("#peat-pull-body tr")
    expect(row).to_contain_text("seeded-pull")
    row.get_by_role("link", name="seeded-pull").click()
    expect(page).to_have_url(f"{live_server.url}/peat/pull/details/{pull_run.pk}")
    expect(page.get_by_role("heading", name="seeded-pull")).to_be_visible()
    assert_clean(page, browser_errors)


def test_config_builder(live_server, page, browser_errors):
    page.goto(f"{live_server.url}/peat/config/create/")

    expect(page.get_by_role("heading", name="Create a PEAT configuration")).to_be_visible()
    page.fill("#id_name", "built-in-test")
    page.fill("#id_author", "e2e")
    page.click("text=Add Device")
    modal = page.locator("#deviceModal")
    expect(modal).to_be_visible()
    page.fill("#device-modal-name", "plc-built")
    page.fill("#device-modal-ip", "10.1.1.1")
    page.select_option("#device-modal-module", "ControlLogix")
    page.click("#device-modal-save")
    expect(modal).to_be_hidden()
    expect(page.locator("#device-container")).to_contain_text("plc-built")

    page.click("form.peat-form button[type=submit]")

    # Wait for the redirect before reading the row the POST creates.
    expect(page).to_have_url(re.compile(r"/peat/config/details/\d+$"))
    config = PeatConfig.objects.get(name="built-in-test")
    expect(page).to_have_url(f"{live_server.url}/peat/config/details/{config.pk}")
    expect(page.get_by_role("heading", name="built-in-test")).to_be_visible()
    expect(page.locator("#device-table-body")).to_contain_text("plc-built")
    expect(page.locator("#device-table-body")).to_contain_text("10.1.1.1")
    assert_clean(page, browser_errors)


def test_config_upload(live_server, page, browser_errors, tmp_path):
    upload = tmp_path / "uploaded.yml"
    metadata = {"name": "uploaded-config", "description": "from a file", "author": "e2e"}
    upload.write_text(yaml.dump({"metadata": metadata, **CONFIG_YAML}))

    page.goto(f"{live_server.url}/peat/config/")
    page.get_by_role("button", name="New Config").click()
    page.get_by_role("link", name="Upload Config").click()
    expect(page.get_by_role("heading", name="Upload a PEAT configuration")).to_be_visible()

    # Choosing the file reveals the metadata fields, filled from the YAML.
    expect(page.locator("#metadata-inputs")).to_be_hidden()
    page.set_input_files("#id_filename", str(upload))
    expect(page.locator("#id_name")).to_have_value("uploaded-config")
    expect(page.locator("#id_author")).to_have_value("e2e")
    page.get_by_role("button", name="Upload").click()

    expect(page).to_have_url(re.compile(r"/peat/config/details/\d+$"))
    config = PeatConfig.objects.get(name="uploaded-config")
    assert config.description == "from a file"
    expect(page.get_by_role("heading", name="uploaded-config")).to_be_visible()
    expect(page.locator("#device-table-body")).to_contain_text("plc-two")
    assert_clean(page, browser_errors)


def test_config_edit(live_server, page, browser_errors, config):
    page.goto(f"{live_server.url}/peat/config/{config.pk}/edit/")

    expect(page.get_by_role("heading", name="Update e2e-config")).to_be_visible()
    expect(page.locator("#id_name")).to_have_value("e2e-config")
    page.fill("#id_name", "renamed-config")
    page.select_option("#id_debug", "2")
    page.click("form.peat-form button[type=submit]")

    expect(page).to_have_url(f"{live_server.url}/peat/config/details/{config.pk}")
    expect(page.get_by_role("heading", name="renamed-config")).to_be_visible()
    config.refresh_from_db()
    assert (config.name, config.debug) == ("renamed-config", 2)
    assert_clean(page, browser_errors)


def test_device_edit(live_server, page, browser_errors, config):
    device = config.hosts.get(label="plc-one")
    page.goto(f"{live_server.url}/peat/device/{device.pk}/edit/")

    expect(page.get_by_role("heading", name="Update plc-one")).to_be_visible()
    expect(page.locator("#id_ip")).to_have_value("10.0.0.5")
    page.fill("#id_comment", "edited comment")
    page.fill("#id_options", '{"web": {"user": "root"}}')
    page.click("form.peat-form button[type=submit]")

    expect(page).to_have_url(f"{live_server.url}/peat/config/details/{config.pk}")
    expect(page.locator("#device-table-body")).to_contain_text("edited comment")
    device.refresh_from_db()
    assert device.options == {"web": {"user": "root"}}
    assert_clean(page, browser_errors)


@pytest.mark.parametrize("method", ["scan", "pull"])
def test_default_run_page(live_server, page, browser_errors, no_peat_run, method):
    page.goto(f"{live_server.url}/peat/config/scan")

    expect(
        page.get_by_role("heading", name="Scan or pull by CIDR without a config")
    ).to_be_visible()
    expect(page.locator("#cidr-id")).to_be_visible()
    page.fill("#cidr-id", "10.0.0.5, 10.0.0.6")
    expect(page.locator("#target-preview")).to_have_text("Ready to run against 2 targets.")

    page.click(f"#{method}-button")
    page.fill("#peat-run-name", f"default {method}")
    page.uncheck("#peat-run-push")
    page.click("#peat-run-button")

    expect(page.locator(".toast-header.bg-success")).to_be_visible()
    expect(page.locator(f"#peat-{method}-body tr")).to_contain_text(f"default {method}")
    model = PeatScan if method == "scan" else PeatPull
    run = model.objects.get()
    assert (run.name, run.devices, run.config_id) == (
        f"default {method}",
        ["10.0.0.5", "10.0.0.6"],
        None,
    )
    getattr(no_peat_run, method).assert_called_once()
    assert_clean(page, browser_errors)


def test_default_run_needs_targets(live_server, page, browser_errors, no_peat_run):
    page.goto(f"{live_server.url}/peat/config/scan")

    page.click("#scan-button")

    # The one place an error toast is the expected outcome, which also proves the
    # ERROR_TOAST selector matches what the app renders.
    expect(page.locator(ERROR_TOAST)).to_be_visible()
    expect(page.locator(".toast-body")).to_contain_text("Provide a CIDR range")
    assert not PeatScan.objects.exists()
    assert not browser_errors, browser_errors


@pytest.mark.parametrize("method", ["scan", "pull"])
def test_run_details(live_server, page, browser_errors, scan_run, pull_run, method):
    run = scan_run if method == "scan" else pull_run
    page.goto(f"{live_server.url}/peat/{method}/details/{run.pk}")

    expect(page.get_by_role("heading", name=f"seeded-{method}")).to_be_visible()
    expect(page.locator("#obj-status")).to_contain_text("Done")
    expect(page.locator("#run-outcome")).to_contain_text("Run completed with device data.")
    expect(page.locator("#device-summary-list")).to_contain_text("plc-one")
    expect(page.locator("#run-file-tree")).to_contain_text("device-data-summary.json")

    # Following the console.log link opens the file the view serves.
    with page.expect_popup() as popup:
        page.locator("#important-files-list a", has_text="console.log").first.click()
    expect(popup.value.locator("body")).to_contain_text("PEAT finished the run")

    with page.expect_download() as download:
        page.click("#run-download-button")
    assert download.value.suggested_filename == f"seeded-{method}.zip"
    assert_clean(page, browser_errors)


def test_device_edit_without_comment(live_server, page, browser_errors):
    config = make_config(
        hosts={
            "hosts": [
                {
                    "label": "plain",
                    "identifiers": {"ip": "10.0.0.9"},
                    "peat_module": "ControlLogix",
                }
            ]
        }
    )
    device = config.hosts.get()
    page.goto(f"{live_server.url}/peat/device/{device.pk}/edit/")

    page.fill("#id_hostname", "plain.local")
    page.click("form.peat-form button[type=submit]")

    expect(page).to_have_url(f"{live_server.url}/peat/config/details/{config.pk}")
    device.refresh_from_db()
    assert device.hostname == "plain.local"
