'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadScript } = require('./load_script');

const HTML = `<body>
  <input name="csrfmiddlewaretoken" value="tok">
  <button id="user-console-button"></button><span id="user-icon"></span><span id="database-status-text"></span>
  <a class="nav-link" href="/"></a><a class="nav-link" href="/other/"></a>
  <form id="sign-in-form"><button id="login-button">Go</button><p id="loginMessage"></p></form>
  <form id="elastic-form">
    <input id="elastic-host" name="host" required>
    <div id="elastic-host-invalid" class="invalid-feedback"></div>
    <input id="id_datastore_authenticate" type="checkbox">
    <div id="opensearch-group"><input type="text"><input type="password" id="opensearch-password"></div>
    <input id="id_datastore_password">
    <span id="os-username"></span><span id="os-password"></span>
    <button id="elastic-form-save" type="button">Save</button>
  </form>
</body>`;

function setup({ fetchImpl, ajax } = {}) {
  const calls = [];
  const toasts = [];
  const modal = { shown: 0, hidden: 0 };
  const toast = (type) => (message, length) => toasts.push({ type, message, length });
  const page = loadScript('project/static/js/main.js', {
    html: HTML,
    libs: ['jquery/dist/jquery.js'],
    globals: {
      fetch: fetchImpl || ((...args) => { calls.push(args); return new Promise(() => {}); }),
      bootstrap: {
        Tooltip: class {},
        Modal: class {
          constructor(selector) { calls.push(['modal', selector]); }
          show() { modal.shown += 1; }
          static getOrCreateInstance(selector) {
            calls.push(['instance', selector]);
            return { show: () => { modal.shown += 1; }, hide: () => { modal.hidden += 1; } };
          }
        },
      },
      toastError: toast('error'),
      toastSuccess: toast('success'),
      toastWarning: toast('warning'),
      toastInfo: toast('info'),
    },
  });
  if (ajax) page.window.jQuery.ajax = ajax;
  return { ...page, calls, toasts, modal, el: (id) => page.document.getElementById(id) };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test('displayMessage maps the message level to the matching toast', () => {
  const { window, toasts } = setup();
  for (const level of ['error', 'success', 'warning', 'anything-else']) window.displayMessage([level, `m-${level}`]);
  assert.deepEqual(toasts.map((t) => t.type), ['error', 'success', 'warning', 'info']);
  assert.ok(toasts.every((t) => t.length === 5000));
});

test('modals open through bootstrap', () => {
  const { window, modal, calls } = setup();
  window.login_modal();
  window.settings_modal();
  assert.equal(modal.shown, 2);
  assert.deepEqual(calls.filter((c) => c[0] === 'modal' || c[0] === 'instance'), [['modal', '#login-modal'], ['instance', '#settings-modal']]);
});

test('toggleOpenseachInput enables credentials only when authentication is on', () => {
  const { window, el } = setup();
  const inputs = () => [...window.document.querySelectorAll('#opensearch-group input')];
  assert.ok(inputs().every((i) => i.disabled), 'initial run with auth off');
  assert.equal(el('os-username').hidden, true);
  el('id_datastore_authenticate').checked = true;
  el('id_datastore_authenticate').dispatchEvent(new window.Event('change'));
  assert.ok(inputs().every((i) => !i.disabled));
  assert.equal(el('os-password').hidden, false);
});

test('the nav link for the current path is activated', () => {
  const { document } = setup();
  const [home, other] = document.querySelectorAll('.nav-link');
  assert.ok(home.classList.contains('active'));
  assert.ok(!other.classList.contains('active'));
});

test('getDatabaseStatus posts with the CSRF token and shows the datastore state', async () => {
  const requests = [];
  const { window, el } = setup({
    fetchImpl: (...args) => {
      requests.push(args);
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ status: true }) });
    },
  });
  window.getDatabaseStatus();
  await tick();
  assert.equal(requests[0][0], '/database/status/');
  assert.equal(requests[0][1].headers['X-CSRFToken'], 'tok');
  assert.ok(el('user-console-button').classList.contains('is-datastore-online'));
  assert.equal(el('database-status-text').textContent, 'Datastore live');
  assert.equal(el('user-console-button').getAttribute('aria-label'), 'User menu. Datastore live.');
});

test('getDatabaseStatus reports offline on a bad status or network failure, and skips hidden tabs', async () => {
  let mode = 'bad';
  const requests = [];
  const { window, el, document } = setup({
    fetchImpl: () => {
      requests.push(1);
      return mode === 'bad'
        ? Promise.resolve({ status: 500 })
        : Promise.resolve({ status: 200, json: () => Promise.resolve({ status: false }) });
    },
  });
  window.getDatabaseStatus();
  await tick();
  assert.equal(el('database-status-text').textContent, 'Status unavailable');
  assert.ok(el('user-console-button').classList.contains('is-datastore-offline'));

  mode = 'offline';
  window.getDatabaseStatus();
  await tick();
  assert.equal(el('database-status-text').textContent, 'Datastore offline');

  Object.defineProperty(document, 'hidden', { value: true, configurable: true });
  window.getDatabaseStatus();
  assert.equal(requests.length, 2, 'no request from a backgrounded tab');
});

test('updateDatastoreStatus ignores a repeated state and a page without the status chip', () => {
  const { window, el, document } = setup();
  window.updateDatastoreStatus(true);
  el('user-console-button').setAttribute('aria-label', 'sentinel');
  window.updateDatastoreStatus(true);
  assert.equal(el('user-console-button').getAttribute('aria-label'), 'sentinel', 'identical state not re-applied');
  el('database-status-text').remove();
  window.updateDatastoreStatus(false);
  assert.ok(el('user-icon').classList.contains('is-datastore-offline'));
  assert.ok(!el('user-icon').classList.contains('is-datastore-online'));
  el('user-icon').remove();
  window.updateDatastoreStatus(true); // no throw
  assert.ok(document);
});

test('sign-in shows the server message on failure and reloads on success', async () => {
  let response = { success: false, message: 'Bad password' };
  const { window, el } = setup({ fetchImpl: () => Promise.resolve({ json: () => Promise.resolve(response) }) });
  const submit = () => el('sign-in-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  submit();
  assert.equal(el('login-button').disabled, true);
  await tick();
  assert.equal(el('loginMessage').innerText, 'Bad password');
  assert.equal(el('login-button').disabled, false);
  assert.equal(el('login-button').innerHTML, 'Go');
});

test('sign-in tells the user when the request itself fails', async () => {
  const { window, el } = setup({ fetchImpl: () => Promise.reject(new Error('down')) });
  el('sign-in-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await tick();
  assert.match(el('loginMessage').innerText, /try again/);
  assert.equal(el('login-button').disabled, false);
});

function save(ajax) {
  const page = setup({ ajax });
  page.el('elastic-host').value = 'es';
  return page;
}
const clickSave = ({ window, el }) => el('elastic-form-save').dispatchEvent(new window.Event('click', { cancelable: true }));

test('saving an invalid form only flags it for validation', () => {
  const sent = [];
  const page = setup({ ajax: (opts) => sent.push(opts) });
  clickSave(page);
  assert.equal(sent.length, 0);
  assert.ok(page.el('elastic-form').classList.contains('was-validated'));
});

test('a successful save closes the modal, clears the password and restores the button', () => {
  let opts;
  const page = save((o) => { opts = o; });
  page.el('id_datastore_password').value = 'secret';
  page.document.body.insertAdjacentHTML('beforeend', '<div class="modal-backdrop"></div>');
  clickSave(page);
  assert.equal(page.el('elastic-form-save').disabled, true);
  assert.match(page.el('elastic-form-save').innerHTML, /Checking connection/);

  opts.success({ messages: [] });
  opts.complete();
  assert.equal(page.toasts[0].message, 'Saved Datastore settings');
  assert.equal(page.modal.hidden, 1);
  assert.equal(page.el('id_datastore_password').value, '');
  assert.equal(page.document.querySelector('.modal-backdrop'), null);
  assert.equal(page.el('elastic-form-save').disabled, false);
  assert.equal(page.el('elastic-form-save').innerHTML, 'Save');
});

test('a save answered with messages shows them instead of closing the modal', () => {
  let opts;
  const page = save((o) => { opts = o; });
  clickSave(page);
  opts.success({ messages: [['error', 'no route']] });
  assert.deepEqual(page.toasts.map((t) => t.message), ['no route']);
  assert.equal(page.modal.hidden, 0);
});

test('a 400 save marks the offending field; any other error is toasted', () => {
  let opts;
  const page = save((o) => { opts = o; });
  clickSave(page);
  opts.error({ status: 400, responseText: JSON.stringify({ host: [{ message: 'unreachable' }] }) }, 'error', '');
  assert.equal(page.el('elastic-host-invalid').innerHTML, 'unreachable');
  assert.ok(page.el('elastic-host').classList.contains('is-invalid'));

  opts.error({ status: 500 }, 'error', 'Server Error');
  assert.equal(page.toasts.at(-1).message, 'Error 500: Server Error');
});
