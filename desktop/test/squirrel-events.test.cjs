'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { isSquirrelEvent, handleSquirrelEvent, shortcutSpecs } = require('../squirrel-events.cjs');
const { desktopMode } = require('../desktop-mode.cjs');

test('backend mode selection is explicit and rejects ambiguous or unknown modes', () => {
  assert.equal(desktopMode(['app']), 'windows');
  assert.equal(desktopMode(['app', '--backend=wsl']), 'wsl');
  assert.throws(() => desktopMode(['--backend=windows', '--backend=wsl']));
  assert.throws(() => desktopMode(['--backend=anything']));
  assert.equal(isSquirrelEvent(['app', '--squirrel-install']), true);
  assert.equal(isSquirrelEvent(['app', '--squirrel-firstrun']), false);
});

test('install, update and uninstall await native shortcut work and preserve unrelated links', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-shortcut-test-'));
  const executable = path.join(root, 'app-0.2.0', 'StandTermDesktopEvaluation.exe');
  let ready = false;
  const app = { whenReady: async () => { ready = true; }, getPath: kind => path.join(root, kind) };
  const shell = {
    writeShortcutLink(file, operation, options) {
      assert.equal(ready, true);
      fs.writeFileSync(file, JSON.stringify(options));
      return true;
    },
    readShortcutLink: file => JSON.parse(fs.readFileSync(file, 'utf8')),
    trashItem: async file => fs.renameSync(file, `${file}.trashed`),
  };
  const argv = ['app', '--squirrel-install'];
  await handleSquirrelEvent(app, shell, argv, executable);
  const specs = shortcutSpecs(executable, app.getPath('desktop'), path.join(app.getPath('appData'),
    'Microsoft', 'Windows', 'Start Menu', 'Programs'));
  assert.equal(specs.length, 4);
  assert.deepEqual(specs.slice(0, 2).map(spec => path.basename(spec.path)), ['StandTerm Desktop.lnk', 'StandTerm Desktop.lnk']);
  assert.match(shell.readShortcutLink(specs[2].path).args, /--backend=wsl/);
  assert.match(shell.readShortcutLink(specs[0].path).args, /--backend=windows/);
  await handleSquirrelEvent(app, shell, ['app', '--squirrel-updated'], executable);
  fs.writeFileSync(specs[0].path, JSON.stringify({ target: 'unrelated.exe', args: '' }));
  await assert.rejects(handleSquirrelEvent(app, shell, argv, executable), /Another shortcut/);
  await handleSquirrelEvent(app, shell, ['app', '--squirrel-uninstall'], executable);
  assert.equal(fs.existsSync(specs[0].path), true);
  for (const spec of specs.slice(1)) assert.equal(fs.existsSync(spec.path), false);
});
