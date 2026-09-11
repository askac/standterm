'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { allowedNavigation } = require('../policy.cjs');

const origin = 'http://127.0.0.1:64487';
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  const consent = deferred(), read = deferred(), notices = [], scripts = [], callbacks = [];
  const state = { focused: true, visible: true, minimized: false, destroyed: false, loading: false,
    url: origin + '/', target: 'fixture-request', prompts: 0, reads: 0, delivered: true };
  const win = { isDestroyed: () => state.destroyed, isVisible: () => state.visible, isMinimized: () => state.minimized };
  let handler;
  const contents = new EventEmitter();
  const frame = { isDestroyed: () => state.destroyed, executeJavaScript: async code => {
    scripts.push(code);
    if (code.includes('completeContextPaste')) return state.delivered;
    if (code.includes(' === ')) return state.target === 'fixture-request';
    return state.target;
  } };
  Object.assign(contents, { mainFrame: frame, isDestroyed: () => state.destroyed, getURL: () => state.url,
    isLoadingMainFrame: () => state.loading, session: { setPermissionRequestHandler: fn => { handler = fn; } } });
  const electron = { BrowserWindow: { getFocusedWindow: () => state.focused ? win : null },
    dialog: { showMessageBox: () => { state.prompts++; return consent.promise; } },
    clipboard: { readText: () => { state.reads++; return read.promise; } } };
  const api = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'context-paste.cjs'), 'utf8'), {
    require: name => name === 'electron' ? electron : { allowedNavigation }, module: api,
  });
  api.exports.installContextPaste(win, contents, origin, async value => notices.push(value));
  return { state, consent, read, contents, scripts, callbacks, notices,
    request: (permission = 'clipboard-read', requester = contents, details = { isMainFrame: true, requestingUrl: state.url }) =>
      new Promise(resolve => handler(requester, permission, value => { callbacks.push(value); resolve(value); }, details)),
    navigate: () => contents.emit('did-start-navigation', {}, origin + '/', false, true),
  };
}

test('one explicit native confirmation reads text once without granting renderer permission', async () => {
  const f = fixture();
  const result = f.request();
  await tick();
  assert.equal(f.state.prompts, 1);
  assert.equal(f.state.reads, 0);
  assert.equal(await f.request(), false, 'a duplicate request must not prompt');
  f.consent.resolve({ response: 1 });
  await tick();
  assert.equal(f.state.reads, 1);
  const text = 'fixture\n";throw new Error("not code");\n';
  f.read.resolve(text);
  assert.equal(await result, false);
  assert.ok(f.scripts.at(-1).includes(JSON.stringify(text)));
  assert.deepEqual(f.callbacks, [false, false]);
  assert.equal(f.notices.length, 0);
});

test('denied origins, subframes, other contents and background requests never prompt or read', async () => {
  for (const change of [s => { s.focused = false; }, s => { s.visible = false; }, s => { s.minimized = true; },
    s => { s.destroyed = true; }, s => { s.loading = true; }, s => { s.url = 'https://example.com/'; }]) {
    const f = fixture(); change(f.state);
    assert.equal(await f.request(), false);
    assert.equal(f.state.prompts + f.state.reads, 0);
  }
  const f = fixture();
  for (const args of [['media'], ['clipboard-read', {}],
    ['clipboard-read', f.contents, { isMainFrame: false, requestingUrl: origin }],
    ['clipboard-read', f.contents, { isMainFrame: true, requestingUrl: 'https://example.com' }]]) {
    assert.equal(await f.request(...args), false);
  }
  assert.equal(f.state.prompts + f.state.reads, 0);
});

test('cancel, stale terminal, navigation and lost focus cannot deliver clipboard text', async () => {
  for (const phase of ['cancel', 'target', 'navigation-dialog', 'navigation-read', 'focus-read', 'destroy-read', 'frame-read']) {
    const f = fixture(); const result = f.request(); await tick();
    if (phase === 'target') f.state.target = null;
    if (phase === 'navigation-dialog') f.navigate();
    f.consent.resolve({ response: phase === 'cancel' ? 0 : 1 });
    await tick();
    if (phase === 'navigation-read') f.navigate();
    if (phase === 'focus-read') f.state.focused = false;
    if (phase === 'destroy-read') f.state.destroyed = true;
    if (phase === 'frame-read') f.contents.mainFrame = {};
    f.read.resolve('private fixture');
    assert.equal(await result, false);
    assert.ok(!f.scripts.some(code => code.includes('completeContextPaste')), phase);
    assert.deepEqual(f.callbacks, [false]);
  }
});

test('renderer failures deny exactly once, report no private data and clear pending state', async () => {
  const f = fixture();
  f.contents.mainFrame.executeJavaScript = async () => { throw new Error('private fixture'); };
  assert.equal(await f.request(), false);
  assert.equal(await f.request(), false);
  assert.deepEqual(f.callbacks, [false, false]);
  assert.equal(f.notices.length, 2);
  assert.ok(f.notices.every(value => !value.includes('private fixture')));
});
