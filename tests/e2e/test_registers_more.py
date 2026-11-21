"""Registers pages beyond the main chart: landing page, Function Codes tab, modals."""

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

ERROR_TOAST = ".toast-header.bg-danger"  # toast.js: createToast("bg-danger", ...)
CHART = "{} canvas, {} svg"


def load(page, live_server, index):
    """Open the analysis page and load ``index`` (registers and function codes)."""
    page.goto(f"{live_server.url}/registers/")
    page.fill("#index", index)
    page.click("#load-data-submit")
    expect(page.locator("#conversation-list button")).to_have_count(2)
    expect(page.locator("#fc-conversation-list button")).to_have_count(2)


def test_error_toast_selector_is_real(live_server, page):
    # Guards the negative toast checks below against being vacuous.
    page.goto(f"{live_server.url}/registers/")
    page.evaluate("toastError('boom', 10000)")
    expect(page.locator(ERROR_TOAST)).to_have_count(1)


def test_network_landing_page(live_server, page, problems):
    page.goto(f"{live_server.url}/registers/network/")

    expect(page.locator("h1")).to_have_text("Operational Technology Analysis")
    start = page.get_by_role("button", name="Start Analysis")
    expect(start).to_be_visible()
    start.click()

    expect(page).to_have_url(f"{live_server.url}/registers/")
    expect(page.locator("#load-data-submit")).to_be_visible()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_function_codes_tab(live_server, page, modbus_index, problems):
    load(page, live_server, modbus_index)

    page.click("#nav-function-codes-tab")
    expect(page.locator("#fcodes-container")).to_be_visible()
    # Summary: two conversations, and the modbus function code is counted.
    expect(page.locator("#fc-conversations")).to_have_text("2")
    expect(page.locator("#fc-main-summary-count")).to_contain_text("READ_INPUT_REGISTERS")

    # Selecting a conversation shows its summary, charts it and enables Group by.
    expect(page.locator("#groupby_btn")).to_be_disabled()
    page.locator("#fc-conversation-list button").first.click()
    expect(page.locator("#fc-conversation-summary h6:visible")).to_have_text("Summary stats")
    expect(page.locator(CHART.format(*["#fc-graph"] * 2)).first).to_be_visible()
    expect(page.locator("#groupby_btn")).to_be_enabled()

    # Re-query grouped by hours.
    page.select_option("#granularityGroupby", "h")
    with page.expect_response(lambda r: "/query/fc/?" in r.url and "groupby=1h" in r.url) as resp:
        page.click("#groupby_btn")
    assert resp.value.ok
    expect(page.locator("#graph_grouped_by")).to_have_value("1h")

    # Filtering protocols: unticking modbus hides every (modbus) conversation.
    page.click("button:has-text('Filters') >> nth=1")
    page.uncheck("#filter-modbus-check")
    expect(page.locator("#fc-conversation-list .fc-conversation-item:visible")).to_have_count(0)

    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_name_map_modal(live_server, page, modbus_index, problems):
    load(page, live_server, modbus_index)

    page.click("#name-mapping-btn")
    modal = page.locator("#name-map-modal")
    expect(modal).to_be_visible()
    expect(modal.locator(".modal-title")).to_have_text("Register Name Mapping")
    expect(
        modal.locator("#name-abbreviation-modal-body td", has_text="Input Registers")
    ).to_be_visible()
    # The short -> long table is built from the loaded data.
    table = modal.locator("#short-to-long-modal-body table")
    expect(table.locator("th")).to_have_text(["Short Name", "Long Name"])
    expect(table.locator("tbody tr")).not_to_have_count(0)

    modal.locator(".btn-close").click()
    expect(modal).to_be_hidden()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_register_stats_modal_and_filter(live_server, page, modbus_index, problems):
    load(page, live_server, modbus_index)
    page.locator("#conversation-list button").first.click()
    registers = page.locator(".register-check:visible")
    expect(registers).to_have_count(4)

    # Constant filter off leaves only the three varying registers.
    page.click("button:has-text('Filters') >> nth=0")
    page.uncheck("#filter-constant-check")
    expect(registers).to_have_count(3)

    # Stats modal for the first remaining register: values alternate 10 and 50.
    page.locator(".register-item:visible .register-modal-icon a").first.click()
    modal = page.locator("#register-modal")
    expect(modal).to_be_visible()
    expect(modal.locator("#register-name")).to_contain_text("10.0.0.1->10.0.0.2:")
    expect(modal.locator("#register-min")).to_have_text("10")
    expect(modal.locator("#register-max")).to_have_text("50")

    modal.locator(".btn-close").click()
    expect(modal).to_be_hidden()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_network_start_link_not_marked_disabled(live_server, page):
    page.goto(f"{live_server.url}/registers/network/")
    expect(page.get_by_role("button", name="Start Analysis")).to_be_enabled()
