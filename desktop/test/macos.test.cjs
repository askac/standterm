'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { desktopMode } = require('../desktop-mode.cjs');
const { macPythonCandidates, validMacPython } = require('../macos-python.cjs');
const { backendCommand } = require('../policy.cjs');
const { browserSessionOptions } = require('../browser-session.cjs');
const { agentConnectionInfo } = require('../diagnostics.cjs');

test('macOS defaults to native mode and rejects incompatible or duplicate backend flags', () => {
  assert.equal(desktopMode([], 'darwin'), 'macos');
  assert.equal(desktopMode([], 'win32'), 'windows');
  assert.equal(desktopMode(['--backend=wsl'], 'win32'), 'wsl');
  assert.equal(desktopMode([], 'linux'), 'windows'); // Existing source/WSLg diagnostic mode.
  for (const argv of [['--backend=windows'], ['--backend=wsl'], ['--backend=unknown'],
    ['--backend=macos', '--backend=macos']]) assert.throws(() => desktopMode(argv, 'darwin'));
  assert.throws(() => desktopMode(['--backend=macos'], 'win32'));
  const command = backendCommand('/Projects/Stand Term', 'darwin', {});
  assert.equal(command.executable, path.join('/Projects/Stand Term', 'tools', '.venv_macos', 'bin', 'python'));
  assert.deepEqual(command.args, ['-u', path.join('/Projects/Stand Term', 'desktop', 'backend.py')]);
  assert.equal(browserSessionOptions('macos').partition, 'persist:standterm-ui-macos-v1');
  assert.equal(agentConnectionInfo({ origin: 'http://127.0.0.1:12345', mode: 'macos',
    instanceId: 'test' }).backend_mode, 'macos');
});

test('Finder Python discovery includes MacPorts, preserves saved paths, and skips Apple stubs', () => {
  const candidates = macPythonCandidates('/Custom Python/bin/python3', { PATH: '/usr/bin:relative:/opt/local/bin' });
  assert.equal(candidates[0], '/Custom Python/bin/python3');
  assert.ok(candidates.includes('/opt/local/bin/python3'));
  assert.ok(candidates.includes('/opt/homebrew/bin/python3'));
  assert.ok(!candidates.includes('/usr/bin/python3'));
  assert.equal(new Set(candidates).size, candidates.length);
  assert.ok(!macPythonCandidates('relative').includes('relative'));
});

test('Python acceptance requires native architecture and a usable macOS venv interpreter', () => {
  const info = { type: 'python_info', platform: 'darwin', bits: 64, venv: true,
    version: [3, 13], machine: 'arm64', executable: '/opt/local/bin/python3' };
  assert.equal(validMacPython(info, 'arm64'), true);
  assert.equal(validMacPython({ ...info, machine: 'x86_64' }, 'x64'), true);
  for (const change of [{ machine: 'x86_64' }, { platform: 'linux' }, { version: [3, 9] },
    { bits: 32 }, { venv: false }, { executable: '/usr/bin/python3' }, { executable: 'python3' }]) {
    assert.equal(validMacPython({ ...info, ...change }, 'arm64'), false);
  }
});
