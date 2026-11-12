'use strict';

/*
 * Runs one of the webapp's browser scripts inside a jsdom window as a classic
 * <script>, so top-level functions become window globals. `export` keywords
 * (main.js is loaded as an ES module in the browser) are stripped, since a
 * classic script cannot parse them. The script keeps its real file name so
 * coverage is attributed to it.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

const WEBAPP = path.resolve(__dirname, '..');

function loadScript(relativePath, { html = '<!doctype html><body></body>', globals = {}, libs = [] } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('error', (...args) => errors.push(args));
  virtualConsole.on('jsdomError', (error) => errors.push([error]));
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://localhost/', virtualConsole });
  const { window } = dom;
  const context = dom.getInternalVMContext();
  const run = (source, filename) => new vm.Script(source, { filename }).runInContext(context);

  for (const lib of libs) {
    const file = path.join(WEBAPP, 'node_modules', lib);
    run(fs.readFileSync(file, 'utf8'), file);
  }
  for (const [name, value] of Object.entries(globals)) {
    Object.defineProperty(window, name, { value, configurable: true, writable: true });
  }

  const file = path.join(WEBAPP, relativePath);
  run(fs.readFileSync(file, 'utf8').replace(/^export /gm, ''), file);
  return { window, document: window.document, errors, eval: (expression) => run(expression, 'eval') };
}

module.exports = { loadScript };
