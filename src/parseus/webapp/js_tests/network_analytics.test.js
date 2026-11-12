'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadScript } = require('./load_script');

const HTML = `<body>
  <input id="train_start"><input id="train_end"><input id="train_ip_filter">
  <div id="train_dates"><input class="flatpickr"><input class="flatpickr"></div>
  <input type="checkbox" name="train_box" value="a"><input type="checkbox" name="train_box" value="b">
  <button id="train_btn"></button>
  <input id="predict_start"><input id="predict_end"><input id="predict_ip_filter">
  <div id="predict_dates"><input class="flatpickr"></div>
  <button name="predict_model" class="model-btn" value="m1"></button>
  <button id="predict_btn"></button>
</body>`;

function setup() {
  const flatpickrCalls = [];
  const page = loadScript('analytics/static/js/network_analytics.js', {
    html: HTML,
    globals: { flatpickr: (selector, options) => flatpickrCalls.push({ selector, options }) },
  });
  return { ...page, flatpickrCalls, api: page.window.network_analytics, el: (id) => page.document.getElementById(id) };
}

test('isIP accepts dotted IPv4 and IPv6 literals and rejects everything else', () => {
  const { window } = setup();
  for (const good of ['10.0.0.1', '255.255.255.255', '0.0.0.0', '::1', 'fe80::1']) {
    assert.equal(window.isIP(good), true, good);
  }
  for (const bad of ['', null, '10.0.0', '10.0.0.256', '10.0.0.01', '10.0.0.a', 'host', 'zz::zz', '1:2:3:4:5:6:7:8:9']) {
    assert.equal(window.isIP(bad), false, String(bad));
  }
});

test('ip_filter_validation marks only non-empty, non-IP input invalid', () => {
  const { window, el } = setup();
  const input = el('train_ip_filter');
  for (const [value, invalid] of [['nope', true], ['', false], ['10.1.1.1', false], ['bad', true]]) {
    input.value = value;
    window.ip_filter_validation(input);
    assert.equal(input.classList.contains('invalid'), invalid, value);
  }
});

test('button_validation disables the button for any invalid input or nothing selected', () => {
  const { window, document } = setup();
  const make = (invalid) => {
    const e = document.createElement('input');
    if (invalid) e.classList.add('invalid');
    return e;
  };
  const btn = document.createElement('button');
  const run = (s, e, ip, checked) => {
    window.button_validation(make(s), make(e), make(ip), btn, checked);
    return btn.disabled;
  };
  assert.equal(run(false, false, false, ['x']), false);
  assert.equal(run(true, false, false, ['x']), true);
  assert.equal(run(false, true, false, ['x']), true);
  assert.equal(run(false, false, true, ['x']), true);
  assert.equal(run(false, false, false, []), true);
});

test('train listeners keep date, IP and selection validation in step with the button', () => {
  const { api, el, document, window } = setup();
  api.setNetworkTrainListeners();
  const fire = (node, type) => node.dispatchEvent(new window.Event(type));
  const [boxA] = document.querySelectorAll('input[name=train_box]');

  assert.equal(el('train_btn').disabled, false);
  api.train_button_validation();
  assert.equal(el('train_btn').disabled, true, 'nothing ticked');
  boxA.checked = true;
  fire(boxA, 'change');
  assert.equal(el('train_btn').disabled, false);

  el('train_start').value = '2026-02-01';
  el('train_end').value = '2026-01-01';
  fire(el('train_end'), 'input');
  assert.ok([...document.querySelectorAll('#train_dates .flatpickr')].every((e) => e.classList.contains('invalid')));
  assert.equal(el('train_btn').disabled, false, 'the hidden date inputs are what carry the invalid mark');
  el('train_start').classList.add('invalid');
  fire(el('train_start'), 'input'); // start > end still, re-validates dates
  assert.equal(el('train_btn').disabled, true);

  el('train_start').value = '2026-01-01';
  fire(el('train_start'), 'input');
  assert.ok([...document.querySelectorAll('#train_dates .flatpickr')].every((e) => !e.classList.contains('invalid')));

  el('train_ip_filter').value = 'bad';
  fire(el('train_ip_filter'), 'input');
  assert.ok(el('train_ip_filter').classList.contains('invalid'));
  assert.equal(el('train_btn').disabled, true);
});

test('predict listeners validate dates, IP filter and model selection', () => {
  const { api, el, document, window } = setup();
  api.setNetworkPredictListeners();
  const fire = (node, type) => node.dispatchEvent(new window.Event(type));
  const model = document.querySelector('button[name=predict_model]');

  fire(model, 'click');
  assert.equal(el('predict_btn').disabled, true, 'no model active');
  model.classList.add('active');
  fire(model, 'click');
  assert.equal(el('predict_btn').disabled, false);

  el('predict_start').value = '2026-02-01';
  el('predict_end').value = '2026-01-01';
  fire(el('predict_start'), 'input');
  assert.ok(document.querySelector('#predict_dates .flatpickr').classList.contains('invalid'));
  fire(el('predict_end'), 'input');

  el('predict_ip_filter').value = '1.2.3';
  fire(el('predict_ip_filter'), 'input');
  assert.ok(el('predict_ip_filter').classList.contains('invalid'));
  assert.equal(el('predict_btn').disabled, true);
});

test('initializeFlatpickr configures every .flatpickr input for 24h date-time entry', () => {
  const { api, flatpickrCalls } = setup();
  api.initializeFlatpickr();
  assert.equal(flatpickrCalls[0].selector, '.flatpickr');
  assert.equal(flatpickrCalls[0].options.enableTime, true);
  assert.equal(flatpickrCalls[0].options.time_24hr, true);
});
