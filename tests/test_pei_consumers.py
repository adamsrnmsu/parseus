"""Characterization tests for PeatLogConsumer (no channel layer, send is recorded)."""

import asyncio
import json

import pytest

from parseus.webapp.pei import consumers
from parseus.webapp.pei.consumers import PeatLogConsumer


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(consumers, "POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(consumers, "LOG_FILE_WAIT_ATTEMPTS", 3)


@pytest.fixture
def dirs(settings, tmp_path):
    settings.PEAT_PULL_DIR = tmp_path / "pulls"
    settings.PEAT_SCAN_DIR = tmp_path / "scans"
    return tmp_path


def make_consumer():
    consumer = PeatLogConsumer()
    consumer.sent = []

    async def send(text_data=None, **kwargs):
        consumer.sent.append(json.loads(text_data))

    consumer.send = send
    consumer.file_path = None
    consumer.file_offset = 0
    consumer.parent_run_dir = None
    consumer.run_dir = None
    consumer.keep_streaming = True
    return consumer


def write_log(root, kind, run_dir, name, text):
    logs = root / kind / run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / name).write_text(text)
    return logs / name


def test_receive_rejects_invalid_json():
    consumer = make_consumer()
    asyncio.run(consumer.receive("not json"))
    assert consumer.sent == [
        {"type": "error", "message": "Invalid log stream request.", "content": ""}
    ]


def test_receive_rejects_unknown_method(dirs):
    consumer = make_consumer()
    asyncio.run(consumer.receive(json.dumps({"run_method": "x", "run_dir": "r"})))
    assert consumer.sent[0]["message"] == "Unknown PEAT run method."


def test_receive_requires_run_dir(dirs):
    consumer = make_consumer()
    asyncio.run(consumer.receive(json.dumps({"run_method": "pull"})))
    assert consumer.sent[0]["message"] == "Run directory is not available."
    assert consumer.parent_run_dir == dirs / "pulls"


def test_receive_rejects_path_escape(dirs):
    consumer = make_consumer()
    asyncio.run(
        consumer.receive(json.dumps({"run_method": "scan", "run_dir": "../../etc"}))
    )
    assert consumer.sent[0]["message"] == "Invalid run directory."
    assert consumer.parent_run_dir == dirs / "scans"


def test_receive_sends_existing_log_then_streams(dirs, monkeypatch):
    write_log(dirs, "scans", "r", "console.log", "hello\n")
    consumer = make_consumer()
    streamed = []

    async def fake_stream():
        streamed.append(consumer.file_offset)

    monkeypatch.setattr(consumer, "stream_log_lines", fake_stream)
    asyncio.run(consumer.receive(json.dumps({"run_method": "scan", "run_dir": "r"})))
    assert consumer.sent == [{"type": "existing_log", "content": "hello\n"}]
    assert streamed == [6]


def test_set_file_path_prefers_console_then_peat_log(dirs):
    consumer = make_consumer()
    assert consumer.set_file_path() is False  # nothing configured
    consumer.parent_run_dir, consumer.run_dir = dirs / "scans", "r"
    assert consumer.set_file_path() is True
    assert consumer.file_path.name == "peat.log"
    write_log(dirs, "scans", "r", "console.log", "x")
    assert consumer.set_file_path() is True
    assert consumer.file_path.name == "console.log"


def test_set_file_path_rejects_escape(dirs):
    consumer = make_consumer()
    consumer.parent_run_dir, consumer.run_dir = dirs / "scans", "../x"
    assert consumer.set_file_path() is False


def test_send_existing_log_content_waits_then_reports(dirs):
    consumer = make_consumer()
    consumer.parent_run_dir, consumer.run_dir = dirs / "scans", "r"
    assert asyncio.run(consumer.send_existing_log_content()) is True
    assert consumer.sent == [
        {
            "type": "status",
            "message": "Log file not found yet; waiting for PEAT output...",
            "content": "",
        }
    ]


def test_stream_log_lines_sends_new_lines_from_offset(dirs):
    write_log(dirs, "scans", "r", "console.log", "old\nnew1\nnew2\n")
    consumer = make_consumer()
    consumer.parent_run_dir, consumer.run_dir = dirs / "scans", "r"
    consumer.file_offset = 4

    async def go():
        task = asyncio.create_task(consumer.stream_log_lines())
        for _ in range(100):
            if consumer.sent:
                break
            await asyncio.sleep(0.005)
        await consumer.disconnect(1000)
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert consumer.sent == [{"type": "new_lines", "content": "new1\nnew2\n"}]


def test_stream_log_lines_returns_when_disconnected_before_file_exists(dirs):
    consumer = make_consumer()
    consumer.parent_run_dir, consumer.run_dir = dirs / "scans", "r"

    async def go():
        task = asyncio.create_task(consumer.stream_log_lines())
        await asyncio.sleep(0.01)
        await consumer.disconnect(1000)
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert consumer.sent == []


def test_read_lines_limits_count(tmp_path):
    path = tmp_path / "f"
    path.write_text("a\nb\nc\n")
    with open(path) as handle:
        assert make_consumer().read_lines(handle, 2) == ["a\n", "b\n"]
        assert make_consumer().read_lines(handle, 5) == ["c\n"]
