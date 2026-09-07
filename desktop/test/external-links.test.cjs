'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { externalUrl, createExternalOpener } = require('../external-links.cjs');
const origin = 'http://127.0.0.1:54321';

test('browser handoff admits only canonical HTTP(S), without credentials or loopback', () => {
  assert.equal(externalUrl('https://example.com/path?q=hello#x', origin), 'https://example.com/path?q=hello#x');
  assert.equal(externalUrl('http://192.168.1.1/', origin), 'http://192.168.1.1/');
  for (const url of ['file:///tmp/test', 'javascript:alert(1)', 'ms-settings:x', '//example.com',
    'https://user:pass@example.com', 'https://example.com/\n', 'https://example.com\\x',
    origin + '/?token=secret', 'http://localhost:5000', 'http://sub.localhost.',
    'http://127.1', 'http://2130706433', 'http://0x7f000001', 'http://[::1]',
    'http://[::ffff:127.0.0.1]', 'http://0.0.0.0', 'http://[::]', 'x'.repeat(4097)]) {
    assert.equal(externalUrl(url, origin), null, url);
  }
});

test('browser handoff requires approval, coalesces requests and rechecks lifetime', async () => {
  let approve;
  let calls = 0;
  const opened = [];
  let current = origin;
  const owner = { isDestroyed: () => false, getURL: () => current };
  const source = { isDestroyed: () => false };
  const request = createExternalOpener({ origin, owner, confirm: () => {
    calls++; return new Promise(resolve => { approve = resolve; });
  }, open: async url => opened.push(url), notify: async () => {} });
  const first = request('https://example.com', source);
  assert.equal(await request('https://example.org', source), false);
  approve(false);
  assert.equal(await first, false);
  assert.equal(calls, 1);
  const second = request('https://example.com', source);
  current = 'https://foreign.invalid';
  approve(true);
  assert.equal(await second, false);
  assert.deepEqual(opened, []);
  current = origin;
  const third = request('https://example.com', source);
  approve(true);
  assert.equal(await third, true);
  assert.deepEqual(opened, ['https://example.com/']);
});

test('failed OS browser launch reports a fixed error and releases the pending prompt', async () => {
  let notices = 0;
  const source = { isDestroyed: () => false, getURL: () => origin };
  const request = createExternalOpener({ origin, owner: source, confirm: async () => true,
    open: async () => { throw new Error('private OS detail'); }, notify: async () => { notices++; } });
  assert.equal(await request('https://example.com', source), false);
  assert.equal(await request('https://example.com', source), false);
  assert.equal(notices, 2);
});

test('browser handoff rejects destroyed requesters and tolerates a closed error dialog', async () => {
  for (const target of ['owner', 'source']) {
    let approve;
    let destroyed = false;
    let opened = false;
    const owner = { isDestroyed: () => target === 'owner' && destroyed, getURL: () => origin };
    const source = { isDestroyed: () => target === 'source' && destroyed };
    const request = createExternalOpener({ origin, owner,
      confirm: () => new Promise(resolve => { approve = resolve; }),
      open: async () => { opened = true; }, notify: async () => {} });
    const pending = request('https://example.com', source);
    destroyed = true;
    approve(true);
    assert.equal(await pending, false);
    assert.equal(opened, false);
  }
  const source = { isDestroyed: () => false, getURL: () => origin };
  const request = createExternalOpener({ origin, owner: source, confirm: async () => true,
    open: async () => { throw new Error('OS error'); }, notify: async () => { throw new Error('Closed'); } });
  assert.equal(await request('https://example.com', source), false);
});
