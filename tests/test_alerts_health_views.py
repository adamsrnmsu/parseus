"""Characterization tests for alert health helpers and handle_alerts validation."""

import json
from collections import namedtuple

import pytest
from django.test import RequestFactory

from parseus.webapp.alerts import views

Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize(
    "size,expected",
    [
        (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.0 GB"), (1024**4, "1.0 TB"),
        (1024**5, "1.0 PB"), (2048 * 1024**5, "2048.0 PB"),
    ],
)
def test_format_bytes(size, expected):
    assert views._format_bytes(size) == expected


@pytest.mark.parametrize(
    "used,status,ok",
    [(10, "ok", True), (80, "warning", False), (89, "warning", False),
     (90, "critical", False), (100, "critical", False)],
)
def test_disk_health_thresholds(monkeypatch, used, status, ok):
    monkeypatch.setattr(views, "DISK_WARNING_PERCENT", 80.0)
    monkeypatch.setattr(views, "DISK_CRITICAL_PERCENT", 90.0)
    monkeypatch.setattr(views.shutil, "disk_usage", lambda path: Usage(100, used, 100 - used))
    out = views._get_disk_health()
    assert out["status"] == status and out["ok"] is ok and out["service"] == "disk"
    assert out["disk"]["used_percent"] == used
    assert out["disk"]["total_bytes"] == 100 and out["disk"]["free_bytes"] == 100 - used
    assert out["message"] == (
        f"{float(used)}% used ({used} B used / 100 B total, {100 - used} B free)"
    )


def test_disk_health_zero_total_is_zero_percent(monkeypatch):
    monkeypatch.setattr(views.shutil, "disk_usage", lambda path: Usage(0, 0, 0))
    out = views._get_disk_health()
    assert out["status"] == "ok" and out["disk"]["used_percent"] == 0


def test_disk_health_error(monkeypatch):
    def boom(path):
        raise OSError("gone")

    monkeypatch.setattr(views.shutil, "disk_usage", boom)
    out = views._get_disk_health()
    assert out["status"] == "error" and out["ok"] is False
    assert out["message"].endswith(": gone") and "disk" not in out


class _Redis:
    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error

    def get(self, key):
        self.key = key
        if self.error:
            raise self.error
        return self.payload


def _patch_redis(monkeypatch, fake):
    monkeypatch.setattr(views.redis, "from_url", lambda url, decode_responses: fake)


def test_ingestor_health_redis_unreachable(monkeypatch):
    _patch_redis(monkeypatch, _Redis(error=ConnectionError("refused")))
    out = views._get_ingestor_health("gpib")
    assert out["status"] == "error" and out["ok"] is False
    assert out["message"] == "Unable to read redis health key: refused"


def test_ingestor_health_no_data(monkeypatch):
    fake = _Redis(payload=None)
    _patch_redis(monkeypatch, fake)
    out = views._get_ingestor_health("gpib")
    assert out["status"] == "unknown" and out["message"] == "No health data yet"
    assert fake.key == f"{views.INGESTOR_HEALTH_REDIS_PREFIX}:gpib"


def test_ingestor_health_invalid_json(monkeypatch):
    _patch_redis(monkeypatch, _Redis(payload="{nope"))
    out = views._get_ingestor_health("gpib")
    assert out["status"] == "error" and out["message"] == "Invalid health payload format"


def test_ingestor_health_fills_missing_keys_without_overwriting(monkeypatch):
    _patch_redis(monkeypatch, _Redis(payload=json.dumps({"status": "ok", "age_seconds": 3})))
    out = views._get_ingestor_health("logstash")
    assert out == {
        "status": "ok", "age_seconds": 3, "service": "logstash",
        "message": "No message", "latest_event_at": None, "checked_at": None,
    }


def test_ingestor_health_view_combines_services(monkeypatch):
    _patch_redis(monkeypatch, _Redis(payload=None))
    monkeypatch.setattr(views.shutil, "disk_usage", lambda path: Usage(100, 1, 99))
    resp = views.ingestor_health(RequestFactory().get("/h"))
    body = json.loads(resp.content)
    assert set(body["services"]) == {"logstash", "gpib", "disk"}
    assert body["services"]["disk"]["status"] == "ok" and body["checked_at"]


# ------------------------------------------------------------ handle_alerts
def _post(payload):
    return views.handle_alerts(
        RequestFactory().post("/h", data=json.dumps(payload), content_type="application/json")
    )


def test_handle_alerts_rejects_get():
    assert views.handle_alerts(RequestFactory().get("/h")).status_code == 405


def test_handle_alerts_rejects_too_many_uuids(monkeypatch):
    monkeypatch.setattr(views, "MAX_ALERT_HANDLE_BATCH", 2)
    resp = _post({"handle_type": "dismissed", "uuids": ["a", "b", "c"]})
    assert resp.status_code == 400
    assert json.loads(resp.content)["error"] == "Too many alerts selected. Maximum is 2."


def test_handle_alerts_rejects_non_string_uuid():
    assert _post({"handle_type": "dismissed", "uuids": [1]}).status_code == 400


def test_handle_alerts_dedupes_and_describes_update(monkeypatch):
    calls = []

    class _Ctx:
        def __enter__(self):
            return "es"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(views, "_datastore_client", lambda request: _Ctx())
    monkeypatch.setattr(
        views, "_update_by_query_with_conflict_retry", lambda es, **kw: calls.append((es, kw))
    )
    one = json.loads(_post({"handle_type": "flagged", "uuids": ["a", "a", ""]}).content)
    assert one == {"message": "1 alert flagged"}
    two = json.loads(_post({"handle_type": "dismissed", "uuids": ["a", "b"]}).content)
    assert two == {"message": "2 alerts dismissed"}
    es, kw = calls[0]
    assert kw["query"] == {"terms": {"event.uuid.keyword": ["a"]}}
    assert kw["script"]["params"]["status"] is False  # flagged stays active
    assert calls[1][1]["script"]["params"]["status"] is True


def test_handle_alerts_maps_errors(monkeypatch):
    class _Ctx:
        def __enter__(self):
            return "es"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(views, "_datastore_client", lambda request: _Ctx())
    for exc, code in [(views.AlertUpdateError("c"), 502), (ValueError("v"), 400), (RuntimeError("r"), 500)]:
        def boom(es, **kw):
            raise exc

        monkeypatch.setattr(views, "_update_by_query_with_conflict_retry", boom)
        assert _post({"handle_type": "dismissed", "uuids": ["a"]}).status_code == code
