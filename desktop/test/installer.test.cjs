'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { installerRequest, performMaintenance, watchInstaller } = require('../installer.cjs');
const { shortcutPlan, applyShortcuts } = require('../installer-shortcuts.cjs');

test('installer requests are typed, bounded and cannot choose arbitrary profile paths', () => {
  assert.equal(installerRequest(['app', '--backend=wsl']), null);
  for (const mode of ['windows', 'wsl', 'both']) {
    assert.deepEqual(installerRequest(['app', `--installer-prepare=${mode}`, '--installer-parent=123']).modes,
      mode === 'both' ? ['windows', 'wsl'] : [mode]);
  }
  for (const flags of [[], ['--installer-prepare=other'], ['--installer-prepare=windows', '--installer-cleanup-venvs'],
    ['--installer-uninstall', '--installer-uninstall'], ['--installer-profile=C:\\Other']]) {
    assert.throws(() => installerRequest(['--installer-parent=123', ...flags]));
  }
  for (const pid of ['0', '-1', '4294967296', '123;exit', '1\n']) {
    assert.throws(() => installerRequest(['--installer-uninstall', `--installer-parent=${pid}`]));
  }
});

test('both modes must finish before publishing shortcuts; a failed second mode is retryable', async () => {
  const events = [];
  const request = { action: 'prepare', modes: ['windows', 'wsl'] };
  const prepare = async (mode, options) => {
    assert.deepEqual(options, { installer: true });
    events.push(mode);
    if (mode === 'wsl') throw new Error('Missing WSL Python');
  };
  const deps = { prepare, ensureAlive() {}, shortcuts: async modes => events.push(modes) };
  await assert.rejects(performMaintenance(request, deps), /Missing WSL/);
  assert.deepEqual(events, ['windows', 'wsl']);
  events.length = 0;
  await performMaintenance(request, { ...deps, prepare: async mode => events.push(mode) });
  assert.deepEqual(events, ['windows', 'wsl', ['windows', 'wsl']]);
});

test('uninstall retains environments by default and reports unavailable cleanup without broadening scope', async () => {
  const events = [];
  const deps = { ensureAlive() {}, shortcuts: async modes => events.push(modes),
    cleanup: async mode => { events.push(mode); throw new Error('Unavailable'); },
    report: async result => { assert.equal(result.length, 2); assert.ok(result.every(item => item.status === 'unknown')); } };
  await performMaintenance({ action: 'uninstall', cleanup: false }, deps);
  assert.deepEqual(events, [[]]);
  events.length = 0;
  await performMaintenance({ action: 'uninstall', cleanup: true }, deps);
  assert.deepEqual(events, ['windows', 'wsl', []]);
});

test('installer loss prevents subsequent mode setup and success publication', async () => {
  let alive = true;
  const events = [];
  await assert.rejects(performMaintenance({ action: 'prepare', modes: ['windows', 'wsl'] }, {
    ensureAlive: () => { if (!alive) throw new Error('Owner lost'); },
    prepare: async mode => { events.push(mode); alive = false; },
    shortcuts: async modes => events.push(modes),
  }), /Owner lost/);
  assert.deepEqual(events, ['windows']);
});

test('installer watcher requires a typed ready frame and observes parent handle exit', async () => {
  let child;
  let lost = 0;
  const launch = (executable, args, options) => {
    assert.match(executable, /powershell\.exe$/);
    assert.ok(args.at(-1).includes('$p.Handle'));
    assert.equal(options.shell, false);
    child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.kill = () => { child.emit('exit', 1); };
    return child;
  };
  const watcher = watchInstaller(123, () => { lost++; }, launch);
  assert.equal(watcher.alive(), false);
  child.stdout.emit('data', Buffer.from('{"type":"parent_ready"}\r\n'));
  await watcher.started;
  assert.equal(watcher.alive(), true);
  child.emit('exit', 0);
  assert.equal(watcher.alive(), false);
  assert.equal(lost, 1);
  watcher.stop();
  assert.equal(lost, 1);
  const broken = watchInstaller(123, () => { lost++; }, launch);
  child.emit('error', new Error('Blocked'));
  await assert.rejects(broken.started);
  broken.stop();
});

test('mode shortcuts never overwrite unrelated links and remove only exact owned targets', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-nsis-links-'));
  const executable = path.join(root, 'StandTermDesktop.exe');
  const plan = shortcutPlan(executable, path.join(root, 'Desktop'), path.join(root, 'Programs'), ['wsl']);
  const links = new Map();
  const changed = [];
  const shell = {
    readShortcutLink: file => links.get(file),
    writeShortcutLink(file, operation, options) {
      fs.writeFileSync(file, 'test link'); links.set(file, options); changed.push(operation); return true;
    },
    async trashItem(file) { fs.renameSync(file, `${file}.recoverable`); changed.push('trash'); },
  };
  await applyShortcuts(shell, plan);
  assert.deepEqual(changed, ['create', 'create']);
  const unrelated = plan.find(item => item.selected);
  links.set(unrelated.path, { target: 'C:\\Other.exe', args: '' });
  changed.length = 0;
  await assert.rejects(applyShortcuts(shell, plan), /Another shortcut/);
  assert.deepEqual(changed, []);
  await applyShortcuts(shell, shortcutPlan(executable, path.join(root, 'Desktop'), path.join(root, 'Programs'), []));
  assert.deepEqual(changed, ['trash']);
  assert.ok(fs.existsSync(unrelated.path));
});
