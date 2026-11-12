"""Process-isolated runner for PEAT scan and pull jobs.

PEAT stores configuration and runtime data in module-level singletons. Running the
PEAT CLI in a fresh Python subprocess gives each Parseus PEAT job process-level
isolation while still allowing this app to stream output and cancel work.
"""

from __future__ import annotations

import functools
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class PeatProcessError(RuntimeError):
    """Raised when a PEAT subprocess exits unsuccessfully."""

    def __init__(self, returncode: int):
        super().__init__(f"PEAT subprocess exited with return code {returncode}")
        self.returncode = returncode


def _peat_cli_prefix() -> list[str]:
    """Return the preferred command prefix for invoking the PEAT CLI."""
    peat_executable = shutil.which("peat")
    if peat_executable:
        return [peat_executable]
    return [sys.executable, "-u", "-m", "peat"]


def _clean_strings(values: Iterable[str]) -> list[str]:
    """Return stripped string forms of ``values`` with blanks dropped."""
    return [str(value).strip() for value in values if str(value).strip()]


def _optional_flags(config_file, elastic_server, method, intensive_scan) -> list[str]:
    """Return the optional PEAT CLI flags in their fixed order."""
    flags = []
    if config_file:
        flags.extend(["-c", str(config_file)])
    if elastic_server:
        flags.extend(["-e", str(elastic_server)])
    if method == "scan" and intensive_scan:
        flags.append("--intensive-scan")
    return flags


def build_peat_command(
    method: str,
    targets: Iterable[str],
    run_dir: Path,
    *,
    config_file: str | Path | None = None,
    elastic_server: str | None = None,
    intensive_scan: bool = False,
    target_mode: str = "ip",
    device_types: Iterable[str] | None = None,
) -> list[str]:
    """Build a safe argv list for the PEAT CLI."""
    if method not in {"scan", "pull"}:
        raise ValueError(f"Unsupported PEAT method: {method}")
    if target_mode not in {"ip", "serial"}:
        raise ValueError(f"Unsupported PEAT target mode: {target_mode}")

    target_list = _clean_strings(targets)
    if not target_list:
        raise ValueError("At least one PEAT target is required")

    selected_device_types = _clean_strings(device_types or [])
    if not selected_device_types or any(
        device_type.lower() == "all" for device_type in selected_device_types
    ):
        selected_device_types = ["all"]

    command = [
        *_peat_cli_prefix(),
        method,
        "--no-color",
        "-d",
        *selected_device_types,
        "--run-dir",
        str(run_dir),
    ]

    command.extend(_optional_flags(config_file, elastic_server, method, intensive_scan))

    target_flag = "-s" if target_mode == "serial" else "-i"
    command.extend([target_flag, *target_list])
    return command


def _render_command_for_log(command: list[str]) -> str:
    """Render argv for logs without exposing datastore credentials."""
    redacted = command.copy()
    for index, arg in enumerate(redacted[:-1]):
        if arg in {"-e", "--elastic-server"}:
            redacted[index + 1] = "<redacted>"
    return shlex.join(redacted)


def _process_exists(pid: int) -> bool:
    """Return whether a process ID exists and is signalable."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(pid: int, grace_seconds: float = 8.0) -> None:
    """Terminate a PEAT subprocess and any children it spawned on Linux."""
    if not pid:
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return
        time.sleep(0.2)

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except OSError:
        return None


@functools.lru_cache(maxsize=1)
def current_process_host() -> str:
    """Return a UUID identifying the PID namespace this process runs in.

    PIDs are only meaningful inside the PID namespace that spawned them, so the
    owner records this next to the PID and any other namespace (e.g. another
    container) refuses to signal it. Hostnames are not used because containers
    can share or override them.

    The UUID is derived (uuid5) from the kernel's PID-namespace identity plus
    the boot ID, so every process in the same namespace (all processes when
    running directly on the host) gets the same value, while separate
    containers, recreated containers, and reboots each get a new one. If the
    namespace cannot be read (non-Linux, restricted /proc), fall back to a
    random per-process UUID: that never matches another process, so a stop
    request is always delegated to the owning worker instead of signalling a
    foreign PID.
    """
    try:
        pid_namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        return str(uuid.uuid4())
    boot_id = _read_text("/proc/sys/kernel/random/boot_id") or ""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{boot_id}:{pid_namespace}"))


def _watch_for_stop(
    process: subprocess.Popen,
    should_stop: Callable[[], bool],
    finished: threading.Event,
    poll_seconds: float,
) -> None:
    """Terminate ``process`` from its own container once ``should_stop`` is true."""
    while not finished.wait(poll_seconds):
        try:
            stop_requested = should_stop()
        except Exception:
            LOGGER.exception("Failed to check PEAT stop request")
            continue
        if stop_requested:
            LOGGER.info("Stop requested; terminating PEAT process %s", process.pid)
            try:
                terminate_process_group(process.pid)
            except OSError:
                LOGGER.exception("Failed to terminate PEAT process %s", process.pid)
            return


def run_peat_subprocess(
    command: list[str],
    run_dir: Path,
    *,
    on_start: Callable[[int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    stop_poll_seconds: float = 2.0,
) -> int:
    """Run PEAT, streaming combined stdout/stderr to logs/console.log.

    Returns the PEAT process return code. ``on_start`` is called with the PID
    immediately after process creation so callers can persist it for cancellation.
    ``should_stop`` is polled in a background thread; when it returns True the
    subprocess group is terminated by this (owning) process, which is how a stop
    requested from another container (e.g. the web app) takes effect.
    """
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    console_log = log_dir / "console.log"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    rendered_command = _render_command_for_log(command)
    LOGGER.info("Starting PEAT subprocess: %s", rendered_command)
    with console_log.open("a", encoding="utf-8", buffering=1) as log_file:
        log_file.write(f"$ {rendered_command}\n")
        log_file.flush()

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                start_new_session=True,
            )
        except OSError as error:
            log_file.write(f"Failed to start PEAT subprocess: {error}\n")
            log_file.flush()
            raise

        if on_start:
            try:
                on_start(process.pid)
            except Exception:
                LOGGER.exception("Failed to persist PEAT subprocess PID")
                log_file.write("Failed to persist PEAT subprocess PID; terminating.\n")
                log_file.flush()
                terminate_process_group(process.pid)
                process.wait()
                raise

        finished = threading.Event()
        watcher = None
        if should_stop:
            watcher = threading.Thread(
                target=_watch_for_stop,
                args=(process, should_stop, finished, stop_poll_seconds),
                name=f"peat-stop-watch:{process.pid}",
                daemon=True,
            )
            watcher.start()

        try:
            assert process.stdout is not None
            for line in process.stdout:
                log_file.write(line)
                log_file.flush()

            returncode = process.wait()
        finally:
            finished.set()
            if watcher:
                watcher.join(timeout=10)
        log_file.write(f"\nPEAT process exited with return code {returncode}\n")
        log_file.flush()
        return returncode
