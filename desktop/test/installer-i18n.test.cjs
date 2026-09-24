'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { create } = require('../i18n.js');
const { createLanguage } = require('../language.cjs');

function fixture(testContext, preferences = {}, options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-installer-i18n-'));
  testContext.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const profiles = Object.fromEntries(['windows', 'wsl'].map(mode => [mode, path.join(root, mode)]));
  for (const [mode, value] of Object.entries(preferences)) {
    fs.mkdirSync(profiles[mode], { recursive: true });
    fs.writeFileSync(path.join(profiles[mode], 'language.json'),
      typeof value === 'string' ? value : JSON.stringify(value));
  }
  const snapshot = () => Object.fromEntries(Object.entries(profiles).map(([mode, directory]) => [mode,
    fs.existsSync(directory) ? fs.readdirSync(directory).map(name => [name, fs.readFileSync(path.join(directory, name), 'utf8')]) : null]));
  const initialPreferences = snapshot();
  const calls = { dialogs: [], events: [], exit: [], stopSetup: 0, watcherKills: 0, launches: [] };
  const app = new EventEmitter();
  Object.assign(app, { getVersion: () => 'test-version', getPath: name => path.join(root, name),
    exit: code => calls.exit.push(code) });
  let watcher;
  const modules = {
    electron: { app, shell: {}, dialog: { showMessageBox: async settings => {
      calls.dialogs.push(settings); return { response: 0 };
    } } },
    'node:child_process': { spawn: (executable, args, launchOptions) => {
      calls.launches.push({ executable, args, options: launchOptions });
      watcher = new EventEmitter();
      watcher.stdout = new EventEmitter();
      watcher.kill = () => { calls.watcherKills++; watcher.emit('exit', 0); };
      queueMicrotask(() => watcher.stdout.emit('data', Buffer.from('{"type":"parent_ready"}\n')));
      return watcher;
    } },
    './language.cjs': { createLanguage },
    './i18n.js': require('../i18n.js'),
    './setup.cjs': {
      modeProfile: mode => profiles[mode],
      preparePackagedBackend: async (mode, settings) => {
        assert.equal(settings.installer, true);
        calls.events.push(`prepare:${mode}`);
        await options.prepare?.(mode);
      },
      cleanupManagedVenvs: async mode => {
        calls.events.push(`cleanup:${mode}`);
        return options.cleanup ? options.cleanup(mode) : { mode, status: 'retained' };
      },
      stopSetup: async () => { calls.stopSetup++; },
      confirmSetupQuit: async () => false,
    },
    './core-source.cjs': { installedStore: async (profile, resources, version) => {
      assert.equal(resources, path.join(root, 'resources'));
      assert.equal(version, 'test-version');
      const mode = Object.keys(profiles).find(key => profiles[key] === profile);
      assert.ok(mode);
      return { reset: async () => { calls.events.push(`reset:${mode}`); } };
    } },
    './installer-shortcuts.cjs': {
      shortcutPlan: (_executable, _desktop, _programs, selected) => Array.from(selected),
      applyShortcuts: async (_shell, selected) => {
        calls.events.push(`shortcuts:${selected.join(',')}`);
        await options.shortcuts?.(selected);
      },
    },
  };
  const exported = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'installer.cjs'), 'utf8'), {
    module: exported, exports: exported.exports, Buffer, setTimeout, clearTimeout,
    process: { env: {}, resourcesPath: path.join(root, 'resources'), execPath: path.join(root, 'StandTermDesktop.exe') },
    require: name => {
      if (Object.hasOwn(modules, name)) return modules[name];
      if (name === 'node:path') return path;
      throw new Error(`Unexpected installer dependency: ${name}`);
    },
  }, { filename: 'installer.cjs' });
  return { calls,
    run: request => exported.exports.runInstaller({ parent: 123, modes: [], ...request }),
    loseWatcher: () => watcher.emit('exit', 0),
    assertUnchanged: () => assert.deepEqual(snapshot(), initialPreferences),
  };
}

const preference = locale => ({ version: 1, locale });
const rawError = 'Failed at C:\\User {retained}\\core: permission denied <details>';

for (const locale of ['en', 'zh-TW']) {
  test(`installer cleanup summary uses ${locale} and preserves separate result counts and recovery paths`, async context => {
    const f = fixture(context, { windows: preference(locale), wsl: preference(locale) }, {
      cleanup: mode => mode === 'windows'
        ? { mode, status: 'checked', results: [{ status: 'detached' }, { status: 'detached' }, { status: 'retained' }] }
        : { mode, status: 'retained' },
    });
    assert.equal(await f.run({ action: 'uninstall', cleanup: true }), 0);
    const [dialog] = f.calls.dialogs;
    const { t } = create(locale);
    assert.notEqual(t('desktop.installer.cleanup_title'), 'desktop.installer.cleanup_title');
    assert.equal(dialog.title, t('desktop.installer.cleanup_title'));
    assert.equal(dialog.message, t('desktop.installer.cleanup_summary', { detached: 2, retained: 2, unknown: 0 }));
    assert.ok(dialog.detail.includes('%LOCALAPPDATA%\\StandTermDesktop\\venv-recovery'));
    assert.ok(dialog.detail.includes('~/.local/share/standterm-desktop/venv-recovery'));
    assert.deepEqual(Array.from(dialog.buttons), [t('desktop.installer.continue_uninstall')]);
    assert.deepEqual(f.calls.events, ['cleanup:windows', 'cleanup:wsl', 'shortcuts:']);
    assert.equal(f.calls.dialogs.length, 1);
    assert.equal(f.calls.launches.length, 1);
    assert.match(f.calls.launches[0].executable, /powershell\.exe$/);
    assert.equal(f.calls.launches[0].options.shell, false);
    assert.equal(f.calls.watcherKills, 1);
    assert.deepEqual(f.calls.exit, []);
    f.assertUnchanged();
  });

  test(`installer setup failure uses ${locale} while preserving the original error and source reset order`, async context => {
    const f = fixture(context, { windows: preference(locale), wsl: preference(locale) }, {
      prepare: async mode => { if (mode === 'wsl') throw new Error(rawError); },
    });
    assert.equal(await f.run({ action: 'prepare', modes: ['windows', 'wsl'] }), 1);
    const [dialog] = f.calls.dialogs;
    const { t } = create(locale);
    assert.equal(dialog.title, t('desktop.installer.setup_failed_title'));
    assert.equal(dialog.message, rawError);
    assert.equal(dialog.detail, t('desktop.installer.setup_failed_detail'));
    assert.deepEqual(Array.from(dialog.buttons), [t('desktop.installer.return_to_installer')]);
    assert.deepEqual(f.calls.events, ['prepare:windows', 'reset:windows', 'prepare:wsl']);
    assert.equal(f.calls.stopSetup, 1);
    assert.equal(f.calls.dialogs.length, 1);
    f.assertUnchanged();
  });
}

test('installer summary falls back to English for mixed, missing or invalid profile preferences without writing them', async context => {
  for (const preferences of [
    { windows: preference('zh-TW'), wsl: preference('en') },
    { windows: preference('zh-TW') },
    { windows: preference('zh-TW'), wsl: '{malformed' },
    { windows: preference('zh-TW'), wsl: preference('zh-CN') },
    { windows: preference('zh-TW'), wsl: { version: 99, locale: 'zh-TW' } },
    {},
  ]) {
    const f = fixture(context, preferences);
    assert.equal(await f.run({ action: 'uninstall', cleanup: true }), 0);
    assert.equal(f.calls.dialogs[0].title, create('en').t('desktop.installer.cleanup_title'));
    f.assertUnchanged();
  }
});

test('single-mode preparation uses only its selected profile language', async context => {
  for (const mode of ['windows', 'wsl']) {
    const other = mode === 'windows' ? 'wsl' : 'windows';
    const f = fixture(context, { [mode]: preference('zh-TW'), [other]: preference('en') }, {
      prepare: async () => { throw new Error(rawError); },
    });
    assert.equal(await f.run({ action: 'prepare', modes: [mode] }), 1);
    assert.equal(f.calls.dialogs[0].title, create('zh-TW').t('desktop.installer.setup_failed_title'));
    assert.deepEqual(f.calls.events, [`prepare:${mode}`]);
    f.assertUnchanged();
  }
});

test('preparation resets each selected source before the next mode and publishes shortcuts only after both finish', async context => {
  const f = fixture(context);
  assert.equal(await f.run({ action: 'prepare', modes: ['windows', 'wsl'] }), 0);
  assert.deepEqual(f.calls.events, ['prepare:windows', 'reset:windows', 'prepare:wsl', 'reset:wsl', 'shortcuts:windows,wsl']);
  assert.deepEqual(f.calls.dialogs, []);
  f.assertUnchanged();
});

test('cleanup exceptions count as unknown modes rather than retained entries', async context => {
  const f = fixture(context, { windows: preference('zh-TW'), wsl: preference('zh-TW') }, {
    cleanup: async mode => { if (mode === 'wsl') throw new Error('WSL unavailable'); return { mode, status: 'retained' }; },
  });
  assert.equal(await f.run({ action: 'uninstall', cleanup: true }), 0);
  assert.equal(f.calls.dialogs[0].message, create('zh-TW').t('desktop.installer.cleanup_summary', {
    detached: 0, retained: 1, unknown: 1,
  }));
});

test('installer cancellation remains exit code 2 and other setup failures remain exit code 1', async context => {
  for (const [code, expected] of [['SETUP_CANCELED', 2], ['OTHER', 1]]) {
    const f = fixture(context, { windows: preference('zh-TW') }, {
      prepare: async () => { throw Object.assign(new Error(rawError), { code }); },
    });
    assert.equal(await f.run({ action: 'prepare', modes: ['windows'] }), expected);
    assert.equal(f.calls.dialogs[0].message, rawError);
    assert.deepEqual(f.calls.events, ['prepare:windows']);
    assert.equal(f.calls.stopSetup, 1);
  }
});

test('losing the owning installer prevents dialogs, source reset, later preparation and shortcuts', async context => {
  const f = fixture(context, { windows: preference('zh-TW'), wsl: preference('zh-TW') }, {
    prepare: async () => { f.loseWatcher(); },
  });
  assert.equal(await f.run({ action: 'prepare', modes: ['windows', 'wsl'] }), 1);
  assert.deepEqual(f.calls.events, ['prepare:windows']);
  assert.deepEqual(f.calls.dialogs, []);
  assert.deepEqual(f.calls.exit, [2]);
  assert.equal(f.calls.watcherKills, 1);
  f.assertUnchanged();
});

test('losing the owning installer during cleanup prevents the summary and shortcut removal', async context => {
  const f = fixture(context, { windows: preference('zh-TW'), wsl: preference('zh-TW') }, {
    cleanup: async mode => { f.loseWatcher(); return { mode, status: 'retained' }; },
  });
  assert.equal(await f.run({ action: 'uninstall', cleanup: true }), 1);
  assert.deepEqual(f.calls.events, ['cleanup:windows']);
  assert.deepEqual(f.calls.dialogs, []);
  assert.deepEqual(f.calls.exit, [2]);
  assert.equal(f.calls.watcherKills, 1);
  f.assertUnchanged();
});

test('uninstall failure after confirmed recovery moves uses the partial-operation notice', async context => {
  for (const locale of ['en', 'zh-TW']) {
    const f = fixture(context, { windows: preference(locale), wsl: preference(locale) }, {
      cleanup: async mode => ({ mode, status: 'checked', results: [{ status: 'detached' }] }),
      shortcuts: async () => { throw new Error(rawError); },
    });
    assert.equal(await f.run({ action: 'uninstall', cleanup: true }), 1);
    const { t } = create(locale);
    assert.equal(f.calls.dialogs[0].message, t('desktop.installer.cleanup_summary', { detached: 2, retained: 0, unknown: 0 }));
    const failure = f.calls.dialogs[1];
    assert.equal(failure.message, rawError);
    assert.equal(failure.detail, t('desktop.installer.setup_failed_detail'));
    assert.ok(failure.detail.includes('venv-recovery'));
    assert.deepEqual(f.calls.events, ['cleanup:windows', 'cleanup:wsl', 'shortcuts:']);
  }
});
