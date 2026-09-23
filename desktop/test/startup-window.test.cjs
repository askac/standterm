'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { startupHtml } = require('../startup-window.cjs');
const { create } = require('../i18n.js');

test('startup copy follows the cached language and escapes scriptless display data', () => {
  for (const locale of ['en', 'zh-TW']) {
    const language = create(locale), html = startupHtml('StandTerm Desktop (WSL)', language);
    assert.ok(html.includes(`<html lang="${locale}">`));
    assert.ok(html.includes(language.t('desktop.startup.starting')));
    assert.ok(html.includes("default-src 'none'"));
    assert.ok(!html.includes('<script'));
  }
  const html = startupHtml('<img src=x>', { locale: 'invalid', t: () => '<script>bad()</script>' });
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<script>'));
});

test('startup is visible before load completes, isolated, refocusable and safe to dispose twice', async () => {
  let win, finish, request, permissions, device;
  const isolated = {
    setPermissionRequestHandler: cb => { permissions = cb; },
    setPermissionCheckHandler: cb => { isolated.check = cb; },
    setDevicePermissionHandler: cb => { device = cb; },
    webRequest: { onBeforeRequest: cb => { request = cb; } },
  };
  class Window extends EventEmitter {
    constructor(options) {
      super(); win = this; this.options = options; this.destroyed = false; this.minimized = true;
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = cb => { this.open = cb; };
    }
    setMenu() {}
    loadURL(url) { this.url = url; return new Promise(resolve => { finish = resolve; }); }
    isDestroyed() { return this.destroyed; }
    isMinimized() { return this.minimized; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    destroy() { this.destroyed = true; }
  }
  const api = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../startup-window.cjs'), 'utf8'), {
    module: api,
    require: name => name === 'electron' ? { BrowserWindow: Window, session: { fromPartition: () => isolated } }
      : require(path.join(__dirname, '..', name)),
  });
  const startup = api.exports.createStartupWindow('StandTerm Desktop (WSL)', create('zh-TW'));
  assert.equal(win.options.show, true);
  assert.equal(win.options.webPreferences.sandbox, true);
  assert.equal(win.options.webPreferences.nodeIntegration, false);
  assert.equal(win.options.webPreferences.contextIsolation, true);
  assert.equal(win.open().action, 'deny');
  permissions(null, 'clipboard-read', allowed => assert.equal(allowed, false));
  assert.equal(isolated.check(), false);
  assert.equal(device(), false);
  for (const url of ['https://example.com', 'file:///tmp/secret', 'data:text/html,<script>bad()</script>']) {
    request({ url }, result => assert.equal(result.cancel, true));
  }
  request({ url: win.url }, result => assert.equal(result.cancel, false));
  startup.focus();
  assert.ok(win.shown && win.focused && !win.minimized);
  finish(); await startup.ready;
  startup.close(); startup.close(); startup.focus();
  assert.equal(win.destroyed, true);
});

test('recovery restores the main window after the startup window closes', () => {
  const source = fs.readFileSync(path.join(__dirname, '../main.cjs'), 'utf8');
  const functions = ['closeStartupWindow', 'showWindow'].map(name => {
    const match = source.match(new RegExp(`function ${name}\\(\\) \\{[\\s\\S]*?\\n\\}`));
    assert.ok(match, `${name} must be available`);
    return match[0];
  }).join('\n');
  const calls = [];
  let destroyed = false, minimized = true;
  const context = vm.createContext({
    booting: false,
    startupWindow: {
      close: () => { destroyed = true; calls.push('close-startup'); },
      focus: () => { if (!destroyed) calls.push('focus-startup'); },
    },
    focusSetup: () => calls.push('focus-setup'),
    win: {
      isDestroyed: () => false, isMinimized: () => minimized,
      restore: () => { minimized = false; calls.push('restore-main'); },
      show: () => calls.push('show-main'), focus: () => calls.push('focus-main'),
    },
  });
  vm.runInContext(functions + '\ncloseStartupWindow(); booting = true; showWindow();', context);
  assert.deepEqual(calls, ['close-startup', 'restore-main', 'show-main', 'focus-main']);
  assert.equal(minimized, false);
});
