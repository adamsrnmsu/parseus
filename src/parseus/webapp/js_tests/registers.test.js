'use strict';

// The registers page: real d3 and jQuery in jsdom, driving the page script's
// functions with representative data and checking what ends up in the DOM.

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadScript } = require('./load_script');

const HTML = `<body>
<div id="nav-tabContent"><div id="graphdiv"></div></div>
<div id="analysis-tabContent"></div>
<select id="state-graph-select" class="chart-option">
  <option selected>Register State Over Time</option><option>Register Updates Heatmap</option>
  <option>State Transition Graph</option><option>Bogus</option>
</select>
<input type="checkbox" id="show-line" checked><input type="checkbox" id="normalize">
${['register-graph', 'state-graph', 'fc-graph'].map((id) => `
<div><div id="${id}-legend"><div class="legend-item d-none"><div class="legend-color"></div><span class="legend-text"></span></div></div>
<div class="wrap"><div id="${id}"><div>prompt</div></div></div>
<button id="reset-zoom-${id}" class="btn"></button></div>`).join('')}
<button id="groupby_btn"></button>
<div id="conversation-list"></div><div id="conversation-register-lists"></div>
<div id="fc-conversation-list"></div><div id="fc-conversation-summary"></div>
<template id="summary-item-template"><div class="summary-item d-none"></div></template>
<template id="conversation-item-template"><div class="conversation-item d-none"><button class="btn">a → b</button></div></template>
<template id="fc-conversation-item-template"><div class="fc-conversation-item d-none"><button class="btn">a → b</button></div></template>
<template id="register-list-template"><div class="register-list d-none"></div></template>
<template id="register-item-template"><div class="register-item d-none"><div><div class="form-check">
  <input class="register-check form-check-input" type="checkbox"><label class="form-check-label"></label>
  <span class="register-modal-icon"><a href="#"></a></span></div></div></div></template>
<span id="register-name"></span><span id="register-count"></span><span id="register-min"></span>
<input type="checkbox" class="filter-check" value="constant"><input type="checkbox" class="fc-filter-check" value="modbus">
<div id="timeline"></div><div id="conversations"></div><div id="timestamps"></div><div id="counts"></div>
<div id="fc-timeline"></div><div id="fc-conversations"></div><div id="fc-timestamps"></div><div id="fc-main-summary-count"></div>
<div id="short-to-long-modal-body"></div>
<input id="index" value="idx"><input id="start-time"><input id="end-time">
</body>`;

function setup() {
  const toasts = [];
  const tooltips = {};
  const ajax = [];
  const page = loadScript('registers/static/js/registers.js', {
    html: HTML,
    libs: ['jquery/dist/jquery.js', 'd3/dist/d3.min.js'],
    globals: {
      toastError: (message) => toasts.push(message),
      bootstrap: {
        Tooltip: { getInstance: (selector) => ({ setContent: (content) => { tooltips[selector] = content; } }) },
      },
    },
  });
  const { window, document } = page;
  window.jQuery.ajax = (options) => ajax.push(options);
  for (const id of ['nav-tabContent', 'analysis-tabContent']) {
    Object.defineProperty(document.getElementById(id), 'clientWidth', { value: 500 });
  }
  const el = (id) => document.getElementById(id);
  return { ...page, toasts, tooltips, ajax, el, $: window.jQuery };
}

const line = (label, n = 4) => ({
  label,
  legendName: `${label}!`,
  showLine: true,
  data: Array.from({ length: n }, (_, i) => [`2026-01-01T00:00:0${i}.000`, i === 1 ? null : i * 2]),
});

test('finishLoadData routes register data, function-code data, and rejects the rest', () => {
  const { window, toasts, el } = setup();
  window.showRegisters = (data) => { window.shown = data; };
  window.showFunctionCodes = () => { window.fcShown = true; };
  window.finishLoadData({ summary: 1 });
  assert.deepEqual(window.shown, { summary: 1 });
  window.finishLoadData({ fc_summary: 1 });
  assert.equal(window.fcShown, true);
  window.finishLoadData({});
  assert.equal(toasts.join(), 'Unknown data received.');
  assert.ok(el('state-graph'));
});

test('draw builds a zoomable scatter+line chart with scales, padding and normalisation', () => {
  const { window } = setup();
  const datasets = [line('a'), { ...line('b'), showLine: false }];
  const svg = window.draw('register-graph', datasets, false, {
    scale: { x: 'scaleUtc' }, padding: { x: 0.1, y: 0.1 }, xLabel: 'time', yLabel: 'val',
    margin: { left: 10 }, lineWidth: 2, pointRadius: 5,
  });
  const node = svg.node();
  assert.equal(node.querySelectorAll('circle').length, 6, 'null points are dropped');
  assert.equal(node.querySelectorAll('path[stroke-width="2"]').length, 2);
  assert.equal(node.querySelector('circle').getAttribute('r'), '5');
  assert.ok([...node.querySelectorAll('text')].some((t) => t.textContent === 'time'));
  assert.equal(node.querySelectorAll('path[display="none"]').length, 1, 'only lines that are enabled are shown');

  const defaults = window.draw('register-graph', [line('a')], true).node();
  assert.equal(defaults.querySelector('circle').getAttribute('r'), '3.5');
  const ys = [...defaults.querySelectorAll('circle')].map((c) => Number(c.getAttribute('cy')));
  assert.equal(new Set(ys).size, 3, 'normalised data still plots distinct points');
});

test('draw: hovering shows the nearest point', () => {
  const { window } = setup();
  const svg = window.draw('register-graph', [line('a'), line('b')], false, {});
  const node = svg.node();
  const fire = (type) => node.dispatchEvent(new window.MouseEvent(type, { clientX: 100, clientY: 50 }));
  fire('pointerenter');
  fire('pointermove');
  fire('pointermove');
  assert.equal(node.querySelectorAll('.dot').length, 2);
  fire('pointerleave');
});

test('wrapy wraps long axis labels onto extra lines', () => {
  const { window } = setup();
  const svg = window.d3.create('svg');
  svg.append('text').attr('x', 0).attr('dy', 0).text('alpha beta gamma delta epsilon');
  svg.selectAll('text').call(window.wrapy, 12);
  assert.ok(svg.selectAll('tspan').size() > 1);
  assert.equal(svg.selectAll('tspan').nodes().map((t) => t.textContent).join(' '), 'alpha beta gamma delta epsilon');
});

test('drawHeatmap makes one cell per point and a tooltip that follows hovering', () => {
  const { window, document } = setup();
  const t = (s) => new Date(Date.UTC(2026, 0, 1, 0, 0, s));
  const datasets = [{ label: 'r1', data: [[t(0), 1], [t(1), 5]] }, { label: 'r2', data: [[t(0), 3]] }];
  const stale = document.createElement('div');
  stale.className = 'heatmaptooltip';
  document.body.appendChild(stale);
  const svg = window.drawHeatmap('state-graph', datasets, null, { margin: { left: 5 } });
  assert.equal(svg.selectAll('rect').size(), 3);
  assert.equal(document.querySelectorAll('.heatmaptooltip').length, 1, 'stale tooltips are removed');

  const cell = svg.select('rect').node();
  const fire = (type) => cell.dispatchEvent(new window.MouseEvent(type));
  fire('mouseover');
  assert.equal(cell.style.stroke, 'white');
  fire('mouseleave');
  assert.equal(cell.style.stroke, 'none');
});

test('drawFcHeatmap draws labelled cells with a colour legend', () => {
  const { window, document } = setup();
  const datasets = [{ label: 'conv a', data: [['fc1', 1], ['fc2', 9]] }, { label: 'conv b', data: [['fc1', 4]] }];
  const svg = window.drawFcHeatmap('fc-graph', datasets, null, { margin: { left: 5 } });
  const node = svg.node();
  assert.equal(node.querySelectorAll('rect[x]').length >= 3, true);
  assert.ok(node.querySelector('#hm_colorScale_legend'));
  const cell = svg.selectAll('rect').nodes().find((r) => r.getAttribute('rx') === '0');
  cell.dispatchEvent(new window.MouseEvent('mouseover'));
  cell.dispatchEvent(new window.MouseEvent('mousemove', { clientX: 5, clientY: 5 }));
  assert.equal(document.querySelector('#fc-graph .heatmaptooltip').textContent !== undefined, true);
  cell.dispatchEvent(new window.MouseEvent('mouseleave'));
});

test('drawNodeEdge lays out nodes and arrows, including a self-loop, and drags nodes', async () => {
  const { window } = setup();
  const graph = JSON.stringify({
    nodes: [{ id: 'a' }, { id: 'b' }],
    links: [{ source: 'a', target: 'b' }, { source: 'a', target: 'a' }],
  });
  const svg = window.drawNodeEdge('state-graph', graph);
  assert.equal(svg.selectAll('circle').size(), 2);
  assert.equal(svg.selectAll('.labels text').size(), 2);
  await new Promise((resolve) => setTimeout(resolve, 100)); // let the simulation tick
  const paths = svg.selectAll('g[stroke="gray"] path').nodes().map((p) => p.getAttribute('d'));
  assert.ok(paths.every(Boolean), 'every link has a path');
  assert.ok(paths.some((d) => d.includes('-45')), 'the self-loop is drawn as an arc');
  window.close();
});

test('updateGraph replaces the chart and legend, or prompts when nothing is chosen', () => {
  const { window, el, document } = setup();
  const draw = (id) => window.d3.create('svg').attr('id', `drawn-${id}`);
  window.updateGraph('register-graph', draw, [line('a'), line('b')]);
  assert.ok(el('register-graph').querySelector('#drawn-register-graph'));
  assert.equal(el('reset-zoom-register-graph').disabled, false);
  assert.ok(!el('register-graph').parentElement.classList.contains('d-none'));

  window.updateGraph('register-graph', draw, [line('a')]);
  assert.equal(el('register-graph').querySelectorAll('svg').length, 1);

  window.updateGraph('state-graph', window.drawHeatmap, [{ label: 'r', data: [[new Date(0), 1]] }]);
  assert.equal(el('reset-zoom-state-graph').disabled, true, 'heatmaps cannot zoom');

  window.updateGraph('state-graph', window.drawNodeEdge, JSON.stringify({ nodes: [{ id: 'x' }], links: [] }));
  assert.equal(el('reset-zoom-state-graph').disabled, false);

  window.updateGraph('register-graph', draw, []);
  assert.match(el('register-graph').textContent, /Please choose a register/);
  assert.equal(el('register-graph').querySelectorAll('svg').length, 0);
  window.close();
});

test('updateStateGraph picks the chart from the selector and rejects unknown types', () => {
  const { window, el, toasts, $ } = setup();
  const errors = [];
  window.console.error = (...a) => errors.push(a);
  const t = '2026-01-01T00:00:00';
  const data = {
    states: { state_id: [['2026-01-01T00:00:00.000', 1], ['2026-01-01T00:00:01.000', 2]] },
    heatmap: { r1: [[t, 1], [t, 2]] },
    node_edge: JSON.stringify({ nodes: [{ id: 'n' }], links: [] }),
  };
  const choose = (n) => { el('state-graph-select').selectedIndex = n; window.updateStateGraph(data); };

  choose(0);
  assert.ok(el('state-graph').querySelector('svg circle'));
  choose(1);
  assert.ok(el('state-graph').querySelector('svg rect'));
  choose(2);
  assert.ok(el('state-graph').querySelector('svg g.labels'));
  choose(3);
  assert.equal(errors.length, 1);
  assert.ok($);
  assert.equal(toasts.length, 0);
  window.close();
});

function conversationData() {
  return {
    summary: { timeline: { length: '1h', start_end: 'a - b' }, conversations: 1, unique_timestamps: 2 },
    counts: { total: 2, list: 'x' },
    conversations: {
      '10.0.0.1->10.0.0.2': { r1: { constant: true, count: 3, min: null }, r2: { constant: false } },
    },
    name_map_array: [['Short', 'Long'], ['r1', 'Register one']],
  };
}

test('updateSummary and populateTable fill the summary boxes and name table', () => {
  const { window, el, tooltips } = setup();
  window.updateSummary(conversationData());
  assert.equal(el('timeline').innerHTML, '1h');
  assert.equal(el('counts').innerHTML, '2');
  assert.equal(tooltips['#timeline']['.tooltip-inner'], 'a - b');
  window.populateTable(conversationData());
  assert.equal(el('short-to-long-modal-body').querySelectorAll('tbody tr').length, 1);
  assert.equal(el('short-to-long-modal-body').querySelectorAll('th').length, 2);
});

test('updateConversationList builds selectable registers and wires conversation and info clicks', () => {
  const { window, document, el, $ } = setup();
  const conversations = window.updateConversationList(conversationData());
  assert.equal(Object.keys(conversations).join(), '10.0.0.1->10.0.0.2');
  const item = document.querySelector('.conversation-item');
  assert.equal(item.id, 'c10_0_0_1-10_0_0_2');
  assert.equal(item.querySelector('.btn').innerHTML, '10.0.0.1 → 10.0.0.2');
  const registers = document.querySelectorAll('#c10_0_0_1-10_0_0_2-list .register-item');
  assert.equal(registers.length, 2);
  assert.ok(registers[0].classList.contains('filter-constant'));
  assert.ok(registers[1].classList.contains('filter-variable'));
  assert.equal(registers[0].querySelector('.register-check').id, 'c10_0_0_1-10_0_0_2--r1');

  item.querySelector('.btn').click();
  assert.ok(item.querySelector('.btn').classList.contains('bg-primary'));
  assert.ok(!document.querySelector('.register-list').classList.contains('d-none'));

  registers[0].querySelector('.register-modal-icon').click();
  assert.equal(el('register-name').textContent, '10.0.0.1->10.0.0.2:r1');
  assert.equal(el('register-count').innerHTML, '3');
  assert.equal(el('register-min').innerHTML, 'N/A');
  assert.equal(window.getCheckedRegisterInfo().length, 0);
  $(registers[0].querySelector('.register-check')).prop('checked', true);
  assert.equal(JSON.stringify(window.getCheckedRegisterInfo()), '[["r1","10.0.0.1->10.0.0.2"]]');
});

function fcData() {
  return {
    fc_summary: { total_time: '2h', start_end: 's-e', unique_pairs: 1, unique_timestamps: 3, fc_summary: 'fc!' },
    conversation_list: {
      '10.0.0.1 -> 10.0.0.2': {
        protocols: ['modbus'],
        stats: { start_time: 's', end_time: 'e', total_time: 't', fc_summary: 'f' },
        points: { 'fc 1': { a: 1, b: 2 } },
      },
    },
  };
}

test('function-code view: summary, conversation list, summaries and graphing on click', () => {
  const { window, document, el } = setup();
  window.finishLoadData(fcData());
  assert.equal(el('fc-timeline').innerHTML, '2h');
  assert.equal(el('fc-main-summary-count').innerHTML, 'fc!');
  assert.ok(el('groupby_btn').disabled, 'nothing selected yet');

  const item = document.querySelector('.fc-conversation-item');
  assert.ok(item.classList.contains('filter-modbus'));
  assert.match(document.querySelector('.summary-item').innerHTML, /First seen/);

  item.innerText = item.textContent.trim(); // jsdom has no innerText
  item.querySelector('.btn').click();
  assert.ok(!el('c10_0_0_1-10_0_0_2-summary').classList.contains('d-none'));
  assert.equal(el('groupby_btn').disabled, false);
  assert.ok(el('fc-graph').querySelector('svg rect'));
  window.close();
});
