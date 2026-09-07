'use strict';

// Compile and run only a read-only NSIS predicate harness, never the installer.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync, spawnSync } = require('node:child_process');

assert.equal(process.platform, 'win32');
const compiler = process.argv[2];
assert.ok(compiler, 'Pass the cached makensis.exe path.');
const root = fs.mkdtempSync(path.join(__dirname, '..', 'dist', 'legacy-scan-test-'));
const harness = path.join(root, 'legacy-scan.exe');
execFileSync(compiler, ['/V2', `/DTEST_OUTPUT=${harness}`, path.join(__dirname, 'legacy-install-smoke.nsi')],
  { windowsHide: true, stdio: 'pipe' });

function check(name, files, expected) {
  const fixture = path.join(root, name);
  fs.mkdirSync(fixture);
  for (const file of files) {
    const target = path.join(fixture, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, 'fixture');
  }
  const before = fs.readdirSync(fixture, { recursive: true });
  const result = spawnSync(harness, [], { windowsHide: true, timeout: 15000,
    env: { ...process.env, STANDTERM_LEGACY_TEST_ROOT: fixture } });
  assert.ifError(result.error);
  assert.equal(result.status, expected, name);
  assert.deepEqual(fs.readdirSync(fixture, { recursive: true }), before, 'Detection must not remove residual files');
}

check('empty', [], 0);
check('uninstalled with spaces', ['Update.exe', '.dead', 'app-0.2.1/remaining.log'], 0);
check('updater only', ['Update.exe'], 0);
check('registered app files', ['Update.exe', 'app-0.2.1/StandTermDesktopEvaluation.exe'], 1);
check('dead marker with app', ['.dead', 'app-0.2.1/StandTermDesktopEvaluation.exe'], 1);
check('later version app', ['app-0.1.0/remaining.log', 'app-0.2.1/StandTermDesktopEvaluation.exe'], 1);
console.log('NSIS legacy file detection: 6 read-only fixture cases passed.');
