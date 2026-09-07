'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const { Menu, dialog, BrowserWindow, clipboard } = require('electron');

async function waitFor(win, predicate) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await win.webContents.executeJavaScript(predicate)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Desktop smoke timed out waiting for terminal state.');
}

async function run(win, origin) {
  const menu = Menu.getApplicationMenu();
  assert.equal(menu.getMenuItemById('diagnostics-origin').label, `URL: ${origin}`);
  const coreVersionLabel = menu.getMenuItemById('diagnostics-core-version').label;
  assert.match(coreVersionLabel, /^Core version: \d+\.\d+\.\d+/);
  const originalAboutDialog = dialog.showMessageBox;
  let about;
  dialog.showMessageBox = async (_owner, options) => { about = options; return { response: 0 }; };
  try { menu.getMenuItemById('desktop-about').click(); }
  finally { dialog.showMessageBox = originalAboutDialog; }
  assert.ok(about.detail.includes(coreVersionLabel));
  assert.ok(about.detail.includes('Core bundle SHA-256:'));
  assert.ok(about.detail.includes(`Electron: ${process.versions.electron}`));
  const originalCopy = clipboard.writeText;
  const copied = [];
  clipboard.writeText = value => copied.push(value);
  try {
    menu.getMenuItemById('diagnostics-copy-origin').click();
    menu.getMenuItemById('diagnostics-copy-agent').click();
  } finally { clipboard.writeText = originalCopy; }
  assert.equal(copied[0], origin);
  const connection = JSON.parse(copied[1]);
  assert.equal(connection.base_url, origin);
  assert.equal(connection.agentinfo_url, origin + '/agentinfo');
  assert.ok(connection.instance_id);
  assert.ok(['windows', 'wsl'].includes(connection.backend_mode));
  assert.deepEqual(Object.keys(connection).sort(),
    ['schema', 'schema_version', 'base_url', 'agentinfo_url', 'instance_id', 'backend_mode'].sort());
  assert.equal(win.webContents.isDevToolsOpened(), false);
  menu.getMenuItemById('diagnostics-status').click();
  const status = BrowserWindow.getAllWindows().find(candidate => candidate !== win);
  await waitFor(status, "document.body?.innerText.includes('Recent startup events') === true");
  assert.notEqual(status.webContents.session, win.webContents.session);
  assert.equal(status.webContents.getLastWebPreferences().sandbox, true);
  assert.equal(status.webContents.getLastWebPreferences().preload, undefined);
  const diagnosticState = await status.webContents.executeJavaScript(`(async () => ({
    node: typeof require, text: document.body.innerText,
    blocked: await fetch('https://example.com').then(() => false, () => true)
  }))()`);
  assert.equal(diagnosticState.node, 'undefined');
  assert.equal(diagnosticState.blocked, true);
  assert.ok(diagnosticState.text.includes(origin));
  assert.ok(diagnosticState.text.includes('Recent startup events'));
  assert.ok(diagnosticState.text.includes(coreVersionLabel.replace('Core version: ', '')));
  assert.deepEqual(await status.webContents.session.cookies.get({}), []);
  status.destroy();
  menu.getMenuItemById('diagnostics-status').click();
  menu.getMenuItemById('diagnostics-status').click();
  const reopened = BrowserWindow.getAllWindows().filter(candidate => candidate !== win);
  assert.equal(reopened.length, 1);
  await waitFor(reopened[0], "document.body?.innerText.includes('Recent startup events') === true");
  reopened[0].destroy();
  const originalMessage = dialog.showMessageBox;
  dialog.showMessageBox = async () => ({ response: 0 });
  try { await menu.getMenuItemById('diagnostics-devtools').click(); }
  finally { dialog.showMessageBox = originalMessage; }
  assert.equal(win.webContents.isDevToolsOpened(), false);
  await waitFor(win, '!!window.terminalTest && window.terminalTest.getSocketState().connected');
  const isolated = await win.webContents.executeJavaScript(`({
    requireType: typeof require, processType: typeof process,
    cookie: document.cookie, url: location.href,
    loginVisible: !!document.getElementById('access-token')
  })`);
  assert.equal(isolated.requireType, 'undefined');
  assert.equal(isolated.processType, 'undefined');
  assert.equal(isolated.loginVisible, false);
  assert.equal(isolated.cookie, '');
  assert.equal(new URL(isolated.url).searchParams.has('token'), false);
  const prefs = win.webContents.getLastWebPreferences();
  assert.equal(prefs.sandbox, true);
  assert.equal(prefs.contextIsolation, true);
  assert.equal(prefs.nodeIntegration, false);
  const unauthenticatedStatus = await new Promise((resolve, reject) => {
    http.get(origin, res => { res.resume(); resolve(res.statusCode); }).on('error', reject);
  });
  assert.equal(unauthenticatedStatus, 401);
  await waitFor(win, "!!document.querySelector('#connectBtn:not([disabled])')");
  await win.webContents.executeJavaScript("document.getElementById('connectBtn').click()");
  await waitFor(win, 'window.terminalTest.getActiveAgentState()?.connected === true');
  await win.webContents.executeJavaScript(`window.terminalTest.emitSocket('ssh_input', {
    terminal_id: window.terminalTest.getTerminalTabsState().activeTerminalId,
    data: 'echo STANDTERM_SMOKE_IO\\r'
  })`);
  await waitFor(win, `Array.from({length: 100}, (_, row) =>
    (window.terminalTest.getActiveTerminalBufferCellsForTest(row) || []).map(cell => cell?.chars || '').join('').trim()
  ).includes('STANDTERM_SMOKE_IO')`);
  // Reload must reattach the existing backend terminal, not create a new shell.
  await win.loadURL(`${origin}/?debug=1`);
  await waitFor(win, '!!window.terminalTest && window.terminalTest.getActiveAgentState()?.connected === true');
  assert.equal(await win.webContents.executeJavaScript(
    'window.terminalTest.getTerminalTabsState().tabs.length',
  ), 1);
  await require('./test/external-links-smoke.cjs').run(win.webContents, true);
  const popup = await win.webContents.executeJavaScript("window.open('file:///blocked') === null");
  assert.equal(popup, true);
  // A second loopback service is outside the allowed origin too.
  const denied = await win.webContents.executeJavaScript(`fetch('http://127.0.0.1:1/')
    .then(() => false, () => true)`);
  assert.equal(denied, true);
  await require('./test/floating-smoke.cjs').run(win, origin);
}

module.exports = { run };
