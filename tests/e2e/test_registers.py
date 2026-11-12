"""Smoke tests: load Modbus data on the Registers page, chart it, open the State tab."""

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

CHART = "{} canvas, {} svg"
ERROR_TOAST = ".toast-header.bg-danger"
STATE_URL = "**/registers/state/state/**"


def load_and_select(page, live_server, index):
    """Load ``index``, open the first IP pair and tick its two changing registers.

    Returns the pending /registers/state/state/ response, which the page requests
    whenever the register selection changes.
    """
    page.goto(f"{live_server.url}/registers/")
    page.fill("#index", index)
    page.click("#load-data-submit")

    # Both IP pairs are listed; open the first one.
    pairs = page.locator("#conversation-list button")
    expect(pairs).to_have_count(2)
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    pairs.first.click()

    # MODBUS_PRESENT (constant marker) plus the three input registers; tick the
    # two that change.
    registers = page.locator(".register-check:visible")
    expect(registers).to_have_count(4)
    registers.nth(1).check()
    with page.expect_response(STATE_URL) as state:
        registers.nth(2).check()
    return state


def test_registers_chart(live_server, page, modbus_index):
    failed = []
    page.on(
        "response",
        lambda r: (
            failed.append(f"{r.status} {r.url}")
            if r.status >= 400 and "/state/state/" not in r.url
            else None
        ),
    )

    load_and_select(page, live_server, modbus_index)

    expect(page.locator(CHART.format(*["#register-graph"] * 2)).first).to_be_visible()
    assert not failed, failed


# Remove this marker once sandialabs/parseus#6 (State tab 400, read-only memoryview in
# HDBSCAN) is merged; strict means CI then fails as a reminder to do so.
@pytest.mark.xfail(
    strict=True,
    reason="State tab returns 400 until sandialabs/parseus#6 is merged",
)
def test_state_tab(live_server, page, modbus_index):
    state = load_and_select(page, live_server, modbus_index)

    page.click("#nav-profile-tab")
    assert state.value.ok, f"{state.value.status} {state.value.text()}"
    expect(page.locator("#state-graph-select")).to_have_value(
        "Register State Over Time"
    )
    expect(page.locator(CHART.format(*["#state-graph"] * 2)).first).to_be_visible()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
