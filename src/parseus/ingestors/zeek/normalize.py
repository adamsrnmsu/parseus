"""Pure conversion of Zeek JSON log records into datastore documents.

The documents follow the Malcolm layout Parseus already queries: ``@timestamp``,
``source``/``destination``, ``event.provider``/``event.dataset`` and
``zeek.<log>.<field>``.
"""

from datetime import datetime, timezone
from hashlib import blake2b
from json import dumps
from typing import Any

# Logs ingested, and the fields of each that are not copied under zeek.<log>.
LOGS = ("modbus_detailed", "dnp3_points", "modbus", "dnp3")
_ENVELOPE = ("ts", "uid", "id.orig_h", "id.orig_p", "id.resp_h", "id.resp_p")


def _timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def normalize(log_name: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """Return the datastore document for one Zeek log record, or None to skip it."""
    if log_name not in LOGS:
        return None
    try:
        doc = {
            "@timestamp": _timestamp(float(record["ts"])),
            "source": {
                "ip": record["id.orig_h"],
                "port": int(record["id.orig_p"]),
            },
            "destination": {
                "ip": record["id.resp_h"],
                "port": int(record["id.resp_p"]),
            },
            "event": {"provider": "zeek", "dataset": log_name},
        }
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    fields = {k: v for k, v in record.items() if k not in _ENVELOPE}
    doc["zeek"] = {"uid": record.get("uid"), log_name: fields}
    if log_name == "modbus_detailed":
        # reg_ops.modbus_to_regop reads the function name from zeek.modbus.func.
        doc["zeek"]["modbus"] = {"func": fields.get("func")}
    if log_name == "dnp3" and fields.get("fc_request"):
        # The OT function-code analytic reads Malcolm's zeek.dnp3_objects.function_code.
        doc["zeek"]["dnp3_objects"] = {"function_code": fields["fc_request"]}
    return doc


def doc_id(log_name: str, record: dict[str, Any]) -> str:
    """Stable document id, so re-reading a log never duplicates documents."""
    raw = dumps(record, sort_keys=True, separators=(",", ":"))
    return blake2b(f"{log_name}\0{raw}".encode(), digest_size=16).hexdigest()


def index_name(pattern: str, doc: dict[str, Any]) -> str:
    """Index for a document: ``pattern`` is a strftime format applied to its date."""
    stamp = datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
    return stamp.strftime(pattern)
