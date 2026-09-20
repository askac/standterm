'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { create } = require('../i18n.js');

const filename = path.join(__dirname, '..', 'setup.cjs');
const nativeRequire = createRequire(filename);
const source = fs.readFileSync(filename, 'utf8');
const raw = 'Distro </p><img src="https://example.invalid/private" onerror="window.injected=true"> <script>window.injected=true</script> {distro}';

async function snapshot(locale, mode) {
  const language = create(locale);
  const { t } = language;
  const scripts = [];
  const titles = [];
  let child;
  let spawned;
  const preparing = new Promise(resolve => { spawned = resolve; });
  class Window extends EventEmitter {
    constructor(options) {
      super();
      assert.equal(options.webPreferences.sandbox, true);
      assert.equal(options.webPreferences.nodeIntegration, false);
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = callback => assert.equal(callback().action, 'deny');
      this.webContents.executeJavaScript = async script => { scripts.push(script); };
    }
    async loadURL(url) { assert.equal(url, pathToFileURL(path.join(path.dirname(filename), 'setup.html')).href); }
    isDestroyed() { return !!this.destroyed; }
    destroy() { this.destroyed = true; this.emit('closed'); }
    setTitle(title) { titles.push(title); }
  }
  const electron = { app: {}, BrowserWindow: Window,
    dialog: { showMessageBox: async (_window, options) => {
      assert.equal(options.defaultId, 0);
      assert.equal(options.cancelId, 0);
      return { response: 1 };
    } },
    session: { fromPartition: () => ({ setPermissionRequestHandler() {}, setPermissionCheckHandler() {},
      webRequest: { onBeforeRequest(callback) {
        callback({ url: 'https://example.invalid/' }, result => assert.equal(result.cancel, true));
      } } }) },
  };
  function spawn() {
    child = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.end = () => {};
    child.stdout = new EventEmitter();
    child.stderr = { resume() {} };
    child.kill = () => assert.fail('Cancellation must finish without forced termination');
    spawned();
    return child;
  }
  const context = vm.createContext({ module: { exports: {} }, __dirname: path.dirname(filename),
    require: name => name === 'electron' ? electron : name === 'node:child_process' ? { spawn } : nativeRequire(name),
    process: { env: {} }, Buffer, setTimeout, clearTimeout });
  vm.runInContext(source + '\nmodule.exports.fixture = { runPreparation, requestSetupCancel };', context, { filename });
  const fixture = context.module.exports.fixture;
  const completion = fixture.runPreparation({ language, t, executable: 'fixture', help: 'fixture help',
    windows: mode === 'windows', macos: mode === 'macos', distro: raw }, []);
  await preparing;
  assert.equal(scripts.length, 1);
  const init = scripts[0];
  const progress = [];
  function frame(stage) { child.stdout.emit('data', Buffer.from(JSON.stringify({ type: 'progress', stage }) + '\n')); }
  for (const stage of ['git', 'copy', 'venv', 'dependencies', 'verify']) {
    frame(stage);
    progress.push({ stage, script: scripts.at(-1), expected: t(`desktop.setup.stage_${stage}`) });
  }
  const beforeUnknown = scripts.length;
  for (const stage of ['__proto__', 'constructor', '<script>window.injected=true</script>']) frame(stage);
  assert.equal(scripts.length, beforeUnknown);
  assert.equal(await fixture.requestSetupCancel(), true);
  const cancel = scripts.at(-1);
  assert.equal(scripts.length, beforeUnknown + 1);
  frame('dependencies');
  assert.equal(scripts.length, beforeUnknown + 1);
  child.emit('close', 1);
  await assert.rejects(completion, error => error.code === 'SETUP_CANCELED');
  assert.deepEqual(titles, [t('desktop.setup.canceling_title')]);
  return { locale, mode, raw, init, progress, cancel,
    title: t('desktop.setup.progress_title'), heading: t('desktop.setup.progress_heading'),
    requirements: t(`desktop.setup.preparation_${mode}`, { distro: raw }),
    aria: t('desktop.setup.progress_aria'), scope: t('desktop.setup.progress_scope'),
    detail: t('desktop.setup.progress_detail'), closeHint: t('desktop.setup.progress_close_hint'),
    starting: t('desktop.setup.progress_starting'), canceling: t('desktop.setup.canceling_status') };
}

(async () => {
  const snapshots = [];
  for (const locale of ['en', 'zh-TW']) {
    for (const mode of ['windows', 'macos', 'wsl']) snapshots.push(await snapshot(locale, mode));
  }
  process.stdout.write(JSON.stringify(snapshots));
})().catch(error => { console.error(error); process.exitCode = 1; });
