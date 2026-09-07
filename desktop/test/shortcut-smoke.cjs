'use strict';

// Native .lnk operations are redirected into a test directory, not the user's
// Desktop/Start menu. No installed app, backend or existing shortcut is touched.
const { app, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const { handleSquirrelEvent, shortcutSpecs } = require('../squirrel-events.cjs');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-native-shortcut-'));
app.setPath('userData', path.join(root, 'profile'));
app.whenReady().then(async () => {
  const testApp = { whenReady: () => app.whenReady(), getPath: kind => path.join(root, kind) };
  const executable = path.join(root, 'app-0.2.0', 'StandTermDesktopEvaluation.exe');
  const specs = shortcutSpecs(executable, testApp.getPath('desktop'), path.join(testApp.getPath('appData'),
    'Microsoft', 'Windows', 'Start Menu', 'Programs'));
  await handleSquirrelEvent(testApp, shell, ['app', '--squirrel-install'], executable);
  for (const spec of specs) {
    const actual = shell.readShortcutLink(spec.path);
    assert.equal(actual.target, spec.options.target);
    assert.equal(actual.args, spec.options.args);
    assert.equal(actual.appUserModelId, spec.options.appUserModelId);
  }
  await handleSquirrelEvent(testApp, shell, ['app', '--squirrel-updated'], executable);
  await handleSquirrelEvent(testApp, shell, ['app', '--squirrel-uninstall'], executable);
  for (const spec of specs) assert.equal(fs.existsSync(spec.path), false);
  console.log('Native shortcut smoke passed: both modes create/update/remove Desktop and Start menu links in the isolated test directory.');
  app.quit();
}).catch(error => { console.error(error); app.exit(1); });
