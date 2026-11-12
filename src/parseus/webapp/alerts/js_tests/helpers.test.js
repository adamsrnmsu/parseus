'use strict';

// Behaviour of the small alert-page helpers: error text, client ids, list
// signatures, reconciliation of optimistic actions, health rendering and the
// stream's event handlers.

const test = require('node:test');
const assert = require('node:assert/strict');
const { createPage, makeAlert } = require('./harness');

test('getAxiosErrorMessage prefers timeout, then server error, message, error text', async () => {
  const { window } = await createPage();
  const msg = (error) => window.getAxiosErrorMessage(error);
  assert.equal(msg({ code: 'ECONNABORTED' }), 'The server did not respond in time.');
  assert.equal(msg({ code: 'ETIMEDOUT' }), 'The server did not respond in time.');
  assert.equal(msg({ response: { data: { error: 'bad', message: 'm' } } }), 'bad');
  assert.equal(msg({ response: { data: { message: 'm' } } }), 'm');
  assert.equal(msg({ message: 'boom' }), 'boom');
  assert.equal(msg('plain'), 'plain');
  assert.equal(msg(undefined), 'Unknown error');
});

test('getStableClientAlertId uses the uuid, else a remembered generated id', async () => {
  const { window } = await createPage();
  const id = window.getStableClientAlertId;
  assert.equal(id(makeAlert(1), 'device', 0), 'u1');

  const noUuid = { '@timestamp': 't', event: { original: 'o' } };
  const first = id(noUuid, 'device', 0);
  assert.match(first, /^client-alert-/);
  assert.equal(id(noUuid, 'device', 5), first, 'same alert keeps its id');
  assert.notEqual(id({ '@timestamp': 't2', event: { original: 'o' } }, 'device', 0), first);
  // No timestamp/event at all: keyed by index.
  assert.equal(id({}, 'device', 3), id({}, 'device', 3));
  assert.notEqual(id({}, 'device', 3), id({}, 'device', 4));
});

test('getStableClientAlertId falls back when crypto.randomUUID is unavailable', async () => {
  const { window } = await createPage();
  Object.defineProperty(window, 'crypto', { value: undefined, configurable: true });
  const generated = window.getStableClientAlertId({ '@timestamp': 'x' }, 'device', 0);
  assert.match(generated, /^client-alert-\d+-/);
});

test('getAlertListSignature changes with any shown field and tolerates sparse alerts', async () => {
  const { window } = await createPage();
  const sig = (alerts) => window.getAlertListSignature(alerts);
  assert.equal(sig(undefined), '[]');
  assert.equal(sig([{}]), '[{"uuid":null,"timestamp":null,"agentType":null,"original":null,"reason":null}]');
  const base = makeAlert(1);
  assert.notEqual(sig([base]), sig([{ ...base, acknowledge: { reason: 'flagged' } }]));
  assert.notEqual(sig([base]), sig([{ ...base, agent: { type: 'Other' } }]));
  assert.equal(sig([base]), sig([{ ...base }]));
});

test('reconcilePendingAlertActions stops suppressing only once the server agrees', async () => {
  const page = await createPage();
  const handled = page.get('pendingHandledAlertUuids');
  const flagged = page.get('pendingFlaggedAlertUuids');
  const reconcile = (snapshot) => page.window.reconcilePendingAlertActions(snapshot);

  reconcile({ device: [makeAlert(1)] }); // nothing pending: no-op
  handled.add('u1');
  handled.add('u2');
  flagged.add('u3');
  flagged.add('u4');

  reconcile({
    device: [
      makeAlert(1), // still present: handled stays pending
      makeAlert(3, { acknowledge: { reason: 'flagged' } }), // flag confirmed
      makeAlert(4), // not flagged yet
      { event: {} }, // no uuid: ignored
      null,
    ],
    other: null,
  });

  assert.deepEqual([...handled], ['u1']);
  assert.deepEqual([...flagged], ['u4']);
  reconcile(undefined);
  assert.equal(handled.size, 0, 'absent from the server snapshot means the removal landed');
});

test('updateHealthToggleText follows the collapse state', async () => {
  const page = await createPage();
  const { document, window } = page;
  window.updateHealthToggleText(true); // elements absent: no-op, no throw
  document.body.insertAdjacentHTML('beforeend',
    '<button id="monitorHealthToggle"></button><span id="monitorHealthToggleText"></span><i id="monitorHealthToggleIcon"></i>');
  window.updateHealthToggleText(true);
  assert.equal(page.byId('monitorHealthToggleText').textContent, 'Hide Monitor Status');
  assert.equal(page.byId('monitorHealthToggleIcon').className, 'bi bi-chevron-up me-2');
  assert.equal(page.byId('monitorHealthToggle').getAttribute('aria-expanded'), 'true');
  window.updateHealthToggleText(false);
  assert.equal(page.byId('monitorHealthToggleText').textContent, 'Show Monitor Status');
  assert.equal(page.byId('monitorHealthToggleIcon').className, 'bi bi-chevron-down me-2');
  assert.equal(page.byId('monitorHealthToggle').getAttribute('aria-expanded'), 'false');
});

test('renderHealthStatus paints the LED, label and message, and skips unchanged health', async () => {
  const page = await createPage();
  const { window } = page;
  window.renderHealthStatus('missing', { status: 'ok' }); // no elements: no-op

  const health = { status: 'warning', message: 'slow', checked_at: 'now' };
  window.renderHealthStatus('logstash', health);
  assert.equal(page.byId('healthLed-logstash').className, 'status-led status-warning');
  assert.equal(page.byId('healthStatusLabel-logstash').textContent, 'WARNING');
  assert.equal(page.byId('healthMessage-logstash').textContent, 'slow');
  assert.equal(page.byId('healthLed-logstash').getAttribute('data-bs-html'), 'true');

  page.byId('healthMessage-logstash').textContent = 'edited';
  window.renderHealthStatus('logstash', health);
  assert.equal(page.byId('healthMessage-logstash').textContent, 'edited', 'identical health is not re-rendered');

  window.renderHealthStatus('logstash', { status: 'ok' });
  assert.equal(page.byId('healthMessage-logstash').textContent, 'No status message available');
});

test('renderHealthStatus replaces an existing tooltip and sizes the disk bar', async () => {
  const page = await createPage();
  let disposed = 0;
  Object.defineProperty(page.window.bootstrap.Tooltip, 'getInstance', {
    value: () => ({ dispose: () => { disposed += 1; } }),
  });
  const disk = (percent) => ({ status: 'critical', message: 'full', disk: { path: '/', used_percent: percent } });

  page.window.renderHealthStatus('disk', disk(42));
  assert.equal(disposed, 1);
  assert.equal(page.byId('diskUsageFill').style.width, '42%');
  assert.equal(page.byId('diskUsageFill').className, 'disk-usage-fill status-critical');
  page.window.renderHealthStatus('disk', disk(250));
  assert.equal(page.byId('diskUsageFill').style.width, '100%');
  page.window.renderHealthStatus('disk', disk('junk'));
  assert.equal(page.byId('diskUsageFill').style.width, '0%');
});

test('stream events: snapshots render, heartbeats tune the watchdog, stale sources are ignored', async () => {
  const page = await createPage();
  page.start();
  const [source] = page.liveSources();

  source.emit('heartbeat', { interval_seconds: 7 });
  assert.equal(page.get('alertStreamHeartbeatSeconds'), 7);
  source.emit('heartbeat', { interval_seconds: -1 });
  assert.equal(page.get('alertStreamHeartbeatSeconds'), 7, 'invalid interval is ignored');
  source.listeners.heartbeat[0]({ data: 'not json' }); // must not throw

  source.emit('alert_snapshot', { alerts: { scpi_range: [makeAlert(1)], device: [] } });
  assert.equal(page.get('alertFirstSnapshotReceived'), true);
  assert.ok(page.checkbox('u1'));
  source.listeners.alert_snapshot[0]({ data: 'not json' }); // logged, not thrown

  source.emit('alert_error', {});
  assert.ok(page.toasts.some((toast) => toast.type === 'error'));

  // A replaced connection's events do nothing.
  source.closed = true;
  page.window.openAlertStream();
  const errorsBefore = page.toasts.length;
  source.emit('alert_error', {});
  source.emit('alert_snapshot', { alerts: { scpi_range: [], device: [] } });
  source.emit('heartbeat', { interval_seconds: 99 });
  source.fail(1);
  assert.equal(page.toasts.length, errorsBefore);
  assert.equal(page.get('alertStreamHeartbeatSeconds'), 7);
  assert.ok(page.checkbox('u1'));
});

test('renderAlertList flags rows, notes a capped list, keeps focus and clears to an empty state', async () => {
  const page = await createPage();
  const head = page.byId('rangeAlertScreen');
  const flagged = makeAlert(2, { acknowledge: { reason: 'flagged' } });
  const sparse = { event: { uuid: 'u9' }, '@timestamp': 't' };
  page.snapshot({ scpi_range: [makeAlert(1), flagged, sparse] }, { scpi_range: 10 });

  assert.equal(page.byId('rangeAlertCount').textContent, '10', 'count shows the true total');
  assert.equal(head.textContent, 'Showing the 3 newest of 10.');
  const card = (uuid) => page.checkbox(uuid).closest('label');
  assert.equal(card('u2').className, 'alert-card flagged');
  assert.equal(card('u1').className, 'alert-card');
  assert.ok(card('u9').textContent.includes('Alert:'));
  assert.ok(card('u9').textContent.includes('No alert details available'));

  page.checkbox('u1').focus();
  page.snapshot({ scpi_range: [makeAlert(0), makeAlert(1)] }, { scpi_range: 2 });
  assert.equal(head.textContent, '');
  assert.equal(page.document.activeElement, page.checkbox('u1'), 'focus follows the alert across a rebuild');

  page.snapshot({ scpi_range: [] }, { scpi_range: 0 });
  assert.equal(page.checkbox('u1'), null);
  assert.equal(page.byId('rangeAlertCount').textContent, '0');
  assert.ok(page.byId('scpi_range_alerts').classList.contains('no-alerts'));
});
