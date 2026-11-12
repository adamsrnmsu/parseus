"""Characterization tests for project.views datastore helpers and DataStoreView."""

import json
from types import SimpleNamespace

import pytest
from django.conf import settings as dj_settings
from django.test import RequestFactory

from parseus.webapp.project import views

SALT = "NaCl123"
PASSWORD_HASH = f"pbkdf2_sha256$600000${SALT}$hash"


class FakeDataStore:
    created = []
    info_result = {"version": {"distribution": "elasticsearch"}}
    info_error = None

    def __init__(self, host, basic_auth=None, base=None):
        self.host, self.basic_auth, self.base = host, basic_auth, base
        FakeDataStore.created.append(self)

    def info(self):
        if FakeDataStore.info_error:
            raise FakeDataStore.info_error
        return FakeDataStore.info_result

    def __str__(self):
        return f"<{self.host}>"


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    FakeDataStore.created = []
    FakeDataStore.info_result = {"version": {"distribution": "elasticsearch"}}
    FakeDataStore.info_error = None
    monkeypatch.setattr(views, "DataStore", FakeDataStore)


def encrypted(password):
    return dj_settings.CIPHER.encrypt((SALT + password).encode()).decode()


def make_request(session=None, method="get", data=None):
    factory = RequestFactory()
    request = getattr(factory, method)("/x", data or {})
    request.session = dict(session or {})
    request.user = SimpleNamespace(password=PASSWORD_HASH)
    return request


AUTH_SESSION = {
    "datastore_host": "https://es.example:9200",
    "datastore_type": "opensearch",
    "datastore_authenticate": True,
    "datastore_username": "bob",
    "datastore_password": None,  # filled per test
}


def auth_session():
    return {**AUTH_SESSION, "datastore_password": encrypted("p@ss")}


def test_client_defaults_without_session(settings):
    client = views._get_database_client(make_request())
    assert (client.host, client.basic_auth, client.base) == (
        settings.DEFAULT_DATASTORE,
        None,
        "elasticsearch",
    )


def test_client_decrypts_password_and_strips_salt():
    client = views._get_database_client(make_request(auth_session()))
    assert client.host == "https://es.example:9200"
    assert client.basic_auth == ("bob", "p@ss")
    assert client.base == "opensearch"


def test_client_ignores_auth_when_credentials_incomplete():
    session = {**auth_session(), "datastore_username": ""}
    assert views._get_database_client(make_request(session)).basic_auth is None


def test_database_url_plain_and_with_credentials(settings):
    assert views.get_database_url(make_request()) == settings.DEFAULT_DATASTORE
    assert (
        views.get_database_url(make_request(auth_session()))
        == "https://bob:p@ss@es.example:9200"
    )


def test_database_status_connected_with_and_without_user():
    body = json.loads(views.get_database_status(make_request(auth_session())).content)
    assert body == {
        "status": True,
        "message": "Connected to https://es.example:9200 as bob",
    }
    plain = json.loads(views.get_database_status(make_request()).content)
    assert plain == {
        "status": True,
        "message": f"Connected to {dj_settings.DEFAULT_DATASTORE}",
    }


def test_database_status_failure_names_exception_type_only():
    FakeDataStore.info_error = ConnectionError("secret detail")
    body = json.loads(views.get_database_status(make_request()).content)
    assert body == {
        "status": False,
        "message": "Failed to connect to database: ConnectionError",
    }


FORM = {
    "datastore_host": "https://es.example:9200",
    "datastore_type": "elasticsearch",
    "datastore_authenticate": "on",
    "datastore_username": "bob",
    "datastore_password": "p@ss",
}


def post(data):
    request = make_request(method="post", data=data)
    return request, views.DataStoreView.as_view()(request)


def test_post_saves_session_and_reports_no_messages():
    request, response = post(FORM)
    assert response.status_code == 200
    assert json.loads(response.content) == {
        "host": "https://es.example:9200",
        "messages": [],
    }
    assert FakeDataStore.created[0].basic_auth == ("bob", "p@ss")
    session = request.session
    assert session["datastore_type"] == "elasticsearch"
    assert session["datastore_host"] == "https://es.example:9200"
    assert session["datastore_authenticate"] is True
    assert session["datastore_username"] == "bob"
    assert dj_settings.CIPHER.decrypt(session["datastore_password"]).decode() == SALT + "p@ss"


def test_post_without_authentication_connects_anonymously():
    data = {k: v for k, v in FORM.items() if k != "datastore_authenticate"}
    _, response = post(data)
    assert response.status_code == 200
    assert FakeDataStore.created[0].basic_auth is None


def test_post_warns_when_opensearch_selected_but_not_opensearch():
    _, response = post({**FORM, "datastore_type": "opensearch"})
    assert json.loads(response.content)["messages"] == [
        ["warning", "IncorrectDatastoreError: Wrong datastore type possibly selected"]
    ]


def test_post_accepts_matching_opensearch():
    FakeDataStore.info_result = {"version": {"distribution": "OpenSearch"}}
    _, response = post({**FORM, "datastore_type": "opensearch"})
    assert json.loads(response.content)["messages"] == []


def test_post_reports_connection_failure():
    FakeDataStore.info_error = TimeoutError()
    _, response = post(FORM)
    assert json.loads(response.content)["messages"] == [
        ["error", "TimeoutError: Failed to connect to database"]
    ]


def test_post_invalid_form_is_400():
    _, response = post({**FORM, "datastore_host": "not a url"})
    assert response.status_code == 400
    assert "datastore_host" in json.loads(response.content)
