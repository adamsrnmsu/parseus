const csrfMeta = document.querySelector('meta[name="csrf-token"]');
if (csrfMeta) {
  axios.defaults.headers.common['X-CSRFToken'] = csrfMeta.getAttribute('content');
}

// axios has NO timeout by default: a request whose connection stalls (flaky
// network, a proxy holding the socket) never settles, so nothing in its
// .then/.catch/.finally ever runs. For a page left open for weeks that means a
// dismiss/flag request could leave `alertActionRequestInFlight` set and every
// action button disabled until the operator reloads. Bound every request.
//
// Reads (health, fallback alert query) should answer quickly and are repeated
// on a timer anyway. Dismiss/flag run an update_by_query with conflict retries
// on the server, so they get longer -- but stay below nginx's 90s proxy read
// timeout, which would otherwise answer first with a less useful 504.
const ALERT_READ_TIMEOUT_MS = 15000;
const ALERT_ACTION_TIMEOUT_MS = 60000;
axios.defaults.timeout = ALERT_READ_TIMEOUT_MS;

// The server renders the page title (see the title block in alerts/index.html).
// Remember it so the live alert count can be prefixed onto it, and restored
// when there is nothing to review, instead of hard-coding a title here.
const ALERT_BASE_DOCUMENT_TITLE = document.title;

const ALERT_SSE_ENABLED = true;
// After this many consecutive SSE connection failures, fall back to polling so
// a persistently-failing stream still delivers alerts instead of leaving the
// columns stuck on "Loading alerts...".
const ALERT_SSE_MAX_ERRORS_BEFORE_FALLBACK = 3;
// While the browser is still retrying the connection itself (EventSource
// readyState CONNECTING) every failed attempt fires another `error` event, a few
// seconds apart. A brief restart of the server or proxy produces several of
// them, so they are NOT counted towards the threshold above; instead, fallback
// polling starts only if the stream has stayed down for this long. A healthy
// frame at any point cancels the countdown.
const ALERT_SSE_RECONNECT_GRACE_MS = 30000;
// If no first snapshot (or connection) has arrived within this window, replace
// the initial "Loading alerts..." text with an explicit retrying state so the
// operator is never left staring at a permanent "Loading..." that silently
// hides whether alerts exist.
const ALERT_FIRST_SNAPSHOT_TIMEOUT_MS = 10000;
// The server sends a named "heartbeat" event whenever the data is unchanged, so
// silence is meaningful. EventSource raises no error when a proxy/NAT silently
// drops an idle TCP connection, so without this watchdog a dashboard left open
// for weeks could show frozen alerts indefinitely. The timeout is a multiple of
// the server's own heartbeat interval (announced in each heartbeat event); the
// default applies until the first heartbeat arrives.
const ALERT_STREAM_DEFAULT_HEARTBEAT_SECONDS = 30;
const ALERT_STREAM_STALL_HEARTBEATS = 3;
// Reconnect backoff for a stream the browser gave up on (EventSource.CLOSED) or
// that the watchdog declared stalled: exponential with full jitter, capped, so
// a restarting server is not hit by every open dashboard at the same instant.
const ALERT_STREAM_RECONNECT_BASE_MS = 1000;
const ALERT_STREAM_RECONNECT_MAX_MS = 30000;
let lastHealthCheckedAt = null;
let renderedHealthSignaturesByService = {};
let alertEventSource = null;
let alertFallbackIntervalId = null;
let alertActionRequestInFlight = false;
let alertStreamConsecutiveErrors = 0;
let alertFirstSnapshotReceived = false;
let alertFirstSnapshotTimeoutId = null;
let alertStreamWatchdogId = null;
let alertStreamReconnectTimeoutId = null;
let alertStreamOutageTimeoutId = null;
let alertStreamReconnectAttempts = 0;
let alertStreamHeartbeatSeconds = ALERT_STREAM_DEFAULT_HEARTBEAT_SECONDS;
let currentAlertsByType = { scpi_range: [], device: [] };
let alertTotalsByType = { scpi_range: 0, device: 0 };
let renderedAlertSignaturesByType = {};
let renderedAlertClientIdsByKey = new Map();
// In-flight guards for the timer-driven requests. A slow server must not let
// a 30s/60s interval stack up overlapping requests whose responses can then
// land out of order (an older response overwriting a newer one).
let healthRefreshInFlight = false;
let alertQueryInFlightByType = {};
let lastAlertToastByKey = {};
let pendingHandledAlertUuids = new Set();
let pendingFlaggedAlertUuids = new Set();


function normalizeStatus(status) {
  if (!status) return 'unknown';
  const value = String(status).toLowerCase();
  if (value === 'critical' || value === 'error') return 'critical';
  if (value === 'warning') return 'warning';
  if (value === 'ok') return 'ok';
  return 'unknown';
}

function getHealthServiceMeta(service) {
  const serviceMeta = {
    logstash: {
      label: 'Sysmon Health',
      icon: 'bi-diagram-3-fill',
      kicker: 'Endpoint signal ingestion',
      backingService: 'Logstash ingestion',
    },
    gpib: {
      label: 'SCPI Health',
      icon: 'bi-hdd-network-fill',
      kicker: 'Instrument telemetry ingestion',
      backingService: 'GPIB ingestion',
    },
    disk: {
      label: 'Disk Storage',
      icon: 'bi-device-hdd-fill',
      kicker: 'Container filesystem',
    },
  };

  return serviceMeta[service] || {
    label: 'Monitor',
    icon: 'bi-heart-pulse-fill',
    kicker: 'Health check',
  };
}

function getHealthStatusPersonality(status) {
  const statusCopy = {
    ok: {
      headline: 'Signal is clean',
      footer: 'Steady rhythm. This monitor is cruising.',
    },
    warning: {
      headline: 'Worth a closer look',
      footer: 'The watchfloor is tapping the glass on this one.',
    },
    critical: {
      headline: 'Needs attention',
      footer: 'Lights are hot. Time to put eyes on the service.',
    },
    unknown: {
      headline: 'Waiting for telemetry',
      footer: 'No confident pulse yet. Standing by for the next check-in.',
    },
  };

  return statusCopy[status] || statusCopy.unknown;
}

function buildHealthTooltip(service, health) {
  const normalizedStatus = normalizeStatus(health.status);
  const serviceMeta = getHealthServiceMeta(service);
  const personality = getHealthStatusPersonality(normalizedStatus);
  const checked = escapeHtml(parseusDatetime.formatTimestamp(health.checked_at));
  const displayStatus = escapeHtml(String(health.status || normalizedStatus).toUpperCase());
  const message = escapeHtml(health.message || 'No detailed status message available yet.');
  const rows = [];

  if (service === 'disk' && health.disk) {
    rows.push(['Path', escapeHtml(health.disk.path)]);
    rows.push(['Used', `${escapeHtml(health.disk.used_percent)}%`]);
    rows.push(['Warning', `${escapeHtml(health.disk.warning_percent)}%`]);
    rows.push(['Critical', `${escapeHtml(health.disk.critical_percent)}%`]);
  } else {
    if (serviceMeta.backingService) {
      rows.push(['Backed by', escapeHtml(serviceMeta.backingService)]);
    }
    rows.push(['Last event', escapeHtml(parseusDatetime.formatTimestamp(health.latest_event_at))]);
  }

  rows.push(['Last check', checked]);

  const details = rows.map(([label, value]) => `
    <div class="health-tooltip-row">
      <span>${label}</span>
      <strong>${value}</strong>
    </div>`).join('');

  return `
    <div class="health-tooltip-card health-tooltip-${normalizedStatus}">
      <div class="health-tooltip-header">
        <span class="health-tooltip-icon"><i class="bi ${serviceMeta.icon}"></i></span>
        <div class="health-tooltip-heading">
          <span>${escapeHtml(serviceMeta.kicker)}</span>
          <strong>${escapeHtml(serviceMeta.label)}</strong>
        </div>
        <span class="health-tooltip-badge">${displayStatus}</span>
      </div>
      <div class="health-tooltip-pulse">${personality.headline}</div>
      <div class="health-tooltip-message">${message}</div>
      <div class="health-tooltip-grid">${details}</div>
      <div class="health-tooltip-footer">${personality.footer}</div>
    </div>`;
}

function getHealthRenderSignature(health) {
  return JSON.stringify({
    status: normalizeStatus(health.status),
    displayStatus: String(health.status || '').toUpperCase(),
    message: health.message || '',
    latestEventAt: health.latest_event_at || null,
    checkedAt: health.checked_at || null,
    disk: health.disk
      ? {
        path: health.disk.path,
        usedPercent: health.disk.used_percent,
        warningPercent: health.disk.warning_percent,
        criticalPercent: health.disk.critical_percent,
      }
      : null,
  });
}

function renderHealthLastUpdated() {
  const element = document.getElementById('healthLastUpdated');
  if (!element) return;

  if (!lastHealthCheckedAt) {
    element.textContent = 'Last updated: not yet available';
    return;
  }

  element.textContent = `Last updated: ${parseusDatetime.formatTimestamp(lastHealthCheckedAt)}`;
}

function updateHealthToggleText(isExpanded) {
  const toggle = document.getElementById('monitorHealthToggle');
  const toggleText = document.getElementById('monitorHealthToggleText');
  const toggleIcon = document.getElementById('monitorHealthToggleIcon');
  if (!toggle || !toggleText || !toggleIcon) return;

  toggleText.textContent = isExpanded ? 'Hide Monitor Status' : 'Show Monitor Status';
  toggleIcon.className = isExpanded ? 'bi bi-chevron-up me-2' : 'bi bi-chevron-down me-2';
  toggle.setAttribute('aria-expanded', String(isExpanded));
}

function initializeHealthToggleBehavior() {
  const toggle = document.getElementById('monitorHealthToggle');
  const collapse = document.getElementById('monitorHealthCollapse');
  if (!toggle || !collapse) return;

  updateHealthToggleText(collapse.classList.contains('show'));

  collapse.addEventListener('shown.bs.collapse', () => {
    updateHealthToggleText(true);
  });

  collapse.addEventListener('hidden.bs.collapse', () => {
    updateHealthToggleText(false);
  });
}

function renderHealthStatus(service, health) {
  const led = document.getElementById(`healthLed-${service}`);
  const label = document.getElementById(`healthStatusLabel-${service}`);
  const message = document.getElementById(`healthMessage-${service}`);
  if (!led || !message) {
    return;
  }

  const nextSignature = getHealthRenderSignature(health);
  if (renderedHealthSignaturesByService[service] === nextSignature) {
    return;
  }
  renderedHealthSignaturesByService[service] = nextSignature;

  const status = normalizeStatus(health.status);
  const displayStatus = String(health.status || status).toUpperCase();
  led.className = `status-led status-${status}`;
  if (label) {
    label.textContent = displayStatus;
    label.className = `health-status-badge status-${status}`;
  }

  const tooltipText = buildHealthTooltip(service, health);
  led.setAttribute('title', tooltipText);
  led.setAttribute('data-bs-title', tooltipText);
  led.setAttribute('data-bs-html', 'true');
  led.setAttribute('data-bs-custom-class', `alerts-health-tooltip alerts-health-tooltip-${status}`);

  const existingTooltip = bootstrap.Tooltip.getInstance(led);
  if (existingTooltip) {
    existingTooltip.dispose();
  }
  new bootstrap.Tooltip(led, {
    boundary: 'window',
    customClass: `alerts-health-tooltip alerts-health-tooltip-${status}`,
    html: true,
    sanitize: false,
  });

  message.textContent = health.message || 'No status message available';

  if (service === 'disk') {
    renderDiskUsage(health, status);
  }
}

function renderDiskUsage(health, status) {
  const usageFill = document.getElementById('diskUsageFill');
  if (!usageFill) return;
  const usedPercent = health.disk && Number.isFinite(Number(health.disk.used_percent))
    ? Number(health.disk.used_percent)
    : 0;
  usageFill.style.width = `${Math.min(100, Math.max(0, usedPercent))}%`;
  usageFill.className = `disk-usage-fill status-${status}`;
}

async function refreshMonitorHealth() {
  if (healthRefreshInFlight) return;
  healthRefreshInFlight = true;

  await axios.get('monitor-health/')
    .then((response) => {
      const services = response.data.services || {};
      renderHealthStatus('logstash', services.logstash || {});
      renderHealthStatus('gpib', services.gpib || {});
      renderHealthStatus('disk', services.disk || {});
      lastHealthCheckedAt = response.data.checked_at || new Date().toISOString();
      renderHealthLastUpdated();
    })
    .catch((error) => {
      console.error('Error loading monitor health:', error);
      alertToastError('Error loading monitor status', 6000, 'monitor-health-error');
      renderHealthStatus('logstash', {
        status: 'error',
        message: 'Failed to load status',
        latest_event_at: null,
        checked_at: null,
      });
      renderHealthStatus('gpib', {
        status: 'error',
        message: 'Failed to load status',
        latest_event_at: null,
        checked_at: null,
      });
      renderHealthStatus('disk', {
        service: 'disk',
        status: 'error',
        message: 'Failed to load disk status',
        latest_event_at: null,
        checked_at: null,
      });
      renderHealthLastUpdated();
    })
    .finally(() => {
      healthRefreshInFlight = false;
    });
}

function alertToast(type, message, length = 5000, key = null, dedupeMs = 3000) {
  const toastKey = key || `${type}:${message}`;
  const now = Date.now();
  if (lastAlertToastByKey[toastKey] && now - lastAlertToastByKey[toastKey] < dedupeMs) {
    return;
  }
  lastAlertToastByKey[toastKey] = now;

  const safeMessage = escapeHtml(message);
  if (type === 'success') {
    toastSuccess(safeMessage, length);
    return;
  }
  if (type === 'warning') {
    toastWarning(safeMessage, length);
    return;
  }
  toastError(safeMessage, length);
}

function alertToastError(message, length = 5000, key = null) {
  alertToast('error', message, length, key);
}

function alertToastSuccess(message, length = 5000, key = null) {
  alertToast('success', message, length, key);
}

function isAxiosTimeout(error) {
  return Boolean(error) && (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT');
}

function getAxiosResponseData(error) {
  return (error && error.response && error.response.data) || {};
}

function getAxiosErrorMessage(error) {
  if (isAxiosTimeout(error)) return 'The server did not respond in time.';
  const data = getAxiosResponseData(error);
  if (data.error) return data.error;
  if (data.message) return data.message;
  if (error && error.message) return error.message;
  return String(error || 'Unknown error');
}

function getStableClientAlertId(alert, alertType, index) {
  const uuid = getAlertUuid(alert);
  if (uuid) return uuid;

  const key = `${alertType}:${alert && alert['@timestamp'] ? alert['@timestamp'] : index}:${alert && alert.event ? alert.event.original : ''}`;
  if (!renderedAlertClientIdsByKey.has(key)) {
    const randomId = window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    renderedAlertClientIdsByKey.set(key, `client-alert-${randomId}`);
  }
  return renderedAlertClientIdsByKey.get(key);
}

// Buttons that act on every active alert, regardless of selection.
const ALERT_BULK_ACTION_SELECTOR = '#dismissAllButton, [data-dismiss-alert-section]';
// Buttons that act only on checked alerts, so they need a selection to be usable.
const ALERT_SELECTION_ACTION_SELECTOR = '#dismissSelectedButton, #flagAlertButton';

function setAlertActionButtonsDisabled(disabled) {
  document.querySelectorAll(ALERT_BULK_ACTION_SELECTOR).forEach((button) => {
    button.disabled = disabled;
  });
  // Re-enabling must not make selection-only buttons usable with nothing selected.
  const hasSelection = getSelectedAlertCount() > 0;
  document.querySelectorAll(ALERT_SELECTION_ACTION_SELECTOR).forEach((button) => {
    button.disabled = disabled || !hasSelection;
  });
}

function getAlertRenderTargets(alertType) {
  if (alertType === 'device') {
    return {
      alertColumn: document.getElementById("device-alerts"),
      alertResults: document.getElementById("deviceResults"),
      alertHead: document.getElementById("deviceAlertScreen"),
    };
  }

  if (alertType === 'scpi_range') {
    return {
      alertColumn: document.getElementById("scpi_range_alerts"),
      alertResults: document.getElementById("rangeResults"),
      alertHead: document.getElementById("rangeAlertScreen"),
    };
  }

  return null;
}

// Used for fallback refreshes.
function alertQuery(alertType) {
  const targets = getAlertRenderTargets(alertType);
  if (!targets) {
    console.error('Invalid alert type:', alertType);
    return;
  }

  return alertQueryHandle(alertType, targets.alertColumn, targets.alertHead, targets.alertResults);
}

function getAlertCountElement(alertType) {
  if (alertType === 'device') return document.getElementById('deviceAlertCount');
  if (alertType === 'scpi_range') return document.getElementById('rangeAlertCount');
  return null;
}

// Operator-facing section names, used in accessible labels.
const ALERT_SECTION_NAMES = { scpi_range: 'SCPI', device: 'Sysmon' };

function getSectionSelectButton(alertType) {
  return document.querySelector(`[data-select-alert-section="${alertType}"]`);
}

function getSectionAlertCheckboxes(alertType) {
  return Array.from(document.querySelectorAll(`input.checkbox-alert[data-alert-type="${alertType}"]`));
}

function getSelectedAlertCount() {
  return document.querySelectorAll('input.checkbox-alert:checked').length;
}

function updateSelectedAlertCount() {
  const element = document.getElementById('selectedAlertCount');
  if (!element) return;

  const selectedCount = getSelectedAlertCount();
  const value = element.querySelector('[data-selected-alert-count-value]');
  if (value) {
    value.textContent = selectedCount;
  }
  element.classList.toggle('d-none', selectedCount === 0);
  element.classList.toggle('has-selection', selectedCount > 0);
  element.setAttribute('aria-label', `${selectedCount} alert${selectedCount === 1 ? '' : 's'} selected`);

  // Selection-only actions are unusable with nothing checked. Never re-enable
  // them mid-request; setAlertActionButtonsDisabled(false) runs afterwards.
  document.querySelectorAll(ALERT_SELECTION_ACTION_SELECTOR).forEach((button) => {
    button.disabled = alertActionRequestInFlight || selectedCount === 0;
  });
}

function updateSectionSelectionButton(alertType) {
  const button = getSectionSelectButton(alertType);
  if (!button) return;

  const checkboxes = getSectionAlertCheckboxes(alertType);
  const selectedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
  const allSelected = checkboxes.length > 0 && selectedCount === checkboxes.length;
  const label = button.querySelector('span');

  button.disabled = checkboxes.length === 0;
  button.setAttribute('aria-pressed', String(allSelected));
  const sectionName = ALERT_SECTION_NAMES[alertType] || alertType;
  button.setAttribute(
    'aria-label',
    allSelected ? `Deselect all ${sectionName} alerts` : `Select all ${sectionName} alerts`,
  );
  if (label) {
    label.textContent = allSelected ? 'Deselect All' : 'Select All';
  }
  updateSelectedAlertCount();
}

function updateAllSectionSelectionButtons() {
  Object.keys(currentAlertsByType).forEach(updateSectionSelectionButton);
}

function toggleSectionAlertSelection(alertType) {
  const checkboxes = getSectionAlertCheckboxes(alertType);
  if (checkboxes.length === 0) {
    alertToastError('There are no alerts to select in this section.', 5000);
    updateSectionSelectionButton(alertType);
    return;
  }

  const shouldSelectAll = checkboxes.some((checkbox) => !checkbox.checked);
  checkboxes.forEach((checkbox) => {
    checkbox.checked = shouldSelectAll;
  });
  updateSectionSelectionButton(alertType);
}

function renderEmptyAlertState(alertResults) {
  alertResults.innerHTML = `
    <div class="alert-empty-state">
      <div>
        <i class="bi bi-shield-check"></i>
        <strong>No active alerts</strong>
        <div class="small mt-1">This column is quiet. New detections will appear here automatically.</div>
      </div>
    </div>`;
}

function getTotalActiveAlertCount() {
  // Reflect the TRUE number of active alerts the operator should trust, not the
  // capped rendered list. For each column use the server total when known
  // (falling back to the rendered count), then discount alerts the operator
  // just optimistically handled (removed) but the server has not yet confirmed,
  // clamped at zero. Flagged alerts stay visible/active, so they are not
  // discounted.
  const summedTotal = Object.keys(currentAlertsByType).reduce((sum, alertType) => {
    const rendered = (currentAlertsByType[alertType] || []).length;
    const total = Number(alertTotalsByType[alertType]);
    return sum + (Number.isFinite(total) ? Math.max(total, rendered) : rendered);
  }, 0);
  return Math.max(0, summedTotal - pendingHandledAlertUuids.size);
}

function updateWatchMode(totalActiveAlerts) {
  const watchMode = document.getElementById('alertWatchMode');
  const icon = document.getElementById('alertWatchModeIcon');
  const label = document.getElementById('alertWatchModeLabel');
  const message = document.getElementById('alertModeMessage');
  const count = document.getElementById('totalActiveAlertCount');
  if (!watchMode || !icon || !label || !message || !count) return;

  const nextCount = String(totalActiveAlerts);
  if (watchMode.dataset.activeAlertCount === nextCount) return;

  const hasActiveAlerts = totalActiveAlerts > 0;
  watchMode.dataset.activeAlertCount = nextCount;
  watchMode.classList.toggle('watch-mode-active', hasActiveAlerts);
  watchMode.classList.toggle('watch-mode-clear', !hasActiveAlerts);
  count.textContent = totalActiveAlerts;

  if (hasActiveAlerts) {
    icon.innerHTML = '<i class="bi bi-exclamation-triangle-fill"></i>';
    label.textContent = 'ALERT MODE';
    message.textContent = `${totalActiveAlerts} detection${totalActiveAlerts === 1 ? '' : 's'} need review now.`;
    document.title = `(${totalActiveAlerts}) ${ALERT_BASE_DOCUMENT_TITLE}`;
    return;
  }

  icon.innerHTML = '<i class="bi bi-shield-check"></i>';
  label.textContent = 'All clear';
  message.textContent = 'No active detections.';
  document.title = ALERT_BASE_DOCUMENT_TITLE;
}

function updatePageAlertTheme() {
  const totalActiveAlerts = getTotalActiveAlertCount();
  const pageContainer = document.querySelector('.alerts-page-container');
  if (pageContainer) {
    pageContainer.classList.toggle('alerts-active-theme', totalActiveAlerts > 0);
  }
  updateWatchMode(totalActiveAlerts);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function getAlertUuid(alert) {
  return alert && alert.event ? alert.event.uuid : null;
}

function getAlertListSignature(alerts) {
  return JSON.stringify((alerts || []).map(getAlertSignatureEntry));
}

function getAlertSignatureEntry(alert) {
  const { '@timestamp': timestamp, agent, event, acknowledge } = alert || {};
  return {
    uuid: getAlertUuid(alert),
    timestamp: timestamp || null,
    agentType: agent ? agent.type : null,
    original: event ? event.original : null,
    reason: acknowledge ? acknowledge.reason : null,
  };
}

function cloneAlertsByType(alertsByType = currentAlertsByType) {
  return Object.fromEntries(
    Object.entries(alertsByType).map(([alertType, alerts]) => [
      alertType,
      (alerts || []).map((alert) => ({
        ...alert,
        agent: { ...(alert.agent || {}) },
        event: { ...(alert.event || {}) },
        acknowledge: { ...(alert.acknowledge || {}) },
      })),
    ]),
  );
}

function applyPendingAlertActions(alertsByType) {
  const nextAlertsByType = cloneAlertsByType(alertsByType);

  Object.keys(nextAlertsByType).forEach((alertType) => {
    nextAlertsByType[alertType] = nextAlertsByType[alertType]
      .filter((alert) => !pendingHandledAlertUuids.has(getAlertUuid(alert)))
      .map((alert) => {
        if (!pendingFlaggedAlertUuids.has(getAlertUuid(alert))) return alert;
        return {
          ...alert,
          acknowledge: { ...(alert.acknowledge || {}), reason: 'flagged' },
        };
      });
  });

  return nextAlertsByType;
}

// Reconcile the optimistic pending sets against a freshly received RAW server
// snapshot (before applyPendingAlertActions filters it). An optimistic hide/flag
// is kept only until the server's own data confirms the change landed:
//   - handled (false-positive): the uuid is no longer present in the snapshot
//     for its column  -> the backend committed the removal -> stop suppressing.
//   - flagged: the uuid is present with acknowledge.reason === 'flagged'
//     -> the backend committed the flag -> stop overriding.
// This replaces a blind fixed-delay timer, which could expire while a stale
// in-flight snapshot (captured before the backend write, or during the
// handle_alerts retry window) was still arriving -- making a just-handled alert
// flicker back into view for one refresh. Reconciling against real data means a
// handled alert stays hidden until, and only until, the server agrees.
function collectSnapshotUuids(alertsByType) {
  const presentUuids = new Set();
  const flaggedUuids = new Set();
  Object.values(alertsByType || {}).forEach((alerts) => {
    (alerts || []).forEach((alert) => {
      const uuid = getAlertUuid(alert);
      if (!uuid) return;
      presentUuids.add(uuid);
      if (alert.acknowledge && alert.acknowledge.reason === 'flagged') {
        flaggedUuids.add(uuid);
      }
    });
  });
  return { presentUuids, flaggedUuids };
}

function reconcilePendingAlertActions(rawSnapshotAlertsByType) {
  if (pendingHandledAlertUuids.size === 0 && pendingFlaggedAlertUuids.size === 0) {
    return;
  }

  const { presentUuids, flaggedUuids } = collectSnapshotUuids(rawSnapshotAlertsByType);

  // A handled (removed) alert is confirmed once the server no longer returns it.
  pendingHandledAlertUuids.forEach((uuid) => {
    if (!presentUuids.has(uuid)) {
      pendingHandledAlertUuids.delete(uuid);
    }
  });
  // A flagged alert is confirmed once the server reports it as flagged.
  pendingFlaggedAlertUuids.forEach((uuid) => {
    if (flaggedUuids.has(uuid)) {
      pendingFlaggedAlertUuids.delete(uuid);
    }
  });
}

// Backstop: if the server never confirms a pending action (e.g. the alert fell
// past the bounded result cap, so it stops appearing for a reason unrelated to
// our action), release the optimistic mark after a generous delay so the UI
// cannot get permanently stuck hiding/flagging. This is a safety net, not the
// primary mechanism -- reconcilePendingAlertActions clears the common case.
function clearPendingAlertActions(uuids, delayMs = 30000) {
  window.setTimeout(() => {
    uuids.forEach((uuid) => {
      pendingHandledAlertUuids.delete(uuid);
      pendingFlaggedAlertUuids.delete(uuid);
    });
  }, delayMs);
}

function captureAlertSelection(alertResults) {
  const selectedIds = new Set();
  alertResults.querySelectorAll('input.checkbox-alert:checked').forEach((checkbox) => {
    selectedIds.add(checkbox.dataset.alertUuid);
  });
  const focused = document.activeElement;
  const focusedId = focused
    && focused.classList
    && focused.classList.contains('checkbox-alert')
    && alertResults.contains(focused)
    ? focused.dataset.alertUuid
    : null;
  return { selectedIds, focusedId };
}

function restoreAlertSelection(alertResults, { selectedIds, focusedId }) {
  alertResults.querySelectorAll('input.checkbox-alert').forEach((checkbox) => {
    const id = checkbox.dataset.alertUuid;
    if (selectedIds.has(id)) {
      checkbox.checked = true;
    }
    if (focusedId !== null && id === focusedId) {
      checkbox.focus({ preventScroll: true });
    }
  });
}

function buildAlertRow(alert, alertType, index) {
  const alertUuid = escapeHtml(getStableClientAlertId(alert, alertType, index));
  const alertTimestamp = escapeHtml(parseusDatetime.formatTimestamp(alert["@timestamp"], "compact", "No timestamp"));
  const alertAgent = escapeHtml(alert.agent && alert.agent.type ? alert.agent.type : 'Alert');
  const alertOriginal = escapeHtml(alert.event && alert.event.original ? alert.event.original : 'No alert details available');
  const alertMessage = document.createElement("div");
  const isFlagged = alert.acknowledge && alert.acknowledge.reason === "flagged";
  const flagClass = isFlagged ? "" : "d-none";
  const cardClass = isFlagged ? "alert-card flagged" : "alert-card";
  alertMessage.className = "alert-message";
  alertMessage.innerHTML = `
    <div class="alert-record d-grid gap-2">
      <label class="${cardClass}" for="alert-${alertUuid}">
        <div class="d-flex justify-content-between align-items-center gap-2 mb-2">
          <div class="small d-flex align-items-center gap-2">
            <input type="checkbox" class="checkbox-alert form-check-input m-0" id="alert-${alertUuid}" data-alert-type="${alertType}" data-alert-uuid="${alertUuid}" autocomplete="off">
            <span>${alertTimestamp}</span>
          </div>
          <div class="icon-flag ${flagClass}">
            <i class="bi bi-flag-fill text-warning"></i>
          </div>
        </div>
        <div class="small ps-4" style="word-break:break-all;"><strong>${alertAgent}:</strong> ${alertOriginal}</div>
      </label>
    </div>`;
  return alertMessage;
}

function getAlertTotalCount(alertType, alertCount) {
  const total = Number(alertTotalsByType[alertType]);
  return Number.isFinite(total) ? Math.max(total, alertCount) : alertCount;
}

function renderAlertList(alertType, alerts, alertColumn, alertHead, alertResults, options = {}) {
  const nextAlerts = cloneAlertsByType({ [alertType]: alerts })[alertType] || [];
  const nextSignature = getAlertListSignature(nextAlerts);
  const alertCount = nextAlerts.length;
  // Total matching alerts on the server (may exceed what is fetched/shown when
  // the backend caps the result set). Falls back to the rendered count when the
  // total is unknown (e.g. older payloads without a totals field).
  const totalCount = getAlertTotalCount(alertType, alertCount);
  const isCapped = totalCount > alertCount;
  const countElement = getAlertCountElement(alertType);
  // Re-render when the visible list changes OR when the capped total changes,
  // so the "showing N of M" indicator stays accurate even if the top-N list
  // happens to be byte-identical across two snapshots.
  const nextSignatureWithTotal = `${nextSignature}|total:${totalCount}`;
  const existingSignature = renderedAlertSignaturesByType[alertType];
  const canSkipDomUpdate = options.forceRender !== true && existingSignature === nextSignatureWithTotal;

  currentAlertsByType[alertType] = nextAlerts;

  if (countElement) {
    // Show the true total so the operator's sense of "how many" is never
    // silently clamped by the fetch cap.
    countElement.textContent = totalCount;
  }

  alertColumn.classList.toggle('has-alerts', alertCount > 0);
  alertColumn.classList.toggle('no-alerts', alertCount === 0);

  if (canSkipDomUpdate) {
    updateSectionSelectionButton(alertType);
    if (options.updateTheme !== false) {
      updatePageAlertTheme();
    }
    return;
  }

  const previousScrollTop = alertResults.scrollTop;
  // The list is rebuilt from scratch below, which would discard any checkbox the
  // operator has ticked and the keyboard focus that is on one. With alerts
  // arriving continuously that made multi-selecting effectively impossible, so
  // remember both by alert id and re-apply them to the new rows.
  const selection = captureAlertSelection(alertResults);
  const fragment = document.createDocumentFragment();

  if (alertCount > 0) {
    // The count pill and watch-mode card already show how many alerts are
    // active, so the status line is only used when the list is capped: the
    // pill shows the true total, which the operator can't otherwise see
    // exceeds the rendered rows.
    alertHead.textContent = isCapped
      ? `Showing the ${alertCount} newest of ${totalCount}.`
      : '';

    nextAlerts.forEach((alert, index) => {
      fragment.appendChild(buildAlertRow(alert, alertType, index));
    });
    alertResults.replaceChildren(fragment);

    // Re-apply the operator's selection/focus. Alerts that are gone from the new
    // snapshot simply have no row to restore, so the selection can never refer
    // to something the operator can no longer see.
    restoreAlertSelection(alertResults, selection);
  } else {
    // The empty-state panel below already says "No active alerts".
    alertHead.textContent = '';
    renderEmptyAlertState(alertResults);
  }

  renderedAlertSignaturesByType[alertType] = nextSignatureWithTotal;
  alertResults.scrollTop = Math.min(previousScrollTop, alertResults.scrollHeight);
  updateSectionSelectionButton(alertType);
  if (options.updateTheme !== false) {
    updatePageAlertTheme();
  }
}

async function alertQueryHandle(alertType, alertColumn, alertHead, alertResults) {
  // Skip, rather than queue, a refresh while the previous one for this column is
  // still outstanding (slow or unreachable server): stacked requests would only
  // add load to a server that is already struggling and can resolve out of order.
  if (alertQueryInFlightByType[alertType]) return;
  alertQueryInFlightByType[alertType] = true;

  await axios.post("alert-query/", { alert_type: alertType })
    .then(response => {
      const rawAlerts = response.data.alerts || [];
      // Reconcile and apply optimistic marks against the raw server data, same
      // as the SSE path, so the fallback poll behaves identically.
      reconcilePendingAlertActions({ [alertType]: rawAlerts });
      if (typeof response.data.total === 'number') {
        alertTotalsByType = { ...alertTotalsByType, [alertType]: response.data.total };
      }
      const alerts = applyPendingAlertActions({ [alertType]: rawAlerts })[alertType] || [];
      renderAlertList(alertType, alerts, alertColumn, alertHead, alertResults);
      return response;
    })
    .catch(error => {
      console.error('Error:', error);
      alertToastError(`Error querying alerts: ${getAxiosErrorMessage(error)}`, 10000, 'alert-query-error');
    })
    .finally(() => {
      alertQueryInFlightByType[alertType] = false;
    });
}

function renderAlertSnapshot(snapshot, options = {}) {
  const rawAlertsByType = snapshot.alerts || {};
  // Only a genuine server snapshot may release optimistic marks. Local
  // optimistic/rollback re-renders pass alerts that have ALREADY had the
  // pending actions applied (handled alerts removed), so reconciling against
  // them would immediately clear the suppression and defeat the optimistic
  // hide. Reconcile BEFORE filtering so a server-confirmed removal/flag stops
  // being suppressed on the same tick it is confirmed.
  if (options.fromServer) {
    reconcilePendingAlertActions(rawAlertsByType);
    if (snapshot.totals) {
      alertTotalsByType = { ...alertTotalsByType, ...snapshot.totals };
    }
  }
  const alertsByType = applyPendingAlertActions(rawAlertsByType);
  Object.keys(alertsByType).forEach((alertType) => {
    const targets = getAlertRenderTargets(alertType);
    if (!targets) return;

    renderAlertList(
      alertType,
      alertsByType[alertType] || [],
      targets.alertColumn,
      targets.alertHead,
      targets.alertResults,
      { updateTheme: false },
    );
  });
  updatePageAlertTheme();
}

function stopAlertFallbackPolling() {
  if (!alertFallbackIntervalId) return;
  window.clearInterval(alertFallbackIntervalId);
  alertFallbackIntervalId = null;
}

function startAlertFallbackPolling() {
  if (alertFallbackIntervalId) return;

  alertQuery('scpi_range');
  alertQuery('device');
  alertFallbackIntervalId = window.setInterval(() => {
    if (document.hidden) return;
    alertQuery('scpi_range');
    alertQuery('device');
  }, 30000);
}

// Tear down only the connection and its watchdog, leaving fallback polling and
// failure counters alone. A reconnect uses this so that replacing a dead stream
// does not forget how many times it has already failed.
function closeAlertEventSource() {
  if (alertStreamWatchdogId) {
    window.clearTimeout(alertStreamWatchdogId);
    alertStreamWatchdogId = null;
  }
  if (!alertEventSource) return;
  alertEventSource.close();
  alertEventSource = null;
}

// Full teardown: connection, watchdog, pending reconnect, first-snapshot timer
// and fallback polling. Used when the page is hidden or unloading.
function closeAlertStream() {
  stopAlertFallbackPolling();
  if (alertFirstSnapshotTimeoutId) {
    window.clearTimeout(alertFirstSnapshotTimeoutId);
    alertFirstSnapshotTimeoutId = null;
  }
  if (alertStreamReconnectTimeoutId) {
    window.clearTimeout(alertStreamReconnectTimeoutId);
    alertStreamReconnectTimeoutId = null;
  }
  clearAlertStreamOutageTimer();
  closeAlertEventSource();
}

// (Re)start the stall watchdog. Called on every frame the server sends: any
// frame proves the connection is alive, so only silence trips it.
function armAlertStreamWatchdog() {
  if (alertStreamWatchdogId) {
    window.clearTimeout(alertStreamWatchdogId);
  }
  const stallMs = alertStreamHeartbeatSeconds * ALERT_STREAM_STALL_HEARTBEATS * 1000;
  alertStreamWatchdogId = window.setTimeout(() => {
    alertStreamWatchdogId = null;
    console.warn(`Alert stream silent for ${stallMs / 1000}s; reconnecting.`);
    handleAlertStreamFailure();
    scheduleAlertStreamReconnect();
  }, stallMs);
}

// Evidence the stream is working end to end (a snapshot or heartbeat is only
// sent after a successful datastore query). Clears everything a past failure
// set in motion, so isolated blips months apart can never accumulate towards
// the fallback threshold or leave polling running next to a healthy stream.
function noteAlertStreamHealthy() {
  alertStreamConsecutiveErrors = 0;
  alertStreamReconnectAttempts = 0;
  clearAlertStreamOutageTimer();
  stopAlertFallbackPolling();
  armAlertStreamWatchdog();
}

// Start the outage countdown (once) when the browser reports it is retrying the
// connection. Repeated errors during the same outage do not restart it.
function startAlertStreamOutageTimer() {
  if (alertStreamOutageTimeoutId) return;
  alertStreamOutageTimeoutId = window.setTimeout(() => {
    alertStreamOutageTimeoutId = null;
    console.warn(`Alert stream down for ${ALERT_SSE_RECONNECT_GRACE_MS / 1000}s; falling back to polling.`);
    escalateAlertStreamOutage();
  }, ALERT_SSE_RECONNECT_GRACE_MS);
}

function clearAlertStreamOutageTimer() {
  if (!alertStreamOutageTimeoutId) return;
  window.clearTimeout(alertStreamOutageTimeoutId);
  alertStreamOutageTimeoutId = null;
}

// Replace the current connection after a failure the browser will not recover
// from by itself: the EventSource gave up (readyState CLOSED, e.g. a 502 from a
// proxy mid-restart) or the watchdog saw the connection go silent. Backs off
// exponentially with full jitter so a restarting server is not hit by every
// open dashboard at once.
function scheduleAlertStreamReconnect() {
  if (alertStreamReconnectTimeoutId) return;
  closeAlertEventSource();

  const ceilingMs = Math.min(
    ALERT_STREAM_RECONNECT_MAX_MS,
    ALERT_STREAM_RECONNECT_BASE_MS * 2 ** alertStreamReconnectAttempts,
  );
  alertStreamReconnectAttempts += 1;
  const delayMs = Math.random() * ceilingMs;

  alertStreamReconnectTimeoutId = window.setTimeout(() => {
    alertStreamReconnectTimeoutId = null;
    if (document.hidden) return; // visibilitychange restarts the stream on return
    openAlertStream();
  }, delayMs);
}

// Set the status line text for every alert column at once (e.g. to replace the
// initial "Loading alerts..." with an explicit error/retrying state so the
// operator is never left unsure whether the feed is working).
function setAllAlertStatusLines(html) {
  Object.keys(currentAlertsByType).forEach((alertType) => {
    const targets = getAlertRenderTargets(alertType);
    if (targets && targets.alertHead) {
      targets.alertHead.innerHTML = html;
    }
  });
}

function initializeAlertStream() {
  if (!ALERT_SSE_ENABLED) {
    startAlertFallbackPolling();
    return;
  }

  if (document.hidden) {
    closeAlertStream();
    return;
  }

  if (!window.EventSource) {
    console.warn('EventSource is not supported by this browser. Falling back to periodic alert refresh.');
    startAlertFallbackPolling();
    return;
  }

  closeAlertStream();
  alertStreamConsecutiveErrors = 0;
  alertStreamReconnectAttempts = 0;
  alertStreamHeartbeatSeconds = ALERT_STREAM_DEFAULT_HEARTBEAT_SECONDS;

  // Guard against a silent "Loading alerts..." forever: if no first snapshot
  // (or connection) has arrived in time, show an explicit retrying state and
  // kick off polling as a fallback so alerts still reach the operator.
  alertFirstSnapshotReceived = false;
  alertFirstSnapshotTimeoutId = window.setTimeout(() => {
    if (!alertFirstSnapshotReceived) {
      setAllAlertStatusLines('<strong>Unable to load alerts yet — retrying…</strong>');
      startAlertFallbackPolling();
    }
  }, ALERT_FIRST_SNAPSHOT_TIMEOUT_MS);

  openAlertStream();
}

function handleAlertSnapshotEvent(event) {
  try {
    const snapshot = JSON.parse(event.data);
    alertFirstSnapshotReceived = true;
    if (alertFirstSnapshotTimeoutId) {
      window.clearTimeout(alertFirstSnapshotTimeoutId);
      alertFirstSnapshotTimeoutId = null;
    }
    noteAlertStreamHealthy();
    renderAlertSnapshot(snapshot, { fromServer: true });
  } catch (error) {
    console.error('Error parsing alert stream payload:', error);
  }
}

function handleAlertHeartbeatEvent(event) {
  try {
    const { interval_seconds: intervalSeconds } = JSON.parse(event.data);
    if (Number.isFinite(intervalSeconds) && intervalSeconds > 0) {
      alertStreamHeartbeatSeconds = intervalSeconds;
    }
  } catch (error) {
    console.error('Error parsing alert stream heartbeat:', error);
  }
  noteAlertStreamHealthy();
}

// Open one EventSource connection. Assumes any previous connection has already
// been closed and does not touch failure counters or fallback polling, so it can
// serve both the initial connect and every later reconnect.
function openAlertStream() {
  const source = new EventSource("alert-stream/");
  alertEventSource = source;
  armAlertStreamWatchdog();

  // Handlers of a connection that has since been replaced must not act on the
  // current one's state.
  const isCurrent = () => source === alertEventSource;

  source.addEventListener("alert_snapshot", (event) => {
    if (isCurrent()) handleAlertSnapshotEvent(event);
  });

  // Sent by the server while the data is unchanged. EventSource cannot see SSE
  // comment lines, so without a named event a quiet system looked identical to a
  // dead connection, and a healthy stream could never clear earlier failures.
  source.addEventListener("heartbeat", (event) => {
    if (isCurrent()) handleAlertHeartbeatEvent(event);
  });

  source.addEventListener("alert_error", (event) => {
    if (!isCurrent()) return;
    console.error('Alert stream error event:', event.data);
    alertToastError('Error streaming alerts. The browser will keep trying to reconnect.', 6000, 'alert-stream-error');
    // The server is alive (it sent this frame), so keep the watchdog quiet, but
    // the failed query still counts toward the fallback threshold: a stream that
    // stays connected yet keeps erroring (e.g. datastore down) flips over to
    // polling instead of leaving the columns stale.
    armAlertStreamWatchdog();
    handleAlertStreamFailure();
  });

  source.onerror = (error) => {
    if (!isCurrent()) return;
    if (source.readyState === EventSource.CLOSED) {
      // The browser has given up and will not retry on its own: a definite
      // failure, counted immediately.
      handleAlertStreamFailure();
      console.error('Alert stream closed by the browser; reconnecting with backoff.', error);
      scheduleAlertStreamReconnect();
      return;
    }
    // The browser is already retrying; this is a blip until proven otherwise.
    console.error('Alert stream interrupted. EventSource will retry automatically.', error);
    startAlertStreamOutageTimer();
  };
}

// Count a definite SSE failure (browser gave up, server reported a query error,
// or the stream went silent) and, past the threshold, fall back to polling.
function handleAlertStreamFailure() {
  alertStreamConsecutiveErrors += 1;
  if (alertStreamConsecutiveErrors < ALERT_SSE_MAX_ERRORS_BEFORE_FALLBACK) {
    return;
  }
  escalateAlertStreamOutage();
}

// Show an explicit retrying state instead of a permanent "Loading alerts..."
// and start polling so alerts still reach the operator.
function escalateAlertStreamOutage() {
  if (!alertFirstSnapshotReceived) {
    setAllAlertStatusLines('<strong>Unable to load alerts — retrying…</strong>');
  }
  startAlertFallbackPolling();
}


// Dismisses every active alert, or just the section named by alertType.
async function dismissAllAlerts(alertType = null) {
  if (alertActionRequestInFlight) return;

  const previousAlertsByType = cloneAlertsByType();
  const optimisticAlertsByType = cloneAlertsByType();
  if (alertType) {
    optimisticAlertsByType[alertType] = [];
  } else {
    Object.keys(optimisticAlertsByType).forEach((type) => {
      optimisticAlertsByType[type] = [];
    });
  }

  alertActionRequestInFlight = true;
  setAlertActionButtonsDisabled(true);

  await axios.post("clear-all/", alertType ? { alert_type: alertType } : {}, { timeout: ALERT_ACTION_TIMEOUT_MS })
    .then(response => {
      alertToastSuccess(response.data.message || 'Alerts dismissed.', 5000);
      // The server confirmed the clear, so its per-column totals are now zero
      // for everything we cleared. The theme/watch-mode/count logic trusts
      // alertTotalsByType over the (now empty) rendered list, so without this
      // the page stays in its yellow "active" state, with stale counts, until
      // the next SSE snapshot (up to ALERT_STREAM_POLL_SECONDS later) happens
      // to refresh the totals.
      const clearedTypes = alertType ? [alertType] : Object.keys(alertTotalsByType);
      const clearedTotals = Object.fromEntries(clearedTypes.map((type) => [type, 0]));
      alertTotalsByType = { ...alertTotalsByType, ...clearedTotals };
      renderAlertSnapshot({ alerts: optimisticAlertsByType });
    })
    .catch(error => {
      renderAlertSnapshot({ alerts: previousAlertsByType });
      console.error('Error:', error);
      alertToastError(`Error dismissing alerts: ${getAxiosErrorMessage(error)}`, 10000, 'dismiss-alerts-error');
    })
    .finally(() => {
      alertActionRequestInFlight = false;
      setAlertActionButtonsDisabled(false);
      updateAllSectionSelectionButtons();
    });
}

// Re-tick the given alerts (by id) in the current rows, e.g. to give the operator
// back a selection that was consumed by an action that then failed.
function reselectAlerts(uuids) {
  const wanted = new Set(uuids);
  document.querySelectorAll('input.checkbox-alert').forEach((checkbox) => {
    if (wanted.has(checkbox.dataset.alertUuid)) {
      checkbox.checked = true;
    }
  });
  updateAllSectionSelectionButtons();
}

// Dismiss or flag only the alerts the operator has checked.
async function handleAlerts(handleType) {
  if (alertActionRequestInFlight) return;

  const checkedCheckboxes = document.querySelectorAll('input[type="checkbox"].checkbox-alert:checked');
  const alert_uuids = Array.from(checkedCheckboxes).map(checkbox => checkbox.dataset.alertUuid || checkbox.id.replace('alert-', ''));

  if (alert_uuids.length === 0) {
    alertToastError('Select at least one alert first.', 5000);
    return;
  }

  const previousAlertsByType = cloneAlertsByType();
  const optimisticAlertsByType = cloneAlertsByType();
  const selectedUuids = new Set(alert_uuids);

  // The action consumes the selection. Rows are now rebuilt with the operator's
  // selection preserved (see renderAlertList), so a flagged alert -- which stays
  // on screen -- would otherwise remain ticked after "Flag Selected".
  checkedCheckboxes.forEach((checkbox) => {
    checkbox.checked = false;
  });
  alert_uuids.forEach((uuid) => {
    if (handleType === 'flagged') {
      pendingFlaggedAlertUuids.add(uuid);
      return;
    }
    pendingHandledAlertUuids.add(uuid);
  });

  Object.keys(optimisticAlertsByType).forEach((alertType) => {
    if (handleType === 'flagged') {
      optimisticAlertsByType[alertType] = optimisticAlertsByType[alertType].map((alert) => {
        if (!selectedUuids.has(getAlertUuid(alert))) return alert;
        return {
          ...alert,
          acknowledge: { ...(alert.acknowledge || {}), reason: 'flagged' },
        };
      });
      return;
    }

    optimisticAlertsByType[alertType] = optimisticAlertsByType[alertType]
      .filter((alert) => !selectedUuids.has(getAlertUuid(alert)));
  });

  renderAlertSnapshot({ alerts: optimisticAlertsByType });
  alertActionRequestInFlight = true;
  setAlertActionButtonsDisabled(true);

  await axios.post("handle-alerts/", {
    handle_type: handleType,
    uuids: alert_uuids
  }, { timeout: ALERT_ACTION_TIMEOUT_MS }).then(response => {
    alertToastSuccess(response.data.message || 'Alerts updated.', 5000);
    clearPendingAlertActions(alert_uuids);
  })
    .catch(error => {
      alert_uuids.forEach((uuid) => {
        pendingHandledAlertUuids.delete(uuid);
        pendingFlaggedAlertUuids.delete(uuid);
      });
      renderAlertSnapshot({ alerts: previousAlertsByType });
      // The action failed, so the operator still wants these alerts handled:
      // give back the selection that was consumed when the request started.
      reselectAlerts(alert_uuids);
      console.error('Error:', error);
      alertToastError(`Error handling alerts: ${getAxiosErrorMessage(error)}`, 10000, 'handle-alerts-error');
    })
    .finally(() => {
      alertActionRequestInFlight = false;
      setAlertActionButtonsDisabled(false);
      updateAllSectionSelectionButtons();
    });
}

// Event listeners for buttons and initial alert queries
document.addEventListener("DOMContentLoaded", () => {
  const dismissSelectedButton = document.getElementById('dismissSelectedButton');
  if (dismissSelectedButton) {
    dismissSelectedButton.addEventListener("click", () => {
      handleAlerts('dismissed');
    });
  }

  const flagAlertButton = document.getElementById('flagAlertButton');
  if (flagAlertButton) {
    flagAlertButton.addEventListener("click", () => {
      handleAlerts('flagged');
    });
  }

  const dismissAllButton = document.getElementById('dismissAllButton');
  if (dismissAllButton) {
    dismissAllButton.addEventListener("click", () => {
      dismissAllAlerts();
    });
  }

  document.querySelectorAll('[data-select-alert-section]').forEach((button) => {
    button.addEventListener('click', () => {
      toggleSectionAlertSelection(button.dataset.selectAlertSection);
    });
  });

  document.querySelectorAll('[data-dismiss-alert-section]').forEach((button) => {
    button.addEventListener('click', () => {
      dismissAllAlerts(button.dataset.dismissAlertSection);
    });
  });

  document.addEventListener('change', (event) => {
    if (!event.target.classList.contains('checkbox-alert')) return;
    updateSectionSelectionButton(event.target.dataset.alertType);
  });

  setInterval(() => {
    if (document.hidden) return;
    refreshMonitorHealth();
  }, 60000);

  initializeHealthToggleBehavior();
  initializeAlertStream();

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      closeAlertStream();
      return;
    }

    initializeAlertStream();
    refreshMonitorHealth();
  });

  window.addEventListener('beforeunload', closeAlertStream);

  refreshMonitorHealth();
  renderHealthLastUpdated();
  updateSelectedAlertCount();
});
