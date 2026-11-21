"""Project pages: home, login, login modal, logout, datastore status and settings modal."""

import pytest
from playwright.sync_api import expect

from tests.globals import DATASTORE_HOST_URL

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

ERROR_TOAST = ".toast-header.bg-danger"
USERNAME, PASSWORD = "e2e-user", "e2e-pass-123!"


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(USERNAME, password=PASSWORD)


def sign_in_via_modal(page, live_server):
    page.goto(f"{live_server.url}/")
    page.click("#login-modal-button")
    modal = page.locator("#login-modal")
    expect(modal).to_be_visible()
    modal.locator("#id_username").fill(USERNAME)
    modal.locator("#id_password").fill(PASSWORD)
    modal.locator("#login-button").click()


@pytest.fixture
def logged_in(page, live_server, user, problems):
    # problems first, so the login flow itself is watched for errors.
    sign_in_via_modal(page, live_server)
    expect(page.locator("#user-console-button")).to_be_visible()
    return page


def test_home(live_server, page, problems):
    page.goto(f"{live_server.url}/")
    expect(page.locator(".fs-3", has_text="Welcome")).to_be_visible()
    expect(page.locator(".navbar-brand .parseus-brand-title")).to_have_text("Parseus")
    links = page.locator(".card-link")
    expect(links.first).to_be_visible()

    # Follow a feature link from the home page.
    target = links.first.get_attribute("href")
    links.first.click()
    expect(page).to_have_url(f"{live_server.url}{target}")
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_login_page(live_server, page, user, problems):
    page.goto(f"{live_server.url}/login/")
    form = page.locator(".loginBox form")
    expect(page.locator(".loginBox h3")).to_have_text("Sign in here")
    expect(form).to_be_visible()

    form.locator("#id_username").fill(USERNAME)
    form.locator("#id_password").fill(PASSWORD)
    form.locator("button[type=submit]").click()

    # Logged in: the user menu shows the name.
    expect(page.locator(".parseus-user-name")).to_have_text(USERNAME)
    expect(page.locator("#login-modal-button")).to_have_count(0)
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_login_page_redirects_home(live_server, page, user):
    page.goto(f"{live_server.url}/login/")
    form = page.locator(".loginBox form")
    form.locator("#id_username").fill(USERNAME)
    form.locator("#id_password").fill(PASSWORD)
    form.locator("button[type=submit]").click()
    expect(page.locator(".parseus-user-name")).to_have_text(USERNAME)
    expect(page).to_have_url(f"{live_server.url}/")


def test_login_modal(live_server, page, user, problems):
    page.goto(f"{live_server.url}/")
    page.click("#login-modal-button")
    modal = page.locator("#login-modal")
    expect(modal).to_be_visible()
    expect(modal.locator("#login-modal-title")).to_have_text("Welcome back to Parseus")

    # Wrong password: stays anonymous, message shown inline.
    modal.locator("#id_username").fill(USERNAME)
    modal.locator("#id_password").fill("wrong")
    modal.locator("#login-button").click()
    expect(modal.locator("#loginMessage")).to_have_text("Invalid credentials.")
    expect(page.locator("#login-modal-button")).to_be_visible()

    # Correct password: page reloads logged in.
    modal.locator("#id_password").fill(PASSWORD)
    modal.locator("#login-button").click()
    expect(page.locator(".parseus-user-name")).to_have_text(USERNAME)
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_logout(live_server, logged_in, problems):
    page = logged_in
    page.click("#user-console-button")
    expect(page.locator(".parseus-user-dropdown-name")).to_have_text(USERNAME)
    page.click(".parseus-signout-item")
    expect(page.locator("#login-modal-button")).to_be_visible()
    expect(page.locator("#user-console-button")).to_have_count(0)
    assert not problems, problems


def test_database_status(live_server, logged_in, problems):
    """The navbar polls /database/status/ and shows the datastore as live."""
    page = logged_in
    expect(page.locator("#user-console-button")).to_have_class(
        "btn parseus-user-link d-inline-flex align-items-center text-start is-datastore-online"
    )
    page.click("#user-console-button")
    expect(page.locator("#database-status-text")).to_have_text("Datastore live")

    status = page.request.post(
        f"{live_server.url}/database/status/",
        headers={"X-CSRFToken": page.locator("[name=csrfmiddlewaretoken]").first.input_value()},
    ).json()
    assert status["status"] is True
    assert status["message"].startswith("Connected to")
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def open_settings(page):
    page.click("#user-console-button")
    page.click("#settings-modal-button")
    modal = page.locator("#settings-modal")
    expect(modal).to_be_visible()
    return modal


def test_settings_modal_save(live_server, logged_in, problems):
    page = logged_in
    modal = open_settings(page)
    expect(modal.locator("#settings-modal-title")).to_have_text("Settings")
    expect(modal.locator("#elastic-form")).to_be_visible()

    modal.locator("#id_datastore_host").fill(DATASTORE_HOST_URL)
    modal.locator("#id_datastore_type").select_option("elasticsearch")
    with page.expect_response("**/database/") as saved:
        modal.locator("#elastic-form-save").click()
    assert saved.value.ok
    assert saved.value.json() == {"host": DATASTORE_HOST_URL, "messages": []}

    expect(page.locator(".toast-header.bg-success")).to_be_visible()
    expect(page.locator(".toast-body", has_text="Saved Datastore settings")).to_be_visible()
    expect(modal).to_be_hidden()
    expect(page.locator(ERROR_TOAST)).to_have_count(0)
    assert not problems, problems


def test_settings_modal_unreachable_host(live_server, logged_in):
    """Proves the error-toast selector: an unreachable host raises a bg-danger toast,
    and the status poll then reports the datastore offline."""
    page = logged_in
    modal = open_settings(page)
    modal.locator("#id_datastore_host").fill("http://127.0.0.1:1")
    modal.locator("#elastic-form-save").click()

    expect(page.locator(ERROR_TOAST)).to_be_visible()
    expect(page.locator(".toast-body", has_text="Failed to connect to database")).to_be_visible()

    page.reload()
    page.click("#user-console-button")
    expect(page.locator("#database-status-text")).to_have_text("Datastore offline")


def test_settings_modal_invalid_url(live_server, logged_in):
    page = logged_in
    modal = open_settings(page)
    modal.locator("#id_datastore_host").fill("not a url")
    modal.locator("#elastic-form-save").click()
    expect(modal.locator("#elastic-datastore_host-invalid")).to_have_text("Enter a valid URL.")
    expect(modal.locator("#id_datastore_host")).to_have_class("form-control is-invalid")
