"""Tests for the GPIB socket ingestor (sockets, clocks and the datastore are faked)."""

import importlib
import json
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from parseus.ingestors.gpib import logger as gpib_logger


@pytest.fixture
def parser(tmp_path, monkeypatch):
    monkeypatch.setenv("INGESTOR_LOG_FILE", str(tmp_path / "logs" / "gpib.log"))
    monkeypatch.setenv("INGESTOR_CAPTURE_DIR", str(tmp_path / "captures"))
    monkeypatch.setenv("DATASTORE_HOST_URL", "http://localhost:9")
    sys.modules.pop("parseus.ingestors.gpib.parser", None)
    module = importlib.import_module("parseus.ingestors.gpib.parser")
    monkeypatch.setattr(module, "EVENT_BUFFER", [])
    monkeypatch.setattr(module, "LAST_EVENT_TIME", None)
    monkeypatch.setattr(module, "LAST_EVENT_HASH", None)
    yield module
    for handler in list(module.logger.handlers):
        module.logger.removeHandler(handler)
        handler.close()
    sys.modules.pop("parseus.ingestors.gpib.parser", None)


# ---------------------------------------------------------------------- logger


def test_int_from_env(monkeypatch):
    monkeypatch.delenv("X_INT", raising=False)
    assert gpib_logger._int_from_env("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "12")
    assert gpib_logger._int_from_env("X_INT", 7) == 12
    monkeypatch.setenv("X_INT", "twelve")
    assert gpib_logger._int_from_env("X_INT", 7) == 7


# -------------------------------------------------------------------- settings


def test_settings_validation(parser, monkeypatch):
    assert parser.load_settings().gpib_port == 1234
    monkeypatch.setenv("INGESTOR_GPIB_PORT", "0")
    with pytest.raises(ValueError, match="INGESTOR_GPIB_PORT must be >= 1"):
        parser.load_settings()
    monkeypatch.setenv("INGESTOR_GPIB_PORT", "1234")
    monkeypatch.setenv("INGESTOR_GPIB_RECONNECT_DELAY_BASE", "50")
    with pytest.raises(ValueError, match="DELAY_BASE must be <="):
        parser.load_settings()


# --------------------------------------------------------------------- counter


def test_next_counter_counts_per_file(parser, tmp_path):
    counter = tmp_path / "day" / ".counter"
    assert [parser._next_counter(counter) for _ in range(3)] == [1, 2, 3]
    assert counter.read_text() == "3"
    counter.write_text("")
    assert parser._next_counter(counter) == 1


# ---------------------------------------------------------------- close_socket


class FakeSocket:
    def __init__(self, payloads=(), fail=False):
        self.payloads = list(payloads)
        self.fail = fail
        self.calls = []

    def shutdown(self, how):
        self.calls.append("shutdown")
        if self.fail:
            raise OSError("boom")

    def close(self):
        self.calls.append("close")
        if self.fail:
            raise OSError("boom")

    def getpeername(self):
        return ("10.0.0.1", 5555)

    def getsockname(self):
        return ("10.0.0.2", 1234)

    def recv(self, size):
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_close_socket_tolerates_none_and_errors(parser):
    parser.close_socket(None)
    sock = FakeSocket()
    parser.close_socket(sock)
    assert sock.calls == ["shutdown", "close"]
    failing = FakeSocket(fail=True)
    parser.close_socket(failing)
    assert failing.calls == ["shutdown", "close"]


# --------------------------------------------------------------- process_payload


@pytest.fixture
def pushed(parser, monkeypatch):
    docs = []
    monkeypatch.setattr(
        parser, "push_to_datastore", lambda doc, datastore, index_base: docs.append(doc)
    )
    return docs


def test_payload_is_pushed_and_buffered(parser, pushed):
    parser.process_payload(b"*IDN? ok\x00\r\n", FakeSocket())
    assert len(pushed) == 1
    doc = pushed[0]
    assert doc["data"] == "*IDN? ok"
    assert doc["ip"] == {"source": "10.0.0.1", "destination": "10.0.0.2"}
    assert doc["tcp"] == {"src_port": 5555, "dst_port": 1234}
    assert doc["source"] == "GPIB_SOCKET"
    assert doc["payload_bytes"] == len(b"*IDN? ok\x00\r\n")
    (event,) = parser.EVENT_BUFFER
    assert event["data"] == "*IDN? ok"
    assert event["source_ip"] == "10.0.0.1" and event["destination_port"] == 1234
    assert parser.LAST_EVENT_TIME is not None


def test_consecutive_duplicate_payloads_are_skipped(parser, pushed):
    sock = FakeSocket()
    parser.process_payload(b"same", sock)
    parser.process_payload(b"same", sock)
    parser.process_payload(b"other", sock)
    parser.process_payload(b"same", sock)
    assert [d["data"] for d in pushed] == ["same", "other", "same"]


def test_blank_payload_is_ignored(parser, pushed):
    parser.process_payload(b"\x00\r\n", FakeSocket())
    assert pushed == [] and parser.EVENT_BUFFER == []


def test_push_failure_does_not_lose_the_event(parser, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(parser, "push_to_datastore", fail)
    parser.process_payload(b"data", FakeSocket())
    assert [e["data"] for e in parser.EVENT_BUFFER] == ["data"]


def test_buffer_flushes_to_disk_at_max_events(parser, pushed, monkeypatch):
    monkeypatch.setattr(
        parser, "SETTINGS", replace(parser.SETTINGS, max_events_per_flush=2)
    )
    sock = FakeSocket()
    parser.process_payload(b"one", sock)
    assert parser.EVENT_BUFFER
    parser.process_payload(b"two", sock)
    assert parser.EVENT_BUFFER == []
    files = list(parser.SETTINGS.capture_dir.rglob("capture_*.ndjson"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert [r["data"] for r in rows] == ["one", "two"]


def test_flush_events_with_empty_buffer_writes_nothing(parser):
    parser.flush_events()
    assert not parser.SETTINGS.capture_dir.exists()


def test_capture_files_are_numbered_sequentially(parser):
    first = parser.write_events_to_file([{"a": 1}])
    second = parser.write_events_to_file([{"a": 2}])
    assert first.name == "capture_1.ndjson" and second.name == "capture_2.ndjson"
    assert json.loads(second.read_text()) == {"a": 2}
    assert not list(Path(first.parent).glob("*.tmp"))


def test_push_to_datastore_indexes_into_dated_index(parser):
    calls = []

    class Store:
        def index(self, index, document):
            calls.append((index, document))
            return {"result": "created"}

    parser.push_to_datastore({"k": 1}, Store(), "scpi")
    ((index, doc),) = calls
    assert index.startswith("scpi-") and len(index) == len("scpi-2024-01-01")
    assert doc == {"k": 1}


# ------------------------------------------------------- receive loop and main


class FakeEvent:
    """Event whose wait() records the delay and shuts down after ``stop_after``."""

    def __init__(self, stop_after=1):
        self.flag = False
        self.waits = []
        self.stop_after = stop_after

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, delay):
        self.waits.append(delay)
        if len(self.waits) >= self.stop_after:
            self.flag = True


def test_receive_loop_processes_until_shutdown(parser, monkeypatch):
    seen = []
    event = FakeEvent()
    monkeypatch.setattr(parser, "_shutdown", event)

    def process(payload, sock):
        seen.append(payload)
        if len(seen) == 2:
            event.set()

    monkeypatch.setattr(parser, "process_payload", process)
    parser.receive_and_process_loop(FakeSocket([b"a", b"b"]))
    assert seen == [b"a", b"b"]


def test_receive_loop_raises_on_timeout_and_closed_connection(parser):
    with pytest.raises(RuntimeError, match="timeout"):
        parser.receive_and_process_loop(FakeSocket([socket.timeout()]))
    with pytest.raises(RuntimeError, match="broken"):
        parser.receive_and_process_loop(FakeSocket([b""]))


def test_request_shutdown_sets_event(parser, monkeypatch):
    event = FakeEvent()
    monkeypatch.setattr(parser, "_shutdown", event)
    parser.request_shutdown(15, None)
    assert event.flag


class FakeThread:
    started = []

    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        FakeThread.started.append(self.target)


@pytest.fixture
def main_env(parser, monkeypatch):
    registered = []
    monkeypatch.setattr(parser.signal, "signal", lambda *a: registered.append(a))
    monkeypatch.setattr(parser.atexit, "register", lambda *a: None)
    monkeypatch.setattr(parser, "Thread", FakeThread)
    FakeThread.started = []
    flushed = []
    monkeypatch.setattr(
        parser, "flush_events", lambda reason="x": flushed.append(reason)
    )
    monkeypatch.setattr(parser, "close_socket", lambda sock: None)
    return registered, flushed


def test_main_reconnects_with_exponential_backoff(parser, main_env, monkeypatch):
    registered, flushed = main_env
    event = FakeEvent(stop_after=4)
    monkeypatch.setattr(parser, "_shutdown", event)
    monkeypatch.setattr(
        parser,
        "SETTINGS",
        replace(
            parser.SETTINGS,
            reconnect_delay_base_seconds=1.0,
            reconnect_delay_max_seconds=3.0,
        ),
    )
    outcomes = iter(
        [socket.error("lost"), RuntimeError("closed"), ValueError("odd"), None]
    )

    def make_socket():
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome
        return FakeSocket()

    monkeypatch.setattr(parser, "make_socket", make_socket)
    monkeypatch.setattr(parser, "receive_and_process_loop", lambda sock: None)
    parser.main()
    # the delay doubles per failure up to the cap, and resets after a connect
    assert event.waits == [1.0, 2.0, 3.0, 1.0]
    assert flushed == ["shutdown"]
    assert FakeThread.started == [parser.idle_flush_loop]
    assert len(registered) == 2


def test_main_waits_only_the_base_delay_after_a_successful_connect(
    parser, main_env, monkeypatch
):
    event = FakeEvent(stop_after=2)
    monkeypatch.setattr(parser, "_shutdown", event)
    monkeypatch.setattr(parser, "make_socket", lambda: FakeSocket())
    monkeypatch.setattr(parser, "receive_and_process_loop", lambda sock: None)
    parser.main()
    base = parser.SETTINGS.reconnect_delay_base_seconds
    assert event.waits == [base, base]


def test_main_exits_without_waiting_when_shutdown_requested(
    parser, main_env, monkeypatch
):
    event = FakeEvent()
    monkeypatch.setattr(parser, "_shutdown", event)
    monkeypatch.setattr(parser, "make_socket", lambda: FakeSocket())

    def loop(sock):
        event.set()
        raise socket.error("closed during shutdown")

    monkeypatch.setattr(parser, "receive_and_process_loop", loop)
    parser.main()
    assert event.waits == []


def test_make_socket_connects_with_configured_timeout(parser, monkeypatch):
    created = []

    class Conn:
        def settimeout(self, value):
            created.append(("settimeout", value))

    def create_connection(address, timeout):
        created.append((address, timeout))
        return Conn()

    monkeypatch.setattr(parser.socket, "create_connection", create_connection)
    parser.make_socket()
    timeout = parser.SETTINGS.socket_timeout_seconds
    assert created == [
        ((parser.SETTINGS.gpib_host, parser.SETTINGS.gpib_port), timeout),
        ("settimeout", timeout),
    ]


# -------------------------------------------------------------- idle_flush_loop


def test_idle_flush_loop_flushes_stale_buffer(parser, monkeypatch):
    event = FakeEvent(stop_after=2)
    monkeypatch.setattr(parser, "_shutdown", event)
    flushed = []
    monkeypatch.setattr(parser, "flush_events", lambda reason: flushed.append(reason))
    parser.EVENT_BUFFER.append({"x": 1})
    parser.LAST_EVENT_TIME = time.time() - 1000
    parser.idle_flush_loop()
    assert len(flushed) == 2  # every pass sees the (faked, never drained) buffer
    assert flushed[0].startswith("idle for 100")


def test_idle_flush_loop_ignores_fresh_or_empty_buffers(parser, monkeypatch):
    flushed = []
    monkeypatch.setattr(parser, "flush_events", lambda reason: flushed.append(reason))
    monkeypatch.setattr(parser, "_shutdown", FakeEvent(stop_after=2))
    parser.idle_flush_loop()  # empty buffer
    parser.EVENT_BUFFER.append({"x": 1})
    parser.LAST_EVENT_TIME = None
    monkeypatch.setattr(parser, "_shutdown", FakeEvent(stop_after=2))
    parser.idle_flush_loop()  # no last event time
    parser.LAST_EVENT_TIME = time.time()
    monkeypatch.setattr(parser, "_shutdown", FakeEvent(stop_after=2))
    parser.idle_flush_loop()  # event is recent
    assert flushed == []


def test_idle_flush_loop_survives_errors(parser, monkeypatch):
    monkeypatch.setattr(parser, "_shutdown", FakeEvent(stop_after=2))
    parser.EVENT_BUFFER.append({"x": 1})
    parser.LAST_EVENT_TIME = time.time() - 1000

    def boom(reason):
        raise RuntimeError("disk full")

    monkeypatch.setattr(parser, "flush_events", boom)
    parser.idle_flush_loop()  # does not raise
