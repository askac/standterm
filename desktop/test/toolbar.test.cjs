'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

function fixture(platform = 'win32') {
  let handler, requestFilter, permission, device, popup = 0, calls = 0, focused;
  const edits = [];
  const contents = new EventEmitter();
  Object.assign(contents, { mainFrame: {}, isDestroyed: () => false, send: () => {}, setWindowOpenHandler: () => {},
    session: { setPermissionRequestHandler: fn => { permission = fn; }, setPermissionCheckHandler: () => {},
      setDevicePermissionHandler: fn => { device = fn; }, webRequest: { onBeforeRequest: fn => { requestFilter = fn; } } } });
  const win = new EventEmitter();
  Object.assign(win, { webContents: contents, getContentSize: () => [640, 480], contentView: { addChildView: () => {}, on: () => {} },
    isDestroyed: () => false, isVisible: () => true, isMinimized: () => false });
  focused = win;
  const core = { setBounds: bounds => { core.bounds = bounds; }, webContents: { focus: () => {}, isDestroyed: () => true } };
  const api = { exports: {} };
  const electron = { BrowserWindow: { getFocusedWindow: () => focused },
    ipcMain: { handle: (_name, fn) => { handler = fn; }, removeHandler: () => { handler = null; } },
    Menu: { getApplicationMenu: () => ({ getMenuItemById: () => ({ submenu: { popup: () => { popup++; } } }) }) } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'toolbar.cjs'), 'utf8'), {
    require: name => name === 'electron' ? electron : require(name), module: api,
    __dirname: path.join(__dirname, '..'), process: { platform },
  });
  const capture = { screenshot: async () => { calls++; }, start: async () => { calls++; },
    togglePause: async () => { calls++; }, stop: async () => { calls++; } };
  const toolbar = api.exports.installToolbar(win, core, capture, {
    refresh: async () => {}, edit: action => { edits.push(action); return true; },
  });
  contents.mainFrame.url = toolbar.url;
  const event = { sender: contents, senderFrame: contents.mainFrame };
  return { invoke: (action, source = event) => handler(source, action), event, win, core, contents, toolbar,
    unfocus: () => { focused = null; }, calls: () => calls, popup: () => popup, edits,
    filter: url => new Promise(resolve => requestFilter({ url }, resolve)),
    permission: () => new Promise(resolve => permission(contents, 'media', resolve)), device: () => device() };
}

test('only the bundled toolbar main frame can invoke fixed native capture/menu actions', async () => {
  const f = fixture();
  assert.equal(await f.invoke('record'), true);
  assert.equal(f.calls(), 1);
  assert.equal(await f.invoke('record', { sender: f.core.webContents, senderFrame: f.event.senderFrame }), false);
  assert.equal(await f.invoke('record', { sender: f.contents, senderFrame: { url: f.toolbar.url } }), false);
  assert.equal(await f.invoke('execute-code'), false);
  assert.equal(await f.invoke({ action: 'record' }), false);
  assert.equal(await f.invoke('menu:standterm'), true);
  assert.equal(await f.invoke('menu:unknown'), false);
  assert.equal(f.popup(), 1);
  f.unfocus();
  assert.equal(await f.invoke('record'), false);
  assert.equal(f.calls(), 1);
});

test('clipboard buttons use only fixed native edit commands from the trusted focused toolbar', async () => {
  const f = fixture();
  assert.equal(await f.invoke('copy-text'), true);
  assert.equal(await f.invoke('paste-text'), true);
  assert.deepEqual(f.edits, ['copy', 'paste']);
  assert.equal(await f.invoke('paste-text', { sender: f.core.webContents, senderFrame: f.event.senderFrame }), false);
  f.unfocus();
  assert.equal(await f.invoke('paste-text'), false);
  assert.deepEqual(f.edits, ['copy', 'paste']);
});

test('toolbar resource and permission boundaries are identical on Windows and macOS', async () => {
  for (const platform of ['win32', 'darwin']) {
    const f = fixture(platform);
    assert.equal((await f.filter(f.toolbar.url)).cancel, false);
    for (const url of ['https://example.com/', 'http://127.0.0.1:5000/', 'file:///private.txt']) {
      assert.equal((await f.filter(url)).cancel, true);
    }
    assert.equal(await f.permission(), false);
    assert.equal(f.device(), false);
    assert.equal(f.core.bounds.y, 36);
    assert.equal(f.core.bounds.height, 444);
    f.contents.mainFrame.url = 'https://example.com/';
    assert.equal(await f.invoke('record'), false);
  }
});
