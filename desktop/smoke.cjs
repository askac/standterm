'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const { Menu, dialog, BrowserWindow, clipboard } = require('electron');

async function waitFor(contents, predicate) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await contents.executeJavaScript(predicate)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Desktop smoke timed out waiting for terminal state.');
}

async function run(win, origin, contents = win.webContents, browserAccess) {
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
  } finally { clipboard.writeText = originalCopy; }
  assert.deepEqual(copied, [origin]);
  for (const id of ['diagnostics-copy-agent', 'agent-copy-connection', 'agent-copy-agentinfo',
    'agent-copy-install', 'agent-copy-usage', 'agent-copy-transfer']) {
    assert.equal(menu.getMenuItemById(id), null);
  }
  const agentinfo = await contents.executeJavaScript('fetch("/agentinfo").then(response => response.json())');
  assert.equal(typeof agentinfo.instance_id, 'string');
  assert.equal(agentinfo.agentinfo_url, origin + '/agentinfo');
  for (const name of ['standterm-external-agent', 'standterm-file-transfer', 'standterm-privileged-hitl']) {
    const skill = agentinfo.skills[name];
    assert.equal(skill.available, true, `Missing ${name} in the running Core`);
    for (const field of ['path', 'boot_prompt_path', 'install_prompt_path']) {
      assert.equal(typeof skill[field], 'string');
      assert.ok(skill[field].length);
    }
  }
  assert.deepEqual(agentinfo.skill, agentinfo.skills['standterm-external-agent']);
  for (const name of ['agent_scp', 'agent_rsfile', 'agent_mcp']) assert.equal(typeof agentinfo.scripts[name], 'string');
  assert.equal(contents.isDevToolsOpened(), false);
  menu.getMenuItemById('diagnostics-status').click();
  const status = BrowserWindow.getAllWindows().find(candidate => candidate !== win);
  await waitFor(status.webContents, "document.body?.innerText.includes('Recent startup events') === true");
  assert.notEqual(status.webContents.session, contents.session);
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
  await waitFor(reopened[0].webContents, "document.body?.innerText.includes('Recent startup events') === true");
  reopened[0].destroy();
  const originalMessage = dialog.showMessageBox;
  dialog.showMessageBox = async () => ({ response: 0 });
  try { await menu.getMenuItemById('diagnostics-devtools').click(); }
  finally { dialog.showMessageBox = originalMessage; }
  assert.equal(contents.isDevToolsOpened(), false);
  await waitFor(contents, '!!window.terminalTest && window.terminalTest.getSocketState().connected');
  const isolated = await contents.executeJavaScript(`({
    requireType: typeof require, processType: typeof process,
    cookie: document.cookie, url: location.href,
    loginVisible: !!document.getElementById('access-token')
  })`);
  assert.equal(isolated.requireType, 'undefined');
  assert.equal(isolated.processType, 'undefined');
  assert.equal(isolated.loginVisible, false);
  assert.equal(isolated.cookie, '');
  assert.equal(new URL(isolated.url).searchParams.has('token'), false);
  const prefs = contents.getLastWebPreferences();
  assert.equal(prefs.sandbox, true);
  assert.equal(prefs.contextIsolation, true);
  assert.equal(prefs.nodeIntegration, false);
  const unauthenticatedStatus = await new Promise((resolve, reject) => {
    http.get(origin, res => { res.resume(); resolve(res.statusCode); }).on('error', reject);
  });
  assert.equal(unauthenticatedStatus, 401);
  await waitFor(contents, "!!document.querySelector('#connectBtn:not([disabled])')");
  await contents.executeJavaScript("document.getElementById('connectBtn').click()");
  await waitFor(contents, 'window.terminalTest.getActiveAgentState()?.connected === true');
  await contents.executeJavaScript(`window.terminalTest.emitSocket('ssh_input', {
    terminal_id: window.terminalTest.getTerminalTabsState().activeTerminalId,
    data: 'echo STANDTERM_SMOKE_IO\\r'
  })`);
  await waitFor(contents, `Array.from({length: 100}, (_, row) =>
    (window.terminalTest.getActiveTerminalBufferCellsForTest(row) || []).map(cell => cell?.chars || '').join('').trim()
  ).includes('STANDTERM_SMOKE_IO')`);
  // Window activation must restore Core's native focus without a terminal click.
  const other = new BrowserWindow({ width: 300, height: 200, show: false,
    webPreferences: { sandbox: true, nodeIntegration: false, contextIsolation: true } });
  const evaluate = script => contents.executeJavaScript(script);
  const restored = async () => {
    await waitFor(contents, 'document.hasFocus()');
    assert.equal(BrowserWindow.getFocusedWindow(), win);
    assert.equal(contents.isFocused(), true);
    assert.equal(win.webContents.isFocused(), false);
  };
  const switchBack = async () => {
    other.show(); other.focus();
    await waitFor(other.webContents, 'document.hasFocus()');
    assert.equal(BrowserWindow.getFocusedWindow(), other);
    assert.equal(await evaluate('document.hasFocus()'), false);
    win.focus();
    await restored();
  };
  try {
    await other.loadURL('data:text/html,<p>Window activation fixture</p>');
    win.focus(); contents.focus();
    await evaluate(`document.querySelector('.terminal-pane.active .xterm-helper-textarea').focus();
      window.terminalTest.captureTerminalIoForTest()`);
    for (let attempt = 0; attempt < 3; attempt++) {
      await switchBack();
      assert.equal(await evaluate('window.terminalTest.activeTerminalHasFocus()'), true);
      await evaluate('window.terminalTest.clearEmitted()');
      contents.sendInputEvent({ type: 'keyDown', keyCode: 'Z' });
      contents.sendInputEvent({ type: 'char', keyCode: 'z' });
      contents.sendInputEvent({ type: 'keyUp', keyCode: 'Z' });
      await waitFor(contents, `window.terminalTest.getEmitted().some(item =>
        item.event === 'ssh_input' && item.args[0]?.data === 'z')`);
    }
    win.minimize();
    await waitFor(contents, 'document.visibilityState === "hidden"');
    win.restore(); win.focus();
    await restored();
    assert.equal(await evaluate('window.terminalTest.activeTerminalHasFocus()'), true);
    win.hide();
    await waitFor(contents, 'document.visibilityState === "hidden"');
    win.show(); win.focus();
    await restored();
    assert.equal(await evaluate('window.terminalTest.activeTerminalHasFocus()'), true);
    await evaluate(`document.getElementById('quick-settings').click();
      document.querySelector('.settings-nav-item[data-tab="appearance"]').click();
      document.getElementById('pref-fontFace').focus()`);
    await switchBack();
    assert.equal(await evaluate('document.activeElement.id'), 'pref-fontFace');
    assert.equal(await evaluate('window.terminalTest.activeTerminalHasFocus()'), false);
    await evaluate('window.terminalTest.clearEmitted()');
    const fontFace = await evaluate('document.getElementById("pref-fontFace").value');
    contents.sendInputEvent({ type: 'keyDown', keyCode: 'Z' });
    contents.sendInputEvent({ type: 'char', keyCode: 'z' });
    contents.sendInputEvent({ type: 'keyUp', keyCode: 'Z' });
    await waitFor(contents, `document.getElementById('pref-fontFace').value !== ${JSON.stringify(fontFace)}`);
    assert.equal(await evaluate('window.terminalTest.getEmitted().some(item => item.event === "ssh_input")'), false);
    await evaluate(`document.getElementById('pref-fontFace').value = ${JSON.stringify(fontFace)};
      document.getElementById('settings-close').click()`);
  } finally { other.destroy(); }
  console.log('Desktop focus smoke: repeated activation, terminal keyboard input, minimize/hide return and preserved Settings input passed.');
  // Reload must reattach the existing backend terminal, not create a new shell.
  await contents.loadURL(`${origin}/?debug=1`);
  await waitFor(contents, '!!window.terminalTest && window.terminalTest.getActiveAgentState()?.connected === true');
  assert.equal(await contents.executeJavaScript(
    'window.terminalTest.getTerminalTabsState().tabs.length',
  ), 1);
  await require('./test/external-links-smoke.cjs').run(contents, true);
  const popup = await contents.executeJavaScript("window.open('file:///blocked') === null");
  assert.equal(popup, true);
  // A second loopback service is outside the allowed origin too.
  const denied = await contents.executeJavaScript(`fetch('http://127.0.0.1:1/')
    .then(() => false, () => true)`);
  assert.equal(denied, true);
  await require('./test/floating-smoke.cjs').run(win, origin, contents);
  await require('./test/toolbar-smoke.cjs').run(win, contents, browserAccess);
}

module.exports = { run };
