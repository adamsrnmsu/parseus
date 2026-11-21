"""Smoke tests for the Alerts app: home, Sysmon rules list, create/edit/delete."""

import pytest
from playwright.sync_api import expect

from parseus.webapp.alerts.models import SysmonRule

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

ERROR_TOAST = ".toast-header.bg-danger"


@pytest.fixture
def rule():
    obj = SysmonRule.objects.create(
        name="Night shift admin",
        analytic_type=SysmonRule.USER,
        match_value="alice",
    )
    yield obj
    SysmonRule.objects.all().delete()


def test_alerts_home(live_server, page, problems):
    page.goto(f"{live_server.url}/alerts/")

    expect(page.get_by_role("heading", name="Alert Operations")).to_be_visible()
    expect(page.get_by_role("heading", name="SCPI Alerts")).to_be_visible()
    expect(page.get_by_role("heading", name="Sysmon Alerts")).to_be_visible()
    # The monitor panel fills in from /alerts/monitor-health/ (disk is always readable).
    expect(page.locator("#healthStatusLabel-disk")).not_to_have_text("Unknown")
    expect(page.locator("#healthMessage-disk")).to_contain_text("used")

    page.get_by_role("link", name="Manage Sysmon rules").click()
    expect(page).to_have_url(f"{live_server.url}/alerts/rules/")
    expect(page.get_by_role("heading", name="Sysmon Rule Deck")).to_be_visible()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_rules_list(live_server, page, rule, problems):
    page.goto(f"{live_server.url}/alerts/rules/")

    expect(page.get_by_role("heading", name="Active Policy Matrix")).to_be_visible()
    row = page.locator(".alerts-rules-table tbody tr")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Night shift admin")
    expect(row).to_contain_text("alice")
    expect(row).to_contain_text("Enabled")

    row.get_by_role("link", name="Edit").click()
    expect(page).to_have_url(f"{live_server.url}/alerts/rules/{rule.pk}/edit/")
    expect(page.locator("#id_name")).to_have_value("Night shift admin")
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_rules_empty(live_server, page, problems):
    page.goto(f"{live_server.url}/alerts/rules/")
    expect(page.get_by_text("No Sysmon rules yet")).to_be_visible()
    assert not problems, problems


def test_create_rule(live_server, page, problems):
    page.goto(f"{live_server.url}/alerts/rules/new/")
    expect(page.get_by_role("heading", name="Create Sysmon Rule")).to_be_visible()
    expect(page.locator("#sysmon-rule-form")).to_be_visible()

    page.fill("#id_name", "Contractor VPN")
    page.select_option("#id_analytic_type", "ip")
    page.fill("#id_match_value", "10.1.2.3")
    page.locator("#sysmon-rule-form").get_by_role("button", name="Save Rule").click()

    expect(page).to_have_url(f"{live_server.url}/alerts/rules/")
    expect(page.locator(".alerts-rule-toast")).to_have_text("Sysmon rule created.")
    row = page.locator(".alerts-rules-table tbody tr")
    expect(row).to_contain_text("Contractor VPN")
    expect(row).to_contain_text("10.1.2.3")
    expect(row).to_contain_text("Sysmon IP")
    assert SysmonRule.objects.get(name="Contractor VPN").match_value == "10.1.2.3"
    SysmonRule.objects.all().delete()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_create_rule_invalid(live_server, page):
    page.goto(f"{live_server.url}/alerts/rules/new/")
    page.fill("#id_name", "Bad window")
    page.fill("#id_match_value", "bob")
    page.fill("#id_allowed_windows_json", "not json")
    page.locator("#sysmon-rule-form").get_by_role("button", name="Save Rule").click()

    expect(page.locator(".alerts-field-error").last).to_be_attached()
    expect(page.get_by_text("Invalid JSON")).to_be_visible()
    assert not SysmonRule.objects.exists()


def test_edit_rule(live_server, page, rule, problems):
    page.goto(f"{live_server.url}/alerts/rules/{rule.pk}/edit/")
    expect(page.get_by_role("heading", name="Edit Sysmon Rule")).to_be_visible()

    page.fill("#id_name", "Day shift admin")
    page.uncheck("#id_enabled")
    page.locator("#sysmon-rule-form").get_by_role("button", name="Save Rule").click()

    expect(page.locator(".alerts-rule-toast")).to_have_text("Sysmon rule updated.")
    row = page.locator(".alerts-rules-table tbody tr")
    expect(row).to_contain_text("Day shift admin")
    expect(row).to_contain_text("Disabled")
    rule.refresh_from_db()
    assert (rule.name, rule.enabled) == ("Day shift admin", False)
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_delete_rule(live_server, page, rule, problems):
    page.goto(f"{live_server.url}/alerts/rules/")
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Delete").click()

    expect(page.locator(".alerts-rule-toast")).to_have_text("Sysmon rule deleted.")
    expect(page.get_by_text("No Sysmon rules yet")).to_be_visible()
    assert not SysmonRule.objects.exists()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems
