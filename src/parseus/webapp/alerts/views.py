import asyncio
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import redis
from asgiref.sync import sync_to_async
from django.contrib import messages
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import loader
from django.views.decorators.http import require_http_methods

from parseus.webapp.alerts.forms import SysmonRuleForm
from parseus.webapp.alerts.models import SysmonRule
from parseus.webapp.project.views import _get_database_client

ALERT_INDEX_PATTERN = "alerts-*"
INGESTOR_HEALTH_REDIS_PREFIX = os.getenv(
    "INGESTOR_HEALTH_REDIS_PREFIX", "health:ingestor"
)
REDIS_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
DISK_HEALTH_PATH = os.getenv("DISK_HEALTH_PATH", "/")
DISK_WARNING_PERCENT = float(os.getenv("DISK_WARNING_PERCENT", "80"))
DISK_CRITICAL_PERCENT = float(os.getenv("DISK_CRITICAL_PERCENT", "90"))
ALERT_STREAM_POLL_SECONDS = float(os.getenv("ALERT_STREAM_POLL_SECONDS", "5"))
ALERT_STREAM_HEARTBEAT_SECONDS = float(
    os.getenv("ALERT_STREAM_HEARTBEAT_SECONDS", "30")
)
ALERT_TYPE_ANALYTICS = {
    "scpi_range": ["ScpiValueRangesAnalytic", "ScpiCompareAnalytic"],
    "device": [
        "SysmonUnapprovedDeviceAnalytic",
        "SysmonUnapprovedIpAnalytic",
        "SysmonUnapprovedUserAnalytic",
    ],
}
ALERT_HANDLE_TYPES = {"dismissed", "flagged"}
# Operator-facing section names for user-visible messages.
ALERT_SECTION_LABELS = {"scpi_range": "SCPI", "device": "Sysmon"}
MAX_ALERT_HANDLE_BATCH = int(os.getenv("MAX_ALERT_HANDLE_BATCH", "500"))
# Maximum alerts fetched and rendered per column on each refresh. The alerts
# view previously scrolled the *entire* unhandled backlog across all daily
# ``alerts-*`` indices on every poll (every ALERT_STREAM_POLL_SECONDS, per
# connected operator, for both columns), which degrades steadily as the backlog
# grows. An operator needs the newest detections and an accurate total, not
# thousands of DOM rows -- so the fetch is bounded and the true match count is
# reported alongside so the UI can show "showing N of M".
ALERT_QUERY_MAX_RESULTS = int(os.getenv("ALERT_QUERY_MAX_RESULTS", "500"))

# Retries for alert-update version conflicts. Our update_by_query calls select
# documents by event.uuid (content), not by a specific _version, and the
# script always sets the same target fields regardless of who "wins" a race
# (handled=<status>, acknowledge.reason=<handle_type>) -- so the mutation is
# idempotent and a version conflict is purely a timing artifact between two
# concurrent writers (e.g. two operator tabs acting on overlapping alerts).
# Re-issuing the identical call is always safe and the conflict typically
# clears within milliseconds once the other writer's request finishes.
# Retrying here, instead of only detecting-and-reporting, turns "two
# operators clicked at the same moment" from an operator-facing error into a
# non-event, and only surfaces an error for the genuinely rare case where
# conflicts persist across every attempt.
ALERT_UPDATE_MAX_ATTEMPTS = int(os.getenv("ALERT_UPDATE_MAX_ATTEMPTS", "5"))
ALERT_UPDATE_RETRY_BACKOFF_SECONDS = float(
    os.getenv("ALERT_UPDATE_RETRY_BACKOFF_SECONDS", "0.1")
)

logger = logging.getLogger(__name__)


class AlertUpdateError(Exception):
    """Raised when an update_by_query against the alerts index reports failures.

    Elasticsearch/OpenSearch's ``_update_by_query`` returns HTTP 200 even when
    some (or all) matching documents could not be updated -- failures (script
    errors, version conflicts under the default ``conflicts="abort"``) show up
    in the response body's ``failures``/``version_conflicts`` fields, not as a
    raised exception or non-2xx status. Treating a 200 response as unconditional
    success silently drops updates: a "handled" alert can keep its prior
    ``handled: false`` state and reappear to the operator on the next refresh
    even though the UI reported success.
    """


def _update_by_query_with_conflict_retry(
    es,
    *,
    index: str,
    query: dict,
    script: dict,
    action: str,
    refresh: bool = True,
) -> dict:
    """Run an idempotent update_by_query, retrying transient version conflicts.

    Only version conflicts are retried: they are a timing artifact of
    concurrent writers racing the *same* idempotent mutation (same target
    field values regardless of who wins), so re-issuing the identical query
    is always safe and the conflict typically clears within milliseconds. A
    non-empty ``failures`` list (script/mapping errors) is a real failure that
    retrying the same request will not fix, so it is raised immediately
    without burning the retry budget.

    :param es: The datastore client.
    :param index: The index pattern to update.
    :param query: The selection query (by content, e.g. event.uuid -- not by
                 document ``_version``, which is what makes retrying safe).
    :param script: The update script/params.
    :param action: A short human-readable description of the attempted action,
                   used in the raised error message and retry logs.
    :param refresh: Whether to make the update visible immediately.
    :return: The final ``update_by_query`` response.
    :raises AlertUpdateError: When the update still has unresolved failures or
        conflicts after :data:`ALERT_UPDATE_MAX_ATTEMPTS` attempts, or when any
        attempt reports a non-conflict failure.
    """
    response: dict = {}
    for attempt in range(1, ALERT_UPDATE_MAX_ATTEMPTS + 1):
        response = es.update_by_query(
            index=index,
            body={"query": query, "script": script},
            # Don't let one version-conflicted document (e.g. a second tab
            # racing this same request) abort updates to the rest of the
            # batch; conflicts are still reported in the response and handled
            # by the retry/raise logic below.
            conflicts="proceed",
            refresh=refresh,
        )

        failures = response.get("failures") or []
        if failures:
            # Not a conflict -- a script/mapping error. Retrying the identical
            # request will not help, so fail fast instead of retrying.
            break

        version_conflicts = response.get("version_conflicts") or 0
        if not version_conflicts:
            return response

        if attempt < ALERT_UPDATE_MAX_ATTEMPTS:
            logger.info(
                "Retrying %s after %d version conflict(s) (attempt %d/%d).",
                action,
                version_conflicts,
                attempt,
                ALERT_UPDATE_MAX_ATTEMPTS,
            )
            time.sleep(ALERT_UPDATE_RETRY_BACKOFF_SECONDS * attempt)

    _raise_on_update_by_query_failures(response, action=action)
    return response


def _raise_on_update_by_query_failures(response: dict, *, action: str) -> None:
    """Raise :class:`AlertUpdateError` when an update_by_query response reports failures.

    :param response: The raw ``_update_by_query`` response body.
    :param action: A short human-readable description of the attempted action,
                   used in the raised error message.
    """
    failures = response.get("failures") or []
    version_conflicts = response.get("version_conflicts") or 0
    if failures or version_conflicts:
        failure_detail = "; ".join(
            str(failure.get("cause", failure)) for failure in failures[:5]
        )
        raise AlertUpdateError(
            f"Failed to {action} for some alerts "
            f"({version_conflicts} version conflict(s)"
            + (f", failures: {failure_detail}" if failure_detail else "")
            + "). No changes were made for the affected alert(s); please retry."
        )



def alerts_home(request):
    """Alert home view"""
    template = loader.get_template("alerts/index.html")
    return HttpResponse(template.render(request=request))


def sysmon_rules(request):
    """List UI-managed Sysmon analytic rules."""
    rules = SysmonRule.objects.all()
    return render(request, "alerts/sysmon_rules.html", {"rules": rules})


def sysmon_rule_create(request):
    """Create a UI-managed Sysmon analytic rule."""
    if request.method == "POST":
        form = SysmonRuleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Sysmon rule created.")
            return redirect("alerts:sysmon_rules")
    else:
        form = SysmonRuleForm()

    return render(
        request,
        "alerts/sysmon_rule_form.html",
        {"form": form, "title": "Create Sysmon Rule"},
    )


def sysmon_rule_update(request, pk):
    """Update a UI-managed Sysmon analytic rule."""
    rule = get_object_or_404(SysmonRule, pk=pk)
    if request.method == "POST":
        form = SysmonRuleForm(request.POST, instance=rule)
        if form.is_valid():
            form.save()
            messages.success(request, "Sysmon rule updated.")
            return redirect("alerts:sysmon_rules")
    else:
        form = SysmonRuleForm(instance=rule)

    return render(
        request,
        "alerts/sysmon_rule_form.html",
        {"form": form, "title": "Edit Sysmon Rule"},
    )


@require_http_methods(["POST"])
def sysmon_rule_delete(request, pk):
    """Delete a UI-managed Sysmon analytic rule."""
    rule = get_object_or_404(SysmonRule, pk=pk)
    rule.delete()
    messages.success(request, "Sysmon rule deleted.")
    return redirect("alerts:sysmon_rules")


def _json_body(request) -> dict[str, object]:
    """Parse a JSON request body and return an object payload."""
    if not request.body:
        return {}

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError as err:
        raise ValueError("Invalid JSON request body") from err

    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object")

    return payload


def _build_alert_filter(alert_type: str) -> list[dict[str, object]]:
    """Build the datastore filter for a supported alert type."""
    analytics = ALERT_TYPE_ANALYTICS.get(alert_type)
    if not analytics:
        raise ValueError("Invalid alert type")

    return [
        {"term": {"handled": False}},
        {
            "bool": {
                # Exact keyword match on the analytic class name. ``agent.type``
                # is set to the analytic's class name (e.g.
                # "ScpiValueRangesAnalytic") at write time, so this must be an
                # exact term on the ``.keyword`` subfield -- an analyzed ``match``
                # depends on analyzer behaviour and can over- or under-match if a
                # future class name contains an analyzer token boundary, or if the
                # field is mapped differently across daily indices.
                "should": [
                    {"term": {"agent.type.keyword": analytic}} for analytic in analytics
                ],
                "minimum_should_match": 1,
            }
        },
    ]


def _alert_sort() -> list[dict[str, object]]:
    """Return the sort used for alert queries.

    Unacknowledged alerts (``acknowledge.reason == 'none'``) sort before
    acknowledged/flagged ones, then newest first. ``unmapped_type`` keeps the
    script-backed sort from failing shards on a freshly created daily index that
    has not yet mapped ``acknowledge.reason.keyword`` -- without it, a brand-new
    ``alerts-*`` index can make the whole multi-index search fail a shard and
    (combined with the datastore read path now surfacing shard failures) turn a
    valid query into an error for the operator.
    """
    return [
        {
            "_script": {
                "type": "number",
                "script": {
                    "source": (
                        "if (!doc.containsKey('acknowledge.reason.keyword')) { return 0; } "
                        "if (doc['acknowledge.reason.keyword'].size() == 0) { return 0; } "
                        "return doc['acknowledge.reason.keyword'].value == 'none' ? 1 : 0;"
                    )
                },
                "order": "desc",
            }
        },
        {"@timestamp": {"order": "desc", "unmapped_type": "date"}},
    ]


def _close_datastore_client(client) -> None:
    """Release a datastore client's connection pool. Never raises.

    Closing is housekeeping: a failure to close (already closed, transport
    gone) must never mask the query result or the original error the caller is
    already handling.
    """
    try:
        client.close()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("Ignoring error while closing datastore client", exc_info=True)


@contextmanager
def _datastore_client(request):
    """Yield a datastore client for one request and always close it afterwards.

    ``_get_database_client`` builds a brand-new client (with its own connection
    pool) on every call. Without an explicit close, each request leaves that pool
    for the garbage collector, which is not a resource-management strategy for a
    process expected to run for months.
    """
    client = _get_database_client(request)
    try:
        yield client
    finally:
        _close_datastore_client(client)


class _AlertSnapshotSource:
    """Owns the one datastore client an alert stream uses for its whole life.

    The stream polls every ``ALERT_STREAM_POLL_SECONDS``. Building a client per
    poll (the previous behaviour) meant a fresh connection pool, and so a fresh
    TCP/TLS handshake, per column per poll, for as long as the page was open.
    This builds the client lazily on first use and reuses it so keep-alive works.

    A client that raised is discarded and rebuilt on the next poll rather than
    reused: after a datastore restart or network fault its pool may hold dead
    connections, and rebuilding is cheap next to a 5 second poll interval.

    Lifecycle under concurrency. Under ASGI a poll runs in a worker thread, and a
    browser disconnect cancels the stream *without* stopping that thread, so
    :meth:`close` routinely arrives while :meth:`fetch` is still mid-query.
    Closing the client at that moment breaks the running query ("Pool is closed")
    and makes the datastore transport retry it and mark the node dead. So
    :meth:`close` never closes a client that a fetch is using: it marks the
    source closed and the *last party out* (the finishing fetch, or ``close``
    itself when nothing is running) releases the client, exactly once.

    The stream loop awaits each poll before starting the next, so at most one
    fetch runs at a time; that is what makes a single client safe to share.

    Once closed, :meth:`fetch` refuses to build a client, so a fetch racing a
    disconnect cannot leak a new pool.
    """

    def __init__(self, request):
        self._request = request
        self._client = None
        self._closed = False
        self._in_flight = 0
        self._lock = threading.Lock()

    def fetch(self) -> dict[str, object]:
        """Return a fresh alert snapshot, discarding the client if the query fails."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Alert snapshot source is closed")
            if self._client is None:
                self._client = _get_database_client(self._request)
            client = self._client
            self._in_flight += 1

        failed = True
        try:
            snapshot = _get_alert_snapshot(client)
            failed = False
            return snapshot
        finally:
            self._finish_fetch(client, failed=failed)

    def _finish_fetch(self, client, *, failed: bool) -> None:
        """Account for a finished fetch and release the client if it is now unwanted."""
        with self._lock:
            self._in_flight -= 1
            retire = self._client is client and (
                failed or (self._closed and self._in_flight == 0)
            )
            if retire:
                self._client = None
        if retire:
            _close_datastore_client(client)

    def close(self) -> None:
        """Mark the source closed; release the client now, or when the running fetch ends."""
        with self._lock:
            self._closed = True
            if self._in_flight:
                return  # the running fetch releases the client when it finishes
            client, self._client = self._client, None
        if client is not None:
            _close_datastore_client(client)


def _query_alerts(
    client, filter: list[dict[str, object]]
) -> tuple[list[dict[str, object]], int]:
    """Query the datastore for alerts matching the filter.

    Returns ``(alerts, total)`` where ``alerts`` is at most
    :data:`ALERT_QUERY_MAX_RESULTS` newest-first documents and ``total`` is the
    full number of matching alerts, so the UI can show "showing N of M".

    :param client: The datastore client to query. The caller owns its lifecycle.
    :param filter: The ``bool.filter`` clauses selecting the alerts.
    """
    query = {"bool": {"filter": filter}}
    documents, total = client.search_top(
        index=ALERT_INDEX_PATTERN,
        query=query,
        sort=_alert_sort(),
        size=ALERT_QUERY_MAX_RESULTS,
    )
    return documents, total


def _get_alert_snapshot(client) -> dict[str, object]:
    """Return all alert columns in the shape expected by the alerts UI.

    :param client: The datastore client to query. The caller owns its lifecycle.
    """
    alerts: dict[str, object] = {}
    totals: dict[str, object] = {}
    for alert_type in ALERT_TYPE_ANALYTICS:
        column_alerts, column_total = _query_alerts(
            client, _build_alert_filter(alert_type)
        )
        alerts[alert_type] = column_alerts
        totals[alert_type] = column_total
    return {
        "alerts": alerts,
        "totals": totals,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _format_sse(event: str, data: dict[str, object]) -> str:
    """Format a Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _format_heartbeat() -> str:
    """Format the liveness frame sent while alert data is unchanged.

    This is a *named* event, not an SSE comment line, on purpose: comments are
    invisible to the browser's ``EventSource`` API, so the page could not tell
    "nothing changed" from "the connection silently died" (a proxy or NAT
    dropping an idle TCP connection raises no ``error`` event). Carrying the
    interval lets the client size its stall watchdog from the server's actual
    configuration instead of a hard-coded copy of it.
    """
    return _format_sse(
        "heartbeat",
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "interval_seconds": ALERT_STREAM_HEARTBEAT_SECONDS,
        },
    )


def alert_query(request):
    """Alert Query function. Kept as a fallback/manual refresh endpoint."""
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        body = _json_body(request)
        alert_type = body.get("alert_type")
        alert_filter = _build_alert_filter(alert_type)
        with _datastore_client(request) as client:
            alerts, total = _query_alerts(client, alert_filter)
        return JsonResponse({"alerts": alerts, "total": total})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def _is_asgi_request(request) -> bool:
    """Return whether the current request is being served by ASGI."""
    return request.__class__.__name__ == "ASGIRequest"


async def _async_alert_event_stream(request):
    """Yield alert Server-Sent Events for ASGI servers such as Daphne."""
    last_payload = None
    last_heartbeat = time.monotonic()
    source = _AlertSnapshotSource(request)

    try:
        # Flush headers immediately so proxies/browsers mark the SSE request open
        # before the first datastore query completes.
        yield ": connected\n\n"

        while True:
            try:
                snapshot = await sync_to_async(
                    source.fetch,
                    thread_sensitive=True,
                )()
                payload = json.dumps(snapshot["alerts"], sort_keys=True, default=str)

                if payload != last_payload:
                    last_payload = payload
                    yield _format_sse("alert_snapshot", snapshot)
                    last_heartbeat = time.monotonic()
                elif time.monotonic() - last_heartbeat >= ALERT_STREAM_HEARTBEAT_SECONDS:
                    yield _format_heartbeat()
                    last_heartbeat = time.monotonic()
            except Exception as e:  # pylint: disable=broad-exception-caught
                # ``Exception`` deliberately excludes ``asyncio.CancelledError``
                # and ``GeneratorExit`` (both ``BaseException``): swallowing
                # cancellation would leave this task running after the client
                # disconnected. They propagate to the ``finally`` below.
                logger.warning("Alert stream snapshot failed: %s", e)
                yield _format_sse(
                    "alert_error",
                    {
                        "error": str(e),
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

            await asyncio.sleep(ALERT_STREAM_POLL_SECONDS)
    finally:
        source.close()


def _sync_alert_event_stream(request):
    """Yield alert Server-Sent Events for WSGI servers such as Django runserver."""
    last_payload = None
    last_heartbeat = time.monotonic()
    source = _AlertSnapshotSource(request)

    try:
        # Django's WSGI development server cannot stream an async iterator. If it is
        # given the async generator used by Daphne, it tries to consume the entire
        # iterator before responding, which leaves the alerts page stuck on
        # "Loading alerts...". Use a normal generator for runserver/WSGI instead.
        yield ": connected\n\n"

        while True:
            try:
                snapshot = source.fetch()
                payload = json.dumps(snapshot["alerts"], sort_keys=True, default=str)

                if payload != last_payload:
                    last_payload = payload
                    yield _format_sse("alert_snapshot", snapshot)
                    last_heartbeat = time.monotonic()
                elif time.monotonic() - last_heartbeat >= ALERT_STREAM_HEARTBEAT_SECONDS:
                    yield _format_heartbeat()
                    last_heartbeat = time.monotonic()
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.warning("Alert stream snapshot failed: %s", e)
                yield _format_sse(
                    "alert_error",
                    {
                        "error": str(e),
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

            time.sleep(ALERT_STREAM_POLL_SECONDS)
    finally:
        source.close()


def alert_stream(request):
    """Stream alert snapshots to the browser using Server-Sent Events."""
    event_stream = (
        _async_alert_event_stream(request)
        if _is_asgi_request(request)
        else _sync_alert_event_stream(request)
    )
    response = StreamingHttpResponse(event_stream, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _alert_update_script(*, handled: bool, reason: str) -> dict[str, object]:
    """Build the update script that records how an alert was handled.

    Shared by the selected-alert and dismiss-all views so every dismiss path
    stores the same ``handled`` / ``acknowledge.reason`` / ``acknowledge.timestamp``
    values, regardless of which button the operator used.

    :param handled: Value for the alert's ``handled`` field (False keeps it active).
    :param reason: Value for ``acknowledge.reason`` (``dismissed`` or ``flagged``).
    :return: A script body for ``update_by_query``.
    """
    return {
        "source": """
            ctx._source.handled = params.status;
            if (ctx._source.acknowledge == null) {
                ctx._source.acknowledge = new HashMap();
            }
            ctx._source.acknowledge.timestamp = params.timestamp;
            ctx._source.acknowledge.reason = params.reason;
        """,
        "params": {
            "status": handled,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        },
    }


def _validate_handle_body(
    body: dict[str, object],
) -> tuple[list[str], JsonResponse | None]:
    """Return ``(deduped uuids, None)`` or ``([], error response)`` for a handle request."""
    uuids = body.get("uuids", [])

    if body.get("handle_type") not in ALERT_HANDLE_TYPES:
        return [], JsonResponse({"error": "Invalid alert handling type"}, status=400)

    if not isinstance(uuids, list) or not all(isinstance(uuid, str) for uuid in uuids):
        return [], JsonResponse(
            {"error": "Alert UUIDs must be a list of strings"}, status=400
        )

    uuids = list(dict.fromkeys(uuid for uuid in uuids if uuid))
    if len(uuids) > MAX_ALERT_HANDLE_BATCH:
        return [], JsonResponse(
            {"error": f"Too many alerts selected. Maximum is {MAX_ALERT_HANDLE_BATCH}."},
            status=400,
        )
    return uuids, None


def handle_alerts(request):
    """Dismiss or flag only the alerts the operator selected.

    ``dismissed`` marks the alerts handled so they leave the active list;
    ``flagged`` leaves them active but highlighted for follow-up.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        body = _json_body(request)
        handle_type = body.get("handle_type")
        uuids, error = _validate_handle_body(body)
        if error:
            return error
        if not uuids:
            return JsonResponse({"message": "No alerts selected for handling."})

        update_script = _alert_update_script(
            handled=handle_type != "flagged", reason=handle_type
        )

        query = {"terms": {"event.uuid.keyword": uuids}}
        with _datastore_client(request) as es:
            _update_by_query_with_conflict_retry(
                es,
                index=ALERT_INDEX_PATTERN,
                query=query,
                script=update_script,
                action=f"mark alerts as '{handle_type}'",
            )

        verb = "flagged" if handle_type == "flagged" else "dismissed"
        noun = "alert" if len(uuids) == 1 else "alerts"
        return JsonResponse({"message": f"{len(uuids)} {noun} {verb}"})

    except AlertUpdateError as e:
        return JsonResponse({"error": str(e)}, status=502)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)



def clear_all_data(request):
    """Dismiss all unhandled alerts, optionally limited to one alert section.

    Unlike :func:`handle_alerts`, this ignores any UI selection and acts on
    every active alert in scope.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        body = _json_body(request)
        alert_type = body.get("alert_type")
        query = (
            {"bool": {"filter": _build_alert_filter(alert_type)}}
            if alert_type
            else {"term": {"handled": False}}
        )
        update_script = _alert_update_script(handled=True, reason="dismissed")
        with _datastore_client(request) as es:
            _update_by_query_with_conflict_retry(
                es,
                index=ALERT_INDEX_PATTERN,
                query=query,
                script=update_script,
                action="dismiss alerts",
            )
        message = (
            f"All {ALERT_SECTION_LABELS[alert_type]} alerts dismissed"
            if alert_type
            else "All alerts dismissed"
        )
        return JsonResponse({"message": message})
    except AlertUpdateError as e:
        return JsonResponse({"error": str(e)}, status=502)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def _default_ingestor_health(service: str) -> dict[str, object]:
    return {
        "ok": False,
        "service": service,
        "status": "unknown",
        "message": "No health data yet",
        "latest_event_at": None,
        "checked_at": None,
        "age_seconds": None,
    }


def _get_ingestor_health(service: str) -> dict[str, object]:
    key = f"{INGESTOR_HEALTH_REDIS_PREFIX}:{service}"
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        payload = redis_client.get(key)
    except Exception as err:
        response = _default_ingestor_health(service)
        response["status"] = "error"
        response["message"] = f"Unable to read redis health key: {err}"
        return response

    if not payload:
        return _default_ingestor_health(service)

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        response = _default_ingestor_health(service)
        response["status"] = "error"
        response["message"] = "Invalid health payload format"
        return response

    parsed.setdefault("service", service)
    parsed.setdefault("status", "unknown")
    parsed.setdefault("message", "No message")
    parsed.setdefault("latest_event_at", None)
    parsed.setdefault("checked_at", None)
    parsed.setdefault("age_seconds", None)
    return parsed


def _format_bytes(size_bytes: int) -> str:
    """Return a compact human-readable byte value."""
    value = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024


def _get_disk_health() -> dict[str, object]:
    """Return disk usage health for the configured container path."""
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        usage = shutil.disk_usage(DISK_HEALTH_PATH)
    except Exception as err:
        return {
            "ok": False,
            "service": "disk",
            "status": "error",
            "message": f"Unable to read disk usage for {DISK_HEALTH_PATH}: {err}",
            "latest_event_at": None,
            "checked_at": checked_at,
            "age_seconds": None,
        }

    used_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0
    status = "ok"
    if used_percent >= DISK_CRITICAL_PERCENT:
        status = "critical"
    elif used_percent >= DISK_WARNING_PERCENT:
        status = "warning"

    return {
        "ok": status == "ok",
        "service": "disk",
        "status": status,
        "message": (
            f"{used_percent}% used "
            f"({_format_bytes(usage.used)} used / {_format_bytes(usage.total)} total, "
            f"{_format_bytes(usage.free)} free)"
        ),
        "latest_event_at": None,
        "checked_at": checked_at,
        "age_seconds": None,
        "disk": {
            "path": DISK_HEALTH_PATH,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": used_percent,
            "warning_percent": DISK_WARNING_PERCENT,
            "critical_percent": DISK_CRITICAL_PERCENT,
        },
    }


def ingestor_health(request):
    """Return latest monitoring health values for ingestors and host resources."""
    services = {
        "logstash": _get_ingestor_health("logstash"),
        "gpib": _get_ingestor_health("gpib"),
        "disk": _get_disk_health(),
    }
    return JsonResponse(
        {
            "services": services,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    )
