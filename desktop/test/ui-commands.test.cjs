'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { allowedNavigation } = require('../policy.cjs');

test('native clipboard commands preserve the Core editing target and reject stale or hidden windows', () => {
  const calls = [], state = { focused: true, hidden: false, minimized: false, destroyed: false, url: 'http://127.0.0.1:64487/' };
  const win = new EventEmitter();
  Object.assign(win, { isDestroyed: () => state.destroyed, isVisible: () => !state.hidden, isMinimized: () => state.minimized });
  const contents = { on: () => {}, isLoadingMainFrame: () => false, mainFrame: {}, isDestroyed: () => state.destroyed, getURL: () => state.url,
    focus: () => calls.push('focus'), copy: () => calls.push('copy'), paste: () => calls.push('paste') };
  const api = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'ui-commands.cjs'), 'utf8'), {
    require: name => name === 'electron' ? { BrowserWindow: { getFocusedWindow: () => state.focused ? win : null } }
      : name === './policy.cjs' ? { allowedNavigation } : require(path.join(__dirname, '..', name)),
    module: api, setInterval: () => 0, clearInterval: () => {},
  });
  const commands = api.exports.createUiCommands(win, contents, 'http://127.0.0.1:64487');
  assert.equal(commands.edit('copy'), true);
  assert.equal(commands.edit('paste'), true);
  assert.deepEqual(calls, ['focus', 'copy', 'focus', 'paste']);
  assert.equal(commands.edit('cut'), false);
  for (const key of ['hidden', 'minimized', 'destroyed']) {
    state[key] = true; assert.equal(commands.edit('paste'), false); state[key] = false;
  }
  state.focused = false;
  assert.equal(commands.edit('paste'), false);
  state.focused = true; state.url = 'https://example.com/';
  assert.equal(commands.edit('paste'), false);
  assert.equal(calls.length, 4);
});

test('localized menu labels preserve typed commands, target IDs and focus checks', async () => {
  for (const locale of ['en', 'zh-TW']) {
    const calls = [];
    let focused = true;
    const win = new EventEmitter();
    Object.assign(win, { isDestroyed: () => false });
    const contents = { on: () => {}, isLoadingMainFrame: () => false, mainFrame: {}, isDestroyed: () => false, getURL: () => 'http://127.0.0.1:64487/',
      executeJavaScript: async script => {
        if (script.includes('.snapshot()')) return { version: 1, ready: true, actions: { newTab: true }, terminalId: 'terminal-1' };
        calls.push(script); return true;
      } };
    const api = { exports: {} };
    const i18n = require('../i18n.js');
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'ui-commands.cjs'), 'utf8'), {
      require: name => name === 'electron' ? { BrowserWindow: { getFocusedWindow: () => focused ? win : null } }
        : name === './policy.cjs' ? { allowedNavigation } : i18n,
      module: api, setInterval: () => 0, clearInterval: () => {},
    });
    const { t } = i18n.create(locale);
    const commands = api.exports.createUiCommands(win, contents, 'http://127.0.0.1:64487', t);
    const item = commands.item('newTab');
    assert.equal(item.id, 'ui-newTab');
    assert.equal(item.label, t('desktop.menu.new_tab'));
    assert.equal(await item.click(), true);
    assert.deepEqual(calls, ['window.standtermUi?.run("newTab", "terminal-1")']);
    assert.equal(await commands.run(item.label), false);
    focused = false;
    assert.equal(await item.click(), false);
    assert.equal(calls.length, 1);
  }
});

function localeFixture() {
  const win = new EventEmitter(), contents = new EventEmitter(), locales = [];
  const state = { destroyed: false, loading: false, url: 'http://127.0.0.1:64487/',
    snapshot: { version: 1, ready: true, actions: {}, uiLanguage: 'zh-TW' } };
  Object.assign(win, { isDestroyed: () => state.destroyed });
  Object.assign(contents, { isDestroyed: () => state.destroyed, isLoadingMainFrame: () => state.loading,
    getURL: () => state.url, mainFrame: {}, executeJavaScript: async () => state.snapshot });
  const api = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'ui-commands.cjs'), 'utf8'), {
    require: name => name === 'electron' ? { BrowserWindow: { getFocusedWindow: () => win },
      Menu: { getApplicationMenu: () => null } } : name === './policy.cjs' ? { allowedNavigation } : require('../i18n.js'),
    module: api, setInterval: () => 0, clearInterval: () => {},
  });
  return { state, contents, locales,
    commands: api.exports.createUiCommands(win, contents, 'http://127.0.0.1:64487', undefined, value => locales.push(value)) };
}

test('only ready Core snapshots with supported locale values update Desktop', async () => {
  const f = localeFixture();
  await f.commands.refresh();
  for (const value of [undefined, null, 'fr', {}, 1]) {
    f.state.snapshot.uiLanguage = value;
    await f.commands.refresh();
  }
  f.state.snapshot.uiLanguage = 'en'; f.state.snapshot.ready = false;
  await f.commands.refresh();
  f.state.snapshot.ready = true; f.state.url = 'https://example.com/';
  await f.commands.refresh();
  f.state.url = 'http://127.0.0.1:64487/'; f.state.loading = true;
  await f.commands.refresh();
  f.state.loading = false;
  await f.commands.refresh();
  assert.deepEqual(f.locales, ['zh-TW', 'en']);
});

test('navigation, frame replacement and closed windows discard late locale snapshots', async () => {
  for (const phase of ['navigation', 'frame', 'destroyed', 'loading', 'foreign']) {
    const f = localeFixture();
    let resolve;
    f.contents.executeJavaScript = () => new Promise(done => { resolve = done; });
    const pending = f.commands.refresh();
    if (phase === 'navigation') f.contents.emit('did-start-navigation', {}, f.state.url, false, true);
    if (phase === 'frame') f.contents.mainFrame = {};
    if (phase === 'destroyed') f.state.destroyed = true;
    if (phase === 'loading') f.state.loading = true;
    if (phase === 'foreign') f.state.url = 'https://example.com/';
    resolve(f.state.snapshot);
    await pending;
    assert.deepEqual(f.locales, [], phase);
  }
});
