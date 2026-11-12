"""Tail Zeek JSON logs and bulk-push them as documents to the datastore."""

import json
import os
import signal
from collections import defaultdict
from dataclasses import dataclass
from os import environ
from pathlib import Path
from threading import Event

from parseus.datastore.client import get_datastore
from parseus.ingestors.gpib.logger import setup_logger
from parseus.ingestors.zeek.normalize import LOGS, doc_id, index_name, normalize

HEAD_BYTES = 64
MAX_READ_BYTES = 8 * 1024 * 1024  # per file per poll, so a backlog never sits in RAM at once
TEMPLATE_NAME = "parseus-zeek"
MAPPINGS = {
    "properties": {
        "@timestamp": {"type": "date"},
        "source": {"properties": {"ip": {"type": "ip"}, "port": {"type": "integer"}}},
        "destination": {
            "properties": {"ip": {"type": "ip"}, "port": {"type": "integer"}}
        },
        "event": {
            "properties": {
                "provider": {"type": "keyword"},
                "dataset": {"type": "keyword"},
            }
        },
        "zeek": {
            "properties": {
                "uid": {"type": "keyword"},
                "modbus": {"properties": {"func": {"type": "keyword"}}},
                "modbus_detailed": {
                    "properties": {
                        "func": {"type": "keyword"},
                        "address": {"type": "long"},
                        "quantity": {"type": "long"},
                        "request_values": {"type": "long"},
                        "response_values": {"type": "long"},
                    }
                },
                "dnp3_points": {
                    "properties": {
                        "function_code": {"type": "keyword"},
                        "object_type": {"type": "keyword"},
                        "group": {"type": "integer"},
                        "variation": {"type": "integer"},
                        "index": {"type": "long"},
                        "value": {"type": "double"},
                        "flags": {"type": "integer"},
                    }
                },
            }
        },
    }
}


@dataclass(frozen=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    """Validated ingestor settings."""

    datastore_host_url: str
    datastore_type: str
    datastore_auth: str | None
    log_dir: Path
    state_file: Path
    index_pattern: str
    poll_seconds: float
    log_file: Path


def load_settings() -> Settings:
    """Load ingestor settings from the environment."""
    artifacts = Path.home() / ".parseus" / "artifacts"
    index_pattern = environ.get(
        "INGESTOR_ZEEK_INDEX", "arkime_sessions3-parseus-zeek-%y%m%d"
    )
    if index_pattern != index_pattern.lower():
        raise ValueError("INGESTOR_ZEEK_INDEX must be lowercase")
    return Settings(
        datastore_host_url=environ.get("DATASTORE_HOST_URL", "http://localhost:9202"),
        datastore_type=environ.get("DATASTORE_TYPE", "elasticsearch"),
        datastore_auth=environ.get("DATASTORE_AUTH"),
        log_dir=Path(environ.get("INGESTOR_ZEEK_LOG_DIR", "/zeek/logs")),
        state_file=Path(
            environ.get(
                "INGESTOR_ZEEK_STATE_FILE", str(artifacts / "logs" / "zeek_state.json")
            )
        ),
        index_pattern=index_pattern,
        poll_seconds=float(environ.get("INGESTOR_ZEEK_POLL_SECONDS", "2")),
        log_file=Path(
            environ.get(
                "INGESTOR_LOG_FILE", str(artifacts / "logs" / "zeek_ingestor.log")
            )
        ),
    )


def read_new_lines(log_dir: Path, name: str, state: dict) -> list[str]:
    """Return the complete lines not yet read from ``name.log`` and its rotations.

    ``state`` maps ``"<name>:<inode>"`` to the bytes already consumed plus the file's
    first bytes. Rotation renames a file but keeps its inode, so a rotated file is
    finished where it left off; a file whose first bytes changed (inode reuse,
    truncation) is read again from the start, which is harmless because documents
    have stable ids.
    """
    lines: list[str] = []
    seen = set()
    paths = [*sorted(log_dir.glob(f"{name}.*.log")), log_dir / f"{name}.log"]
    for path in paths:
        try:
            info = path.stat()
            done = state.get(f"{name}:{info.st_ino}")
            if done and done["offset"] == info.st_size:
                # ponytail: assumes same inode and size means unchanged (inode reuse
                # at an identical size would be missed); skips reopening old rotations.
                seen.add(f"{name}:{info.st_ino}")
                continue
            with path.open("rb") as fh:
                info = os.fstat(fh.fileno())
                key = f"{name}:{info.st_ino}"
                seen.add(key)
                head = fh.read(HEAD_BYTES)
                entry = state.get(key, {"offset": 0, "head": ""})
                if not head.hex().startswith(entry["head"]) or info.st_size < entry["offset"]:
                    entry = {"offset": 0, "head": ""}
                fh.seek(entry["offset"])
                data = fh.read(MAX_READ_BYTES)
        except FileNotFoundError:
            continue
        complete = data[: data.rfind(b"\n") + 1]
        lines.extend(complete.decode("utf-8", errors="replace").splitlines())
        state[key] = {"offset": entry["offset"] + len(complete), "head": head.hex()}
    for key in [k for k in state if k.startswith(f"{name}:") and k not in seen]:
        del state[key]
    return lines


def load_state(path: Path) -> dict:
    """Read the saved offsets; a missing or corrupt file means start from the top."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    """Atomically write the offsets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def to_docs(name: str, lines: list[str], index_pattern: str) -> dict[str, list[dict]]:
    """Group normalized documents by target index; ``_id`` makes pushes idempotent."""
    by_index = defaultdict(list)
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        doc = normalize(name, record)
        if doc is not None:
            by_index[index_name(index_pattern, doc)].append(
                {**doc, "_id": doc_id(name, record)}
            )
    return by_index


def main() -> None:
    """Poll the Zeek log directory until SIGTERM/SIGINT."""
    settings = load_settings()
    logger = setup_logger("zeek_ingestor", settings.log_file)
    datastore = get_datastore(
        datastore_host_url=settings.datastore_host_url,
        datastore_type=settings.datastore_type,
        datastore_auth=settings.datastore_auth,
    )
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    datastore.ensure_index_template(
        TEMPLATE_NAME, [settings.index_pattern.split("%")[0] + "*"], MAPPINGS
    )
    state = load_state(settings.state_file)
    logger.warning("Tailing %s into %s", settings.log_dir, settings.index_pattern)
    while not stop.is_set():
        for name in LOGS:
            # Work on a copy: state only advances once the push succeeded.
            trial = json.loads(json.dumps(state))
            lines = read_new_lines(settings.log_dir, name, trial)
            if not lines:
                state = trial
                continue
            try:
                for index, docs in to_docs(name, lines, settings.index_pattern).items():
                    if not datastore.bulk_push_docs(index, docs):
                        raise RuntimeError(f"bulk push to {index} failed")
            except Exception:  # pylint: disable=broad-exception-caught  # keep running; retried next poll
                logger.exception("Push failed for %s; will retry", name)
                continue
            state = trial
            save_state(settings.state_file, state)
            logger.info("Pushed %d %s record(s)", len(lines), name)
        stop.wait(settings.poll_seconds)
