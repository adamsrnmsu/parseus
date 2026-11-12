"""Tasks intended for celery execution."""

import logging
import random
import shutil
import threading
import time
from pathlib import Path

from celery import shared_task
from celery.exceptions import Retry
from django.conf import settings
from django.db import connection

from .models import PeatConfig, PeatPull, PeatScan
from .peat_locks import (
    acquire_target_locks,
    build_owner_token_for_run,
    get_lock_ttl_seconds,
    refresh_owner_locks,
    release_owner_locks,
    verify_target_locks,
)
from .peat_process import (
    PeatProcessError,
    build_peat_command,
    current_process_host,
    run_peat_subprocess,
    terminate_process_group,
)

LOGGER = logging.getLogger(__name__)


def get_peat_run_device_types(peat_run_obj):
    """Return selected PEAT modules for a run, defaulting to all modules."""
    return peat_run_obj.modules or ["all"]


class PeatLockHeartbeat:
    """Refresh PEAT target lock TTLs while a subprocess is running."""

    def __init__(self, owner: str):
        self.owner = owner
        self._stop_event = threading.Event()
        self._thread = None
        self.interval_seconds = self._get_interval_seconds()

    def _get_interval_seconds(self) -> int:
        configured_interval = int(getattr(settings, "PEAT_LOCK_HEARTBEAT_SECONDS", 0))
        if configured_interval > 0:
            return configured_interval
        ttl_seconds = get_lock_ttl_seconds()
        return max(1, min(max(ttl_seconds // 3, 1), 5 * 60))

    def __enter__(self):
        missing_locks = refresh_owner_locks(self.owner)
        if missing_locks:
            raise RuntimeError(
                f"Unable to refresh PEAT target locks before launch: {missing_locks}"
            )
        self._thread = threading.Thread(
            target=self._run,
            name=f"peat-lock-heartbeat:{self.owner}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop_event.wait(self.interval_seconds):
            try:
                missing_locks = refresh_owner_locks(self.owner)
            except Exception:
                LOGGER.exception("Failed to refresh PEAT target locks for %s", self.owner)
                return
            if missing_locks:
                LOGGER.warning(
                    "PEAT target lock heartbeat stopped for %s; missing locks: %s",
                    self.owner,
                    missing_locks,
                )
                return


def _release_peat_locks_safely(owner: str):
    """Release Redis PEAT locks without masking task status handling."""
    try:
        release_owner_locks(owner)
    except Exception:
        LOGGER.exception("Failed to release PEAT locks for %s", owner)


def _queue_retry_delay_seconds() -> int:
    """Return retry delay with optional jitter for waiting PEAT runs."""
    retry_seconds = int(getattr(settings, "PEAT_LOCK_QUEUE_RETRY_SECONDS", 30))
    jitter_seconds = int(getattr(settings, "PEAT_LOCK_QUEUE_RETRY_JITTER_SECONDS", 5))
    if jitter_seconds > 0:
        retry_seconds += random.randint(0, jitter_seconds)
    return max(1, retry_seconds)


def _queue_timeout_exceeded(peat_run_obj) -> bool:
    """Return True when a waiting PEAT run has exceeded its queue timeout."""
    timeout_seconds = int(getattr(settings, "PEAT_LOCK_QUEUE_TIMEOUT_SECONDS", 3600))
    if timeout_seconds <= 0:
        return False
    return time.time() - peat_run_obj.created.timestamp() >= timeout_seconds


def _mark_peat_run_error(peat_run_obj, message: str):
    """Mark a PEAT run as errored and append a console message."""
    peat_run_obj.refresh_from_db()
    if peat_run_obj.status == "stopped":
        return
    peat_run_obj.status = "error"
    peat_run_obj.process_id = None
    peat_run_obj.save(update_fields=["status", "process_id"])
    peat_run_obj._append_console_message(message)


def _promote_to_running_or_release(peat_run_obj, owner: str) -> bool:
    """Move a locked run to ``running``; release the locks if it was stopped."""
    updated = peat_run_obj.__class__.objects.filter(
        pk=peat_run_obj.pk,
        status__in=["started", "waiting"],
    ).update(status="running")
    peat_run_obj.refresh_from_db()
    if not updated:
        release_owner_locks(owner)
        return False
    return True


def _acquire_queued_locks_or_retry(
    task,
    peat_run_obj,
    *,
    owner: str,
    targets: list[str],
    target_mode: str,
    queue_if_busy: bool,
) -> bool:
    """Acquire locks inside a task or retry without occupying a worker slot."""
    peat_run_obj.refresh_from_db()
    if peat_run_obj.status == "stopped":
        return False

    lock_acquisition = acquire_target_locks(
        owner=owner,
        targets=targets,
        target_mode=target_mode,
    )
    if lock_acquisition.acquired:
        return _promote_to_running_or_release(peat_run_obj, owner)

    if not queue_if_busy:
        _mark_peat_run_error(
            peat_run_obj,
            "PEAT target locks were busy when the queued task started; aborting run.",
        )
        return False

    if _queue_timeout_exceeded(peat_run_obj):
        _mark_peat_run_error(
            peat_run_obj,
            "Timed out waiting for PEAT target locks; aborting run.",
        )
        return False

    if peat_run_obj.status != "waiting":
        peat_run_obj.status = "waiting"
        peat_run_obj.save(update_fields=["status"])
    if task.request.retries == 0:
        peat_run_obj._append_console_message(
            "Waiting for PEAT target locks: " + ", ".join(lock_acquisition.busy_targets)
        )
    raise task.retry(countdown=_queue_retry_delay_seconds())


def _verify_peat_locks_or_raise(peat_run_obj):
    """Raise RuntimeError if the current task no longer owns its target locks."""
    owner = build_owner_token_for_run(peat_run_obj)
    missing_locks = verify_target_locks(owner)
    if not missing_locks:
        return owner

    message = (
        "PEAT target locks expired or are no longer owned by this task; "
        "aborting run before launch."
    )
    peat_run_obj._append_console_message(message)
    raise RuntimeError(f"{message} Missing locks: {missing_locks}")


def peat_scan_found_no_devices(run_dir: Path) -> bool:
    """Return True when PEAT failed only because a scan found no devices.

    PEAT exits non-zero for some valid negative scan outcomes, such as no hosts
    responding to the lightweight reachability check. The webapp should present
    those as completed scans with zero findings rather than infrastructure
    errors.
    """
    console_log = run_dir / "logs" / "console.log"
    try:
        log_text = console_log.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False

    no_finding_markers = [
        "no hosts are active",
        "no device results, skipping export",
        "scan failed: no results",
        "failed unicast_ip_scan: no hosts are active",
    ]
    fatal_markers = [
        "invalid scan_type",
        "none of the specified hosts are valid",
        "no hosts specified",
        "failed to initialize peat",
    ]
    return any(marker in log_text for marker in no_finding_markers) and not any(
        marker in log_text for marker in fatal_markers
    )


@shared_task(queue="maintenance")
def peat_results_cleanup(result_path):
    if result_path:
        try:
            shutil.rmtree(result_path)
            LOGGER.info("Directory %s successfully removed.", result_path)
        except OSError:
            LOGGER.exception("Error removing directory %s", result_path)
    else:
        LOGGER.info("Directory %s does not exist.", result_path)


def _claim_started_run(peat_run_obj, lock_owner: str) -> bool:
    """Move a ``started`` run to ``running``; release its locks if it moved on."""
    updated = peat_run_obj.__class__.objects.filter(
        pk=peat_run_obj.pk, status="started"
    ).update(status="running")
    peat_run_obj.refresh_from_db()
    if not updated:
        _release_peat_locks_safely(lock_owner)
    return bool(updated)


def _ensure_locks(
    task, peat_run_obj, lock_owner, *, targets, target_mode, queue_if_busy, locks_acquired
) -> bool:
    """Verify (already locked) or acquire (queued) locks; False means stop here."""
    if locks_acquired:
        _verify_peat_locks_or_raise(peat_run_obj)
        return True
    return _acquire_queued_locks_or_retry(
        task,
        peat_run_obj,
        owner=lock_owner,
        targets=targets,
        target_mode=target_mode,
        queue_if_busy=queue_if_busy,
    )


def _finish_run(peat_run_obj, status: str, failure_message: str | None = None) -> bool:
    """Record a terminal ``status`` unless the user stopped the run; True if recorded."""
    peat_run_obj.refresh_from_db()
    if peat_run_obj.status == "stopped":
        return False
    if failure_message:
        LOGGER.exception(failure_message)
    peat_run_obj.status = status
    peat_run_obj.process_id = None
    peat_run_obj.save(update_fields=["status", "process_id"])
    return True


def _finish_scan_after_process_error(peat_scan_obj):
    """Treat a no-devices PEAT exit as a completed scan, anything else as an error."""
    peat_scan_obj.refresh_from_db()
    run_dir = settings.PEAT_SCAN_DIR / peat_scan_obj.run_dir
    if peat_scan_obj.status != "stopped" and peat_scan_found_no_devices(run_dir):
        LOGGER.info("PEAT scan completed with no identified devices")
        if _finish_run(peat_scan_obj, "done"):
            peat_scan_obj._append_console_message(
                "No supported devices were identified; marking scan complete."
            )
    else:
        _finish_run(peat_scan_obj, "error", "PEAT scan failed")


def _scan_config(config_pk, intensive_scan: bool):
    """Return ``(config_file, intensive_scan)``; a stored config overrides the flag."""
    if not config_pk:
        return None, intensive_scan
    peat_config = PeatConfig.objects.get(pk=config_pk)
    return peat_config.filename.path, peat_config.intensive_scan


@shared_task(bind=True, queue="peat")
def peat_pull(
    self,
    peat_pull_pk,
    device_list,
    database_url,
    target_mode="ip",
    queue_if_busy=False,
    locks_acquired=True,
):
    """Run a PEAT pull subprocess for ``peat_pull_obj``, managing its target locks."""
    peat_pull_obj = PeatPull.objects.get(pk=peat_pull_pk)
    lock_owner = build_owner_token_for_run(peat_pull_obj)
    if locks_acquired and not _claim_started_run(peat_pull_obj, lock_owner):
        return True
    try:
        if not _ensure_locks(
            self,
            peat_pull_obj,
            lock_owner,
            targets=device_list,
            target_mode=target_mode,
            queue_if_busy=queue_if_busy,
            locks_acquired=locks_acquired,
        ):
            return True
        if not peat_pull_obj.run_dir:
            raise ValueError("PEAT pull run_dir must be set before queueing the task")
        peat_config = peat_pull_obj.config
        with PeatLockHeartbeat(lock_owner):
            run_peat_job(
                peat_pull_obj,
                method="pull",
                targets=device_list,
                target_mode=target_mode,
                run_dir=settings.PEAT_PULL_DIR / peat_pull_obj.run_dir,
                database_url=database_url,
                config_file=peat_config.filename.path if peat_config else None,
            )
    except Retry:
        raise
    except Exception:
        _finish_run(peat_pull_obj, "error", "PEAT pull failed")
    else:
        _finish_run(peat_pull_obj, "done")
    finally:
        _release_peat_locks_safely(lock_owner)
    return True


@shared_task(bind=True, queue="peat")
def peat_scan(
    self,
    peat_scan_primary_key,
    cidr,
    database_url,
    config_pk=None,
    intensive_scan=False,
    target_mode="ip",
    queue_if_busy=False,
    locks_acquired=True,
):
    """Run a PEAT scan subprocess for ``peat_scan_obj``, managing its target locks."""
    peat_scan_obj = PeatScan.objects.get(pk=peat_scan_primary_key)
    lock_owner = build_owner_token_for_run(peat_scan_obj)
    if locks_acquired and not _claim_started_run(peat_scan_obj, lock_owner):
        return True
    try:
        if not _ensure_locks(
            self,
            peat_scan_obj,
            lock_owner,
            targets=cidr,
            target_mode=target_mode,
            queue_if_busy=queue_if_busy,
            locks_acquired=locks_acquired,
        ):
            return True
        if not peat_scan_obj.run_dir:
            raise ValueError("PEAT scan run_dir must be set before queueing the task")
        config_file, intensive_scan = _scan_config(config_pk, intensive_scan)
        with PeatLockHeartbeat(lock_owner):
            run_peat_job(
                peat_scan_obj,
                method="scan",
                targets=cidr,
                target_mode=target_mode,
                run_dir=settings.PEAT_SCAN_DIR / peat_scan_obj.run_dir,
                database_url=database_url,
                config_file=config_file,
                intensive_scan=intensive_scan,
            )
    except Retry:
        raise
    except PeatProcessError:
        _finish_scan_after_process_error(peat_scan_obj)
    except Exception:
        _finish_run(peat_scan_obj, "error", "PEAT scan failed")
    else:
        _finish_run(peat_scan_obj, "done")
    finally:
        _release_peat_locks_safely(lock_owner)
    return True


def run_peat_job(
    peat_run_obj,
    *,
    method: str,
    targets: list[str],
    run_dir: Path,
    database_url: str | None,
    config_file: str | None = None,
    intensive_scan: bool = False,
    target_mode: str = "ip",
):
    """Run a PEAT scan or pull in an isolated subprocess."""
    peat_run_obj.refresh_from_db()
    if peat_run_obj.status == "stopped":
        return

    command = build_peat_command(
        method,
        targets,
        run_dir,
        config_file=config_file,
        elastic_server=database_url,
        intensive_scan=intensive_scan,
        target_mode=target_mode,
        device_types=get_peat_run_device_types(peat_run_obj),
    )

    def save_process_id(pid: int):
        peat_run_obj.refresh_from_db()
        if peat_run_obj.status == "stopped":
            terminate_process_group(pid)
            return
        peat_run_obj.process_id = pid
        peat_run_obj.process_host = current_process_host()
        peat_run_obj.save(update_fields=["process_id", "process_host"])

    def stop_requested() -> bool:
        # Runs in a watcher thread, which needs its own DB connection.
        try:
            return peat_run_obj.__class__.objects.filter(
                pk=peat_run_obj.pk, status="stopped"
            ).exists()
        finally:
            connection.close()

    returncode = run_peat_subprocess(
        command, run_dir, on_start=save_process_id, should_stop=stop_requested
    )
    peat_run_obj.refresh_from_db()
    if peat_run_obj.status == "stopped":
        peat_run_obj.process_id = None
        peat_run_obj.process_host = None
        peat_run_obj.save(update_fields=["process_id", "process_host"])
        return
    if returncode != 0:
        raise PeatProcessError(returncode)
