"""Fixtures for the Playwright end-to-end tests (``pytest -m e2e``).

Needs a reachable Elasticsearch (``DATASTORE_HOST_URL``, default localhost:9200),
the static assets from ``make setup``, and a browser (``playwright install chromium``).
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.globals import DATASTORE, DATASTORE_HOST_URL

# Playwright's sync API runs an event loop in the test thread; without this Django
# refuses ORM access from it (the live server's session lookups included).
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

# Two conversations, each reading 3 input registers starting at address 100. The
# values hold one state for 15 samples, then another, then the first again.
START = datetime(2024, 1, 1, tzinfo=UTC)
SAMPLES = 45
PAIRS = [("10.0.0.1", "10.0.0.2"), ("10.0.0.3", "10.0.0.2")]
STATES = [[10, 20, 30], [50, 60, 70]]


def modbus_doc(src, dst, when, values):
    """A Malcolm-shaped ``modbus_detailed`` document."""
    return {
        "@timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "event": {"dataset": "modbus_detailed"},
        "source": {"ip": src},
        "destination": {"ip": dst},
        "zeek": {
            "modbus": {"func": "READ_INPUT_REGISTERS"},
            "modbus_detailed": {"address": 100, "response_values": values},
        },
    }


@pytest.fixture
def modbus_index():
    index = f"e2e-modbus-{uuid.uuid4().hex[:8]}"
    docs = []
    for src, dst in PAIRS:
        for i in range(SAMPLES):
            when = START + timedelta(minutes=i)
            docs.append(modbus_doc(src, dst, when, STATES[(i // 15) % 2]))
            # Malcolm also logs each transaction as a plain "modbus" event, which
            # is what the Function Codes tab (loaded alongside the registers) reads.
            plain = modbus_doc(src, dst, when, [])
            plain["event"]["dataset"] = "modbus"
            docs.append(plain)
    # Malcolm's types for the fields the queries filter, sort and aggregate on.
    DATASTORE.create_index(
        index,
        mappings={
            "properties": {
                "@timestamp": {"type": "date"},
                "event": {"properties": {"dataset": {"type": "keyword"}}},
                "source": {"properties": {"ip": {"type": "ip"}}},
                "destination": {"properties": {"ip": {"type": "ip"}}},
                "zeek": {
                    "properties": {
                        "modbus": {"properties": {"func": {"type": "keyword"}}}
                    }
                },
            }
        },
    )
    assert DATASTORE.bulk_push_docs(index, docs), f"cannot seed {DATASTORE_HOST_URL}"
    yield index
    DATASTORE.delete_index(index)


@pytest.fixture(autouse=True)
def _http_session_cookies(settings):
    # The live server is plain http; secure cookies would drop the session that
    # carries the loaded register data between /query/ and /state/state/.
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
