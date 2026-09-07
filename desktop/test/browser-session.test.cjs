'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { browserSessionOptions, resetBrowserAuthentication } = require('../browser-session.cjs');

test('browser profiles persist across restarts and isolate backend modes and test runs', () => {
  const a = browserSessionOptions('wsl');
  assert.deepEqual(a, browserSessionOptions('wsl'));
  assert.ok(a.partition.startsWith('persist:'));
  assert.notEqual(a.partition, browserSessionOptions('windows').partition);
  assert.equal(a.options.cache, false);
  const temporary = browserSessionOptions('wsl', true);
  assert.ok(!temporary.partition.startsWith('persist:'));
  assert.notEqual(temporary.partition, browserSessionOptions('wsl', true).partition);
  assert.throws(() => browserSessionOptions('../other'));
});

test('fresh login clears cookies and workers without clearing Core preferences or CryptoKeys', async () => {
  const calls = [];
  await resetBrowserAuthentication({
    clearStorageData: async options => calls.push(options),
    clearAuthCache: async () => calls.push('http-auth'),
  });
  assert.deepEqual(calls, [{ storages: ['cookies', 'serviceworkers', 'cachestorage'] }, 'http-auth']);
  let continued = false;
  await assert.rejects(resetBrowserAuthentication({
    clearStorageData: async () => { throw new Error('Storage reset failed'); },
    clearAuthCache: async () => { continued = true; },
  }));
  assert.equal(continued, false);
});
