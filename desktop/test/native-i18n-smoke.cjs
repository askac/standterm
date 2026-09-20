'use strict';

// Run with Windows Electron, not ELECTRON_RUN_AS_NODE. Only owned windows are captured.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { app, BrowserWindow, Menu, ipcMain, session } = require('electron');

const [stageArgument, evidenceArgument] = process.argv.slice(2);
assert.ok(stageArgument && evidenceArgument, 'Usage: electron native-i18n-smoke.cjs <desktop-stage> <evidence-dir>');
assert.equal(process.platform, 'win32', 'This probe qualifies Windows rendering only');
const stage = path.resolve(stageArgument);
const evidence = path.resolve(evidenceArgument);
fs.mkdirSync(evidence, { recursive: true });
app.setPath('userData', path.join(evidence, 'profile'));
app.setPath('sessionData', path.join(evidence, 'session'));
app.setAppLogsPath(path.join(evidence, 'logs'));
const { create } = require(path.join(stage, 'i18n.js'));
const { createUiCommands, UI_ACTIONS } = require(path.join(stage, 'ui-commands.cjs'));
const results = { platform: process.platform, electron: process.versions.electron,
  stage, checks: [], screenshots: [], limitations: [
    'No Core, installer, clipboard, capture encoder, or user profile is exercised.',
    'Setup process events are fixtures; HTML and generated scripts are rendered by Windows Electron.',
    'Menu labels are checked through the native Menu API; OS dialog interaction is not qualified.',
  ] };
const windows = new Set();
const errors = [];
let finished = false;
const timeout = setTimeout(() => finish(new Error('Native localization probe timed out')), 45000);

function finish(error) {
  if (finished) return;
  finished = true;
  clearTimeout(timeout);
  if (error) results.error = error.stack || String(error);
  results.passed = !error;
  results.rendererErrors = errors;
  for (const win of windows) if (!win.isDestroyed()) win.destroy();
  fs.writeFileSync(path.join(evidence, 'native-i18n-result.json'), JSON.stringify(results, null, 2) + '\n');
  process.stdout.write(JSON.stringify(results) + '\n');
  app.exit(error ? 1 : 0);
}

function createWindow(width, height, preload) {
  const partition = `native-i18n-${windows.size}-${Date.now()}`;
  const isolated = session.fromPartition(partition);
  const allowed = new Set(['toolbar.html', 'toolbar.css', 'toolbar.js', 'toolbar-preload.cjs',
    'messages.js', 'i18n.js', 'setup.html'].map(name => pathToFileURL(path.join(stage, name)).href));
  isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !allowed.has(details.url) }));
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  const win = new BrowserWindow({ width, height, useContentSize: true, show: false,
    title: 'StandTerm localization acceptance probe',
    webPreferences: { preload, session: isolated, sandbox: true, contextIsolation: true,
      nodeIntegration: false, backgroundThrottling: false } });
  windows.add(win);
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.on('console-message', (_event, level, message) => {
    if (level >= 3) errors.push(message);
  });
  win.webContents.on('render-process-gone', (_event, detail) => errors.push(JSON.stringify(detail)));
  return win;
}

async function capture(win, name) {
  await win.webContents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  const file = `${name}.png`;
  fs.writeFileSync(path.join(evidence, file), (await win.webContents.capturePage()).toPNG());
  results.screenshots.push(file);
}

async function setupSnapshots() {
  // Reuse the existing process fixture against the staged setup module and catalog.
  const filename = path.join(__dirname, 'setup-i18n-fixture.cjs');
  const nativeRequire = createRequire(filename);
  let output = '';
  const context = { __dirname: path.join(stage, 'test'), Buffer, setTimeout, clearTimeout,
    console, process: { stdout: { write: text => { output += text; } } },
    require: name => name === '../i18n.js' ? require(path.join(stage, 'i18n.js')) : nativeRequire(name) };
  await vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context, { filename });
  assert.equal(context.process.exitCode, undefined, 'Setup fixture generation failed');
  return JSON.parse(output);
}

async function run() {
  await app.whenReady();
  Menu.setApplicationMenu(null);
  const toolbarStates = new Map();
  ipcMain.handle('standterm-toolbar-action', (event, action) => {
    assert.equal(action, 'ready', 'The probe never invokes terminal or clipboard actions');
    event.sender.send('standterm-toolbar-state', toolbarStates.get(event.sender.id));
    return true;
  });
  for (const locale of ['en', 'zh-TW']) {
    const { t } = create(locale);
    const win = createWindow(640, 180, path.join(stage, 'toolbar-preload.cjs'));
    toolbarStates.set(win.webContents.id, { locale, mac: false, state: 'idle' });
    await win.loadFile(path.join(stage, 'toolbar.html'));
    win.show();
    const commands = createUiCommands(win, win.webContents, 'http://127.0.0.1:1', t);
    const menu = Menu.buildFromTemplate(Object.keys(UI_ACTIONS).map(action => commands.item(action)));
    for (const [action, key] of Object.entries(UI_ACTIONS)) {
      assert.equal(menu.getMenuItemById(`ui-${action}`).label, t(key));
    }
    results.checks.push({ locale, nativeMenuLabels: menu.items.map(item => item.label) });
    for (const state of ['idle', 'recording', 'paused']) {
      const label = state === 'idle' ? '' : t(`desktop.capture.status_${state}`, { time: '00:04' });
      win.webContents.send('standterm-toolbar-state', { locale, mac: false, state, label });
      await capture(win, `toolbar-${locale}-${state}`);
      const actual = await win.webContents.executeJavaScript(`(() => ({
        locale: document.documentElement.lang,
        label: document.getElementById('recording-status').textContent,
        pause: document.getElementById('pause').getAttribute('aria-label'),
        csp: document.querySelector('meta[http-equiv="Content-Security-Policy"]').content,
        buttons: [...document.querySelectorAll('button')].filter(button => !button.hidden && button.getBoundingClientRect().width)
          .map(button => ({ id: button.id || button.dataset.menu, left: button.getBoundingClientRect().left,
            right: button.getBoundingClientRect().right, viewport: innerWidth }))
      }))()`);
      assert.equal(actual.locale, locale);
      assert.equal(actual.label, label);
      assert.ok(actual.csp.includes("default-src 'none'"));
      assert.ok(actual.buttons.every(button => button.left >= 0 && button.right <= button.viewport), JSON.stringify(actual));
      assert.equal(actual.pause, t(state === 'paused' ? 'desktop.toolbar.record_resume' : 'desktop.toolbar.record_pause'));
      results.checks.push({ locale, toolbarState: state, ...actual });
    }
    win.close();
  }
  for (const snapshot of await setupSnapshots()) {
    const win = createWindow(700, 500);
    await win.loadFile(path.join(stage, 'setup.html'));
    await win.webContents.executeJavaScript(snapshot.init);
    win.show();
    const actual = await win.webContents.executeJavaScript(`(() => ({
      locale: document.documentElement.lang, title: document.title,
      requirements: document.getElementById('requirements').textContent,
      scope: document.getElementById('scope').textContent,
      overflow: document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight,
      injected: typeof window.injected !== 'undefined', elements: document.querySelectorAll('script,img,iframe,a').length
    }))()`);
    assert.equal(actual.locale, snapshot.locale);
    assert.equal(actual.title, snapshot.title);
    assert.equal(actual.requirements, snapshot.requirements);
    assert.equal(actual.scope, snapshot.scope);
    assert.equal(actual.overflow, false);
    assert.equal(actual.injected, false);
    assert.equal(actual.elements, 0);
    for (const frame of snapshot.progress) {
      await win.webContents.executeJavaScript(frame.script);
      assert.equal(await win.webContents.executeJavaScript('document.getElementById("stage").textContent'), frame.expected);
    }
    await capture(win, `setup-${snapshot.locale}-${snapshot.mode}`);
    await win.webContents.executeJavaScript(snapshot.cancel);
    assert.equal(await win.webContents.executeJavaScript('document.getElementById("stage").textContent'), snapshot.canceling);
    results.checks.push({ locale: snapshot.locale, setupMode: snapshot.mode, ...actual });
    win.close();
  }
  assert.deepEqual(errors, []);
  finish();
}

// Keep the probe alive between its separately owned windows.
app.on('window-all-closed', () => {});
run().catch(finish);
