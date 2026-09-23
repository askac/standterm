'use strict';

// Run with native Electron. Uses a disposable profile and never starts Core.
const { app, BrowserWindow, screen } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { create } = require('../i18n.js');
const { createStartupWindow } = require('../startup-window.cjs');
const { loadWindowState, trackWindowState } = require('../window-state.cjs');

app.enableSandbox();
if (process.platform === 'win32') app.commandLine.appendSwitch('disable-features', 'CalculateNativeWinOcclusion');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-startup-smoke-'));
app.setPath('userData', profile);
app.on('window-all-closed', () => {});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check) {
  const deadline = Date.now() + 5000;
  while (!check()) {
    if (Date.now() >= deadline) throw new Error('Window transition timed out.');
    await delay(50);
  }
}
setTimeout(() => { console.error('Startup window smoke timed out.'); app.exit(1); }, 30000).unref();
app.whenReady().then(async () => {
  for (const locale of ['en', 'zh-TW']) {
    console.log(`Checking startup window: ${locale}`);
    const splash = createStartupWindow('StandTerm Desktop (WSL)', create(locale));
    await splash.ready;
    const win = BrowserWindow.getAllWindows()[0];
    assert.ok(win.isVisible());
    assert.equal(await win.webContents.executeJavaScript('document.documentElement.lang'), locale);
    assert.equal(await win.webContents.executeJavaScript('document.querySelector("[role=status]").textContent'),
      create(locale).t('desktop.startup.starting'));
    win.minimize(); await until(() => win.isMinimized());
    splash.focus(); await until(() => !win.isMinimized() && win.isVisible());
    if (process.env.STANDTERM_STARTUP_SMOKE_OUTPUT) {
      const output = process.env.STANDTERM_STARTUP_SMOKE_OUTPUT;
      fs.mkdirSync(output, { recursive: true });
      await delay(200);
      fs.writeFileSync(path.join(output, `startup-${locale}.png`), (await win.webContents.capturePage()).toPNG());
    }
    splash.close(); splash.close();
    assert.ok(win.isDestroyed());
  }
  const file = path.join(profile, 'window-wsl.json');
  console.log('Checking native window state transitions.');
  let initial = loadWindowState(file, screen);
  let win = new BrowserWindow({ ...initial.options, show: false });
  win.setMenu(null);
  let tracker = trackWindowState(win, file, initial);
  await win.loadURL('data:text/html,<title>Window state smoke</title>');
  win.show();
  win.setBounds({ x: initial.options.x, y: initial.options.y, width: 800, height: 600 });
  await delay(300);
  const normal = win.getNormalBounds();
  win.maximize(); await until(() => win.isMaximized()); await delay(300);
  tracker.save();
  assert.deepEqual(JSON.parse(fs.readFileSync(file)), { version: 1, bounds: normal, maximized: true });
  win.minimize(); await until(() => win.isMinimized()); await delay(300);
  tracker.save();
  assert.equal(JSON.parse(fs.readFileSync(file)).maximized, true);
  win.destroy();
  initial = loadWindowState(file, screen);
  win = new BrowserWindow({ ...initial.options, show: false });
  win.setMenu(null);
  tracker = trackWindowState(win, file, initial);
  await win.loadURL('data:text/html,<title>Restored window smoke</title>');
  win.setBounds({ x: initial.options.x, y: initial.options.y, width: initial.options.width, height: initial.options.height });
  if (initial.maximized) win.maximize();
  win.show(); await until(() => win.isMaximized());
  win.unmaximize(); await until(() => !win.isMaximized()); await delay(300);
  for (const key of ['x', 'y', 'width', 'height']) {
    assert.ok(Math.abs(win.getNormalBounds()[key] - normal[key]) <= 1, `Restored ${key} drifted beyond native rounding.`);
  }
  tracker.save();
  assert.equal(JSON.parse(fs.readFileSync(file)).maximized, false);
  assert.deepEqual(JSON.parse(fs.readFileSync(file)).bounds, normal);
  win.hide(); tracker.save(); win.destroy();
  assert.equal(loadWindowState(path.join(profile, 'window-windows.json'), screen).maximized, false);
  console.log('Startup/window smoke passed: bilingual splash, focus, bounds, maximize/minimize, reopen and profile isolation.');
  app.exit(0);
}).catch(error => { console.error(error.stack); app.exit(1); });
