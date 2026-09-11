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
  const contents = { isDestroyed: () => state.destroyed, getURL: () => state.url,
    focus: () => calls.push('focus'), copy: () => calls.push('copy'), paste: () => calls.push('paste') };
  const api = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'ui-commands.cjs'), 'utf8'), {
    require: name => name === 'electron' ? { BrowserWindow: { getFocusedWindow: () => state.focused ? win : null } } : { allowedNavigation },
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
