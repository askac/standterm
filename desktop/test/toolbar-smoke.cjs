'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { app, BrowserWindow, Menu, clipboard, dialog } = require('electron');

async function run(win, contents, browserAccess) {
  const evaluate = script => contents.executeJavaScript(script, true);
  const until = async (check, stage) => {
    const deadline = Date.now() + 15000;
    while (!await check()) {
      if (Date.now() > deadline) throw new Error(`Desktop toolbar smoke timed out: ${stage}; ${JSON.stringify({
        visible: win.isVisible(), minimized: win.isMinimized(), focused: win.isFocused(),
        expectedWindow: win.id, focusedWindow: BrowserWindow.getFocusedWindow()?.id,
      })}`);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  };
  const menu = Menu.getApplicationMenu();
  assert.deepEqual(menu.items.map(item => item.id), ['standterm', 'edit', 'agent-menu', 'view', 'diagnostics']);
  assert.deepEqual(await win.webContents.executeJavaScript(`({
    menusHidden: document.getElementById('menus').hidden,
    titleHidden: document.getElementById('mac-title').hidden,
  })`), { menusHidden: process.platform === 'darwin', titleHidden: process.platform !== 'darwin' });
  assert.notEqual(win.webContents.session, contents.session);
  assert.equal(win.webContents.backgroundThrottling, false);
  assert.deepEqual(await win.webContents.session.cookies.get({}), []);
  assert.equal(await evaluate('typeof window.desktopToolbar'), 'undefined');
  assert.equal(contents.getLastWebPreferences().preload, undefined);
  assert.equal(await win.webContents.executeJavaScript('typeof require'), 'undefined');
  assert.equal(await win.webContents.executeJavaScript("window.desktopToolbar.invoke('unknown')"), false);
  assert.equal(await win.webContents.executeJavaScript("fetch('https://example.com').then(() => false, () => true)"), true);
  await until(() => evaluate('window.standtermUi?.snapshot().ready === true'), 'Core ready');
  assert.equal(await evaluate('innerHeight'), win.getContentSize()[1] - 36, 'initial Core bounds must exclude the Desktop toolbar');
  const copied = [];
  const originalCopy = clipboard.writeText;
  const originalDialog = dialog.showMessageBox;
  clipboard.writeText = value => copied.push(value);
  dialog.showMessageBox = async () => ({ response: 1 });
  try {
    assert.equal(await browserAccess.run('copy-token'), true, 'native access-token retrieval must use the authenticated Core session');
    assert.equal(await browserAccess.run('copy-auth'), true, 'native browser authorization must pass the launcher check');
    assert.equal(copied.length, 2);
    const auth = new URL(copied[1]);
    assert.ok(auth.searchParams.get('token') === copied[0]);
    assert.ok(auth.searchParams.get('authorize'));
    assert.equal(auth.origin, new URL(contents.getURL()).origin);
  } finally { clipboard.writeText = originalCopy; dialog.showMessageBox = originalDialog; }
  const originalSize = win.getSize();
  if (win.isMinimized()) win.restore();
  win.show(); win.moveTop(); win.focus(); contents.focus();
  await until(() => BrowserWindow.getFocusedWindow() === win, 'main focus');
  await until(() => menu.getMenuItemById('ui-settings').enabled, 'Settings enabled');
  menu.getMenuItemById('ui-settings').click();
  await until(() => evaluate("document.getElementById('settings-modal').classList.contains('open')"), 'Settings opened');
  await evaluate("document.getElementById('settings-close').click()");
  const original = await evaluate('window.terminalTest.getTerminalTabsState().tabs.length');
  await until(() => menu.getMenuItemById('ui-newTab').enabled, 'New Tab enabled');
  menu.getMenuItemById('ui-newTab').click();
  await until(async () => await evaluate('window.terminalTest.getTerminalTabsState().tabs.length') === original + 1, 'New Tab created');
  assert.equal(await evaluate("window.standtermUi.run('closeTab', 'wrong-target')"), false);
  const other = new BrowserWindow({ width: 300, height: 200, show: false,
    webPreferences: { sandbox: true, nodeIntegration: false, contextIsolation: true } });
  try {
    await other.loadURL('data:text/html,<p>Auxiliary window focus fixture</p>');
    other.show(); other.focus();
    await until(() => BrowserWindow.getFocusedWindow() === other, 'auxiliary focus');
    menu.getMenuItemById('ui-closeTab').click();
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(await evaluate('window.terminalTest.getTerminalTabsState().tabs.length'), original + 1);
  } finally { other.destroy(); }
  win.focus(); contents.focus();
  await until(() => BrowserWindow.getFocusedWindow() === win, 'restored main focus');
  await until(() => menu.getMenuItemById('ui-closeTab').enabled, 'Close Tab enabled');
  menu.getMenuItemById('ui-closeTab').click();
  await until(async () => await evaluate('window.terminalTest.getTerminalTabsState().tabs.length') === original, 'Tab closed');
  win.setSize(640, 600);
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(await evaluate(`['new-tab-btn', 'agent-toggle-btn', 'sftp-status-btn', 'quick-settings'].every(id => {
    const element = document.getElementById(id);
    if (element.hidden) return true;
    const bounds = element.getBoundingClientRect();
    return bounds.width > 0 && bounds.x >= 0 && bounds.right <= innerWidth && bounds.top >= 0;
  })`), true);
  assert.equal(await evaluate("document.querySelector('#status-bar #new-tab-btn, #status-bar #quick-settings, #status-bar #agent-pause-btn') === null"), true);
  assert.equal(await evaluate('innerHeight'), win.getContentSize()[1] - 36);
  const directory = await fs.mkdtemp(path.join(app.isPackaged ? app.getPath('temp') : path.join(__dirname, '..', 'dist'), 'toolbar-smoke-'));
  const screenshotCss = await contents.insertCSS('#debug-hud, #policy-debug-panel, #payload-log { display: none !important; }');
  console.log('Desktop toolbar smoke: capturing the Core preview.');
  try {
    await fs.writeFile(path.join(directory, 'core.png'), (await contents.capturePage()).toPNG(), { flag: 'wx' });
  } catch (error) {
    console.error('Core preview state:', JSON.stringify({ visible: win.isVisible(), focused: win.isFocused(),
      minimized: win.isMinimized(), page: await evaluate('({visibility:document.visibilityState,width:innerWidth,height:innerHeight})') }));
    throw error;
  }
  console.log('Desktop toolbar smoke: capturing the toolbar preview.');
  await fs.writeFile(path.join(directory, 'toolbar.png'), (await win.webContents.capturePage({ x: 0, y: 0, width: win.getContentSize()[0], height: 36 })).toPNG(), { flag: 'wx' });
  await contents.removeInsertedCSS(screenshotCss);
  win.setSize(...originalSize);
  if (process.platform === 'darwin') {
    const toolbar = script => win.webContents.executeJavaScript(script);
    const visible = () => toolbar("document.getElementById('notice-area').classList.contains('visible')");
    const background = new BrowserWindow({ width: 300, height: 200, show: false,
      webPreferences: { sandbox: true, nodeIntegration: false, contextIsolation: true } });
    try {
      await background.loadURL('data:text/html,<p>Background timer fixture</p>');
      background.show(); background.focus();
      await until(() => BrowserWindow.getFocusedWindow() === background, 'background timer focus');
      for (const [noticeId, error, minimumMs] of [[-1, false, 4900], [-2, true, 9900]]) {
        const started = Date.now();
        win.webContents.send('standterm-toolbar-state', { notice: 'Isolated Mac notice fixture.', noticeId, error });
        await until(visible, 'notice visible');
        await until(async () => !await visible(), 'notice fade');
        assert.ok(Date.now() - started >= minimumMs, 'notice must not disappear early');
        await until(async () => await toolbar("document.getElementById('notice').textContent") === '', 'notice text cleared');
      }
    } finally { background.destroy(); }
    win.focus(); contents.focus();
    await until(() => BrowserWindow.getFocusedWindow() === win, 'focus after background notices');
    const layout = await toolbar(`({
      scale: devicePixelRatio, width: innerWidth,
      reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
      transition: getComputedStyle(document.getElementById('notice-area')).transitionDuration,
      controlsFit: [...document.querySelectorAll('#capture-tools button')].filter(element => !element.hidden).every(element => {
        const bounds = element.getBoundingClientRect(); return bounds.x >= 0 && bounds.right <= innerWidth;
      }),
    })`);
    assert.ok(layout.controlsFit);
    assert.ok(layout.scale >= 1);
    if (layout.reducedMotion) assert.equal(layout.transition, '0s');
    console.log(`macOS toolbar renderer: native menu, background five/ten-second notices and wide layout passed; ${JSON.stringify(layout)}`);
  }
  console.log(`Desktop toolbar smoke: isolated SVG toolbar, focus guards, native Settings/tab actions and compact layout passed (${directory}).`);
}

module.exports = { run };
