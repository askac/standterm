'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { backendCommand, parseHandoff, allowedRequest, allowedNavigation } = require('../policy.cjs');

test('handoff accepts typed loopback metadata and rejects attacker origins', () => {
  const frame = {
    type: 'standterm_desktop_ready', version: 1, origin: 'http://127.0.0.1:45678',
    instance_id: 'test-instance', launcher_token: 'test-launcher',
    session_token: 'test-session', cookie_name: 'session_token',
  };
  assert.equal(parseHandoff(JSON.stringify(frame)).origin, frame.origin);
  for (const origin of ['https://example.com', 'http://127.0.0.1:45678/path',
    'http://127.0.0.1.example.com:45678', 'http://user@127.0.0.1:45678']) {
    assert.throws(() => parseHandoff(JSON.stringify({ ...frame, origin })));
  }
  assert.throws(() => parseHandoff('Access URL: http://localhost:5000'));
  assert.throws(() => parseHandoff(JSON.stringify({ ...frame, version: 2 })));
  assert.throws(() => parseHandoff(' '.repeat(4097)));
  const versioned = { ...frame, core_version: '2.11.0-dev', python_version: '3.12.7', core_bundle_id: 'a'.repeat(64) };
  assert.equal(parseHandoff(JSON.stringify(versioned)).core_version, '2.11.0-dev');
  assert.equal(parseHandoff(JSON.stringify(versioned)).core_bundle_id, 'a'.repeat(64));
  assert.equal(parseHandoff(JSON.stringify(frame)).core_version, undefined, 'older backends retain unknown-version fallback');
  for (const change of [{ core_version: '2.11' }, { core_version: '2.11.0\nsecret' },
    { python_version: {} }, { core_bundle_id: '../other' }]) {
    assert.throws(() => parseHandoff(JSON.stringify({ ...versioned, ...change })));
  }
});

test('page cannot navigate to other origins, files or executable URLs', () => {
  const origin = 'http://127.0.0.1:45678';
  assert.equal(allowedNavigation(`${origin}/?debug=1`, origin), true);
  assert.equal(allowedRequest('ws://127.0.0.1:45678/socket.io/', origin), true);
  assert.equal(allowedRequest(`blob:${origin}/image`, origin), true);
  for (const url of ['https://example.com/', 'file:///etc/passwd', 'javascript:alert(1)',
    'http://127.0.0.1:45679/', 'http://127.0.0.1:45678@evil.example/']) {
    assert.equal(allowedNavigation(url, origin), false);
    assert.equal(allowedRequest(url, origin), false);
  }
  assert.equal(allowedNavigation('data:text/html,hello', origin), false);
});

test('WSL arguments preserve paths without shell interpolation', () => {
  const config = backendCommand('/repo', 'win32', {
    STANDTERM_DESKTOP_WSL_DISTRO: 'Ubuntu',
    STANDTERM_DESKTOP_WSL_REPO: '/mnt/d/My Project/standterm',
  });
  assert.equal(config.executable, 'wsl.exe');
  assert.ok(config.args.includes('/mnt/d/My Project/standterm/desktop/backend.py'));
  assert.ok(config.args.includes('--exec'));
  assert.throws(() => backendCommand('/repo', 'win32', { STANDTERM_DESKTOP_WSL_DISTRO: 'Ubuntu' }));
});
