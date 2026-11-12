import os
import uuid
from datetime import datetime, timezone

import redis
from celery import shared_task
from celery.utils.log import get_task_logger

from parseus.interfaces.common import (
    get_ds_client,
    schedule_run_index,
    schedule_timestamp_range,
    source_has_data,
)
from parseus.interfaces.tasks import run_and_push_analytics
from parseus.webapp.analytics.models import Analytic

logger = get_task_logger(__name__)

# One-time Redis client (share it across workers)
# assumes redis as the broker
redis_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(redis_url, decode_responses=True)

ANALYTIC_CHECKPOINT_SUFFIX = "last_run_end_ts"
ANALYTIC_LOCK_SUFFIX = "run_lock"
DEFAULT_ANALYTIC_LOCK_SECONDS = 3600


def _env_bool(name: str, default: bool = False) -> bool:
    """Return a boolean environment setting using production-friendly values."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _analytic_checkpoint_key(analytic_str: str) -> str:
    """Return the Redis key used for an analytic high-water mark."""
    return f"{analytic_str}:{ANALYTIC_CHECKPOINT_SUFFIX}"


def _analytic_lock_key(analytic_str: str) -> str:
    """Return the Redis key used to prevent overlapping analytic runs."""
    return f"{analytic_str}:{ANALYTIC_LOCK_SUFFIX}"


def _analytic_lock_seconds() -> int:
    """Return the Redis lock TTL for scheduled analytic runs."""
    try:
        return max(1, int(os.getenv("ANALYTICS_RUN_LOCK_SECONDS", "")))
    except ValueError:
        return DEFAULT_ANALYTIC_LOCK_SECONDS


def _backfill_on_first_run() -> bool:
    """
    Return whether a missing checkpoint should backfill historical data.

    The production default is false: the first scheduled invocation initializes a
    high-water mark and skips historical data. This prevents a newly enabled
    scheduled analytic from flooding the alerts index when the source index
    already contains a large history.
    """
    return _env_bool("ANALYTICS_BACKFILL_ON_FIRST_RUN", default=False)


def _parse_checkpoint(value: str | None, analytic_str: str) -> datetime | None:
    """Return a checkpoint datetime, or ``None`` when the value is absent/invalid."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OSError):
        logger.warning(
            "Invalid checkpoint for analytic %s: %r. Reinitializing high-water mark.",
            analytic_str,
            value,
        )
        return None


def _set_checkpoint(analytic_str: str, end: datetime) -> None:
    """Persist an analytic high-water mark in Redis."""
    redis_client.set(_analytic_checkpoint_key(analytic_str), end.timestamp())


def _acquire_analytic_lock(analytic_str: str) -> str | None:
    """Acquire a per-analytic Redis lock and return its token, or ``None``."""
    token = str(uuid.uuid4())
    acquired = redis_client.set(
        _analytic_lock_key(analytic_str),
        token,
        nx=True,
        ex=_analytic_lock_seconds(),
    )
    return token if acquired else None


def _release_analytic_lock(analytic_str: str, token: str) -> None:
    """Release a per-analytic Redis lock if this task still owns it."""
    lock_key = _analytic_lock_key(analytic_str)
    try:
        if redis_client.get(lock_key) == token:
            redis_client.delete(lock_key)
    except Exception:
        logger.exception("Failed to release analytic lock %s.", lock_key)


def _load_analytic_class(analytic_str: str):
    """Load an analytic class by its public analytics package name."""
    module = __import__("parseus.analytics", fromlist=[analytic_str])
    return getattr(module, analytic_str)


def _plan_run_window(analytic, analytic_str):
    """Return the ``(start, end)`` range to run, or ``None`` to skip this run.

    Initializes or repairs the checkpoint (without running) when it is absent
    or invalid, unless first-run backfill is enabled.
    """
    checkpoint_value = redis_client.get(_analytic_checkpoint_key(analytic_str))
    start = _parse_checkpoint(checkpoint_value, analytic_str)

    _, end = schedule_timestamp_range(analytic)

    if checkpoint_value is None and start is None and not _backfill_on_first_run():
        _set_checkpoint(analytic_str, end)
        logger.info(
            "Initialized analytic %s checkpoint at %s; skipping initial backfill.",
            analytic.__name__,
            end,
        )
        return None

    if checkpoint_value is not None and start is None:
        _set_checkpoint(analytic_str, end)
        logger.warning(
            "Reinitialized analytic %s checkpoint at %s after invalid Redis value; "
            "skipping this run to avoid an accidental backfill.",
            analytic.__name__,
            end,
        )
        return None

    if start is not None and end <= start:
        logger.info("Skipping analytic %s: No new data detected.", analytic.__name__)
        return None

    return start, end


@shared_task(queue="analytics")
def analytic_runner(analytic_str):
    "Runs the analytic over data since the last successful run."

    lock_token = _acquire_analytic_lock(analytic_str)
    if lock_token is None:
        logger.info("Skipping analytic %s: another run is already in progress.", analytic_str)
        return

    try:
        analytic = _load_analytic_class(analytic_str)

        # Skip cleanly when the source index is missing or empty. This avoids
        # initializing or advancing a checkpoint from the fabricated
        # epoch/now fallbacks that the datastore returns for absent data, which
        # could otherwise skip genuinely delayed initial ingestion.
        run_index = schedule_run_index(analytic)
        if not source_has_data(get_ds_client(), run_index):
            logger.info(
                "Skipping analytic %s: source index %s is missing or has no data.",
                analytic.__name__,
                run_index,
            )
            return

        window = _plan_run_window(analytic, analytic_str)
        if window is None:
            return
        start, end = window

        # ``Analytic.name`` is a resolved property, not a DB column, so look the
        # row up by the stored fully-qualified class path instead.
        ui_analytic = Analytic.objects.get(
            analytic_class=f"{analytic.__module__}.{analytic.__qualname__}"
        )
        ui_input = ui_analytic.ui.get_analytic_cfg()
        user_input = {
            **ui_input,
            "run_start": start,
            "run_end": end,
        }
        logger.info(
            "Starting analytic %s for range %s to %s...",
            analytic.__name__,
            start,
            end,
        )
        run_and_push_analytics(analytic, user_input=user_input)
        _set_checkpoint(analytic_str, end)
    finally:
        _release_analytic_lock(analytic_str, lock_token)
