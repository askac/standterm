'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function run(win, origin, contents = win.webContents) {
  const evaluate = async script => {
    let timer;
    try {
      return await Promise.race([contents.executeJavaScript(script, true), new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`Floating script timed out: ${script.slice(0, 180)}`)), 10000);
      })]);
    } finally { clearTimeout(timer); }
  };
  async function until(script) {
    const end = Date.now() + 15000;
    while (Date.now() < end) {
      if (await evaluate(script)) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    const state = await evaluate(`({ pip: !!window.terminalTest.getFloatingWindowForTest(),
      title: window.terminalTest.getFloatingWindowForTest()?.document.title,
      status: window.terminalTest.getFloatingWindowForTest()?.document.querySelector('.sftp-transfer-status')?.textContent,
      alerts: window.__floatingAlerts,
      menu: window.terminalTest?.showContextMenuForTest(window.terminalTest.getTerminalTabsState().activeTerminalId)
    })`);
    throw new Error(`Floating window smoke timed out: ${script}; ${JSON.stringify(state)}`);
  }
  const created = [];
  const record = child => created.push(child);
  contents.on('did-create-window', record);
  try {
    await evaluate('window.__floatingAlerts = []; window.alert = message => window.__floatingAlerts.push(message); void 0');
    const terminalId = await evaluate('window.terminalTest.getTerminalTabsState().activeTerminalId');
    const filesAvailable = await evaluate("!document.getElementById('sftp-status-btn').disabled");
    assert.equal(filesAvailable, process.platform !== 'win32' || !!process.env.STANDTERM_DESKTOP_WSL_DISTRO,
      'Local Files capability must match anchored POSIX backend support');
    await evaluate("document.getElementById('new-tab-btn').click()");
    await evaluate(`window.terminalTest.switchTerminalForTest(${JSON.stringify(terminalId)});
      window.terminalTest.showContextMenuForTest(${JSON.stringify(terminalId)})`);
    await evaluate(`window.__floatingOpenCount = 0; window.__countedOpen = window.open;
      window.open = (...args) => { window.__floatingOpenCount++; return window.__countedOpen(...args); }; void 0`);
    if (filesAvailable) {
      console.log('Floating smoke: checking Files download.');
      await evaluate("document.getElementById('sftp-send-option').click(); document.getElementById('sftp-send-option').click(); document.getElementById('pip-option').click()");
      await until("!!window.terminalTest.getFloatingWindowForTest()?.document.querySelector('.sftp-path-input')?.value && !window.terminalTest.getFloatingWindowForTest().document.querySelector('.sftp-directory-list').classList.contains('busy')");
      assert.equal(await evaluate('window.terminalTest.getFloatingWindowForTest().document.title'), 'StandTerm - Files');
    } else {
      await evaluate("document.getElementById('pip-option').click(); document.getElementById('pip-option').click()");
      await until("!!window.terminalTest.getFloatingWindowForTest()?.document.querySelector('.pip-terminal-host .terminal-pane')");
    }
    assert.equal(await evaluate('window.__floatingOpenCount'), 1, 'same-turn repeated/mixed clicks must initialize only once');
    await evaluate('window.open = window.__countedOpen; void 0');
    assert.equal(created.length, 1, 'real PiP must be covered by did-create-window guards');
    const child = created.at(-1);
    assert.equal(child.webContents.session, contents.session);
    const prefs = child.webContents.getLastWebPreferences();
    assert.equal(prefs.sandbox, true);
    assert.equal(prefs.contextIsolation, true);
    assert.equal(prefs.nodeIntegration, false);
    assert.equal(prefs.preload, undefined);
    await require('./external-links-smoke.cjs').run(child.webContents);
    assert.deepEqual(await child.webContents.executeJavaScript('({ node: typeof require, cookie: document.cookie })'),
      { node: 'undefined', cookie: '' });
    if (filesAvailable) {
      const root = process.env.STANDTERM_DESKTOP_TEST_ROOT || path.resolve(__dirname, '../..');
      const dist = path.join(root, 'desktop', 'dist');
      fs.mkdirSync(dist, { recursive: true });
      const fixture = fs.mkdtempSync(path.join(dist, 'files-smoke-'));
      const source = Buffer.from(Array.from({ length: 1024 }, (_, index) => index % 256));
      fs.writeFileSync(path.join(fixture, 'fixture.bin'), source, { flag: 'wx' });
      const browsePath = process.env.STANDTERM_DESKTOP_WSL_REPO
        ? `${process.env.STANDTERM_DESKTOP_WSL_REPO}/desktop/dist/${path.basename(fixture)}` : fixture;
      await child.webContents.executeJavaScript(`document.querySelector('.sftp-path-input').value = ${JSON.stringify(browsePath)};
        document.querySelector('.sftp-go').click()`, true);
      await until("!!window.terminalTest.getFloatingWindowForTest()?.document.querySelector('[data-entry-name=\"fixture.bin\"]')");
      await child.webContents.executeJavaScript("document.querySelector('[data-entry-name=\"fixture.bin\"]').click()", true);
      await until("!window.terminalTest.getFloatingWindowForTest().document.querySelector('.sftp-file-download').disabled");
      const output = path.join(fixture, 'downloaded.bin');
      const downloadSession = child.webContents.session;
      let item, timer, onDownload;
      const completed = new Promise(resolve => {
        onDownload = (_event, download) => {
          item = download;
          download.setSavePath(output);
          download.once('done', (_doneEvent, state) => resolve(state));
        };
        downloadSession.once('will-download', onDownload);
        timer = setTimeout(() => {
          downloadSession.removeListener('will-download', onDownload);
          resolve('timeout');
        }, 15000).unref();
      });
      let result;
      try {
        await child.webContents.executeJavaScript("document.querySelector('.sftp-file-download').click()", true);
        result = await completed;
      } finally {
        clearTimeout(timer);
        downloadSession.removeListener('will-download', onDownload);
      }
      console.log(`Floating smoke: download ${result}.`);
      if (result !== 'completed') item?.cancel();
      assert.equal(result, 'completed', 'Files ticket download must complete using the private session');
      assert.deepEqual(fs.readFileSync(output), source);
      assert.equal(created.length, 1, 'Download must not create another window');
    }
    assert.equal(await child.webContents.executeJavaScript("window.open('about:blank') === null"), true);
    // External URLs are covered by the consent test above; do not open a real
    // browser-confirmation dialog outside its scoped dialog/OS-open mocks.
    assert.equal(await evaluate("window.open('about:blank') === null"), true);
    for (const destination of ['https://example.com/', 'data:text/html,blocked', `${origin}/?forbidden=1`]) {
      const attempts = [];
      const recordNavigation = event => attempts.push(event.defaultPrevented);
      child.webContents.on('will-navigate', recordNavigation);
      child.webContents.on('will-frame-navigate', recordNavigation);
      await child.webContents.executeJavaScript(`location.href = ${JSON.stringify(destination)}; void 0`);
      await new Promise(resolve => setTimeout(resolve, 300));
      child.webContents.removeListener('will-navigate', recordNavigation);
      child.webContents.removeListener('will-frame-navigate', recordNavigation);
      // Chromium may reject data navigation before Electron emits will-navigate.
      if (!destination.startsWith('data:')) assert.ok(attempts.length, destination);
      assert.ok(attempts.every(prevented => prevented), destination);
      assert.equal(await child.webContents.executeJavaScript('location.href'), 'about:blank', destination);
    }
    await evaluate('window.terminalTest.getFloatingWindowForTest().close()');
    await until('!window.terminalTest.getFloatingWindowForTest()');
    await until('window.terminalTest.getTerminalTabsState().tabs.length === 2');
    await evaluate(`window.terminalTest.switchTerminalForTest(${JSON.stringify(terminalId)});
      window.terminalTest.showContextMenuForTest(${JSON.stringify(terminalId)}); document.getElementById('pip-option').click()`);
    await until("!!window.terminalTest.getFloatingWindowForTest()?.document.querySelector('.pip-terminal-host .terminal-pane')");
    assert.equal(await evaluate('window.terminalTest.getTerminalTabsState().tabs.filter(tab => tab.inPip).length'), 1);
    // Exercise the immediate close-old/open-new transition used by the PiP Files button.
    if (filesAvailable) {
      await evaluate("window.terminalTest.getFloatingWindowForTest().document.querySelector('.pip-sftp-button').click()");
      await until("window.terminalTest.getFloatingWindowForTest()?.document.title === 'StandTerm - Files'");
      assert.equal(await evaluate('window.terminalTest.getTerminalTabsState().tabs.some(tab => tab.inPip)'), false);
    }
    assert.equal(await evaluate('window.__floatingAlerts.length'), 0);
    const beforeReload = created.at(-1);
    console.log('Floating smoke: reloading Core with an open child.');
    await contents.loadURL(`${origin}/?debug=1`);
    await until('!!window.terminalTest && window.terminalTest.getSocketState().connected');
    console.log('Floating smoke: Core reloaded.');
    assert.equal(beforeReload.isDestroyed(), true, 'opener reload must not orphan its child');
    await evaluate("if (window.terminalTest.getTerminalTabsState().tabs.length < 2) document.getElementById('new-tab-btn').click()");
    console.log('Floating smoke: checking blocked-window fallback.');
    await evaluate(`window.terminalTest.switchTerminalForTest(${JSON.stringify(terminalId)});
      window.__floatingAlerts = []; window.__originalAlert = window.alert;
      window.alert = message => window.__floatingAlerts.push(message);
      window.__originalOpen = window.open;
      window.open = () => null;
      window.terminalTest.showContextMenuForTest(${JSON.stringify(terminalId)}); void 0`);
    if (filesAvailable) {
      await evaluate("document.getElementById('sftp-send-option').click()");
      await until('window.__floatingAlerts.length === 1');
    }
    console.log('Floating smoke: checking blocked PiP fallback.');
    await evaluate("document.getElementById('pip-option').click()");
    await until(`window.__floatingAlerts.length === ${filesAvailable ? 2 : 1}`);
    assert.equal(await evaluate('window.terminalTest.getTerminalTabsState().tabs.some(tab => tab.inPip)'), false);
    await evaluate('window.open = window.__originalOpen; window.alert = window.__originalAlert; void 0');
    console.log(`Floating smoke: PiP, child guards, reload cleanup and failure alerts passed; Files ${filesAvailable ? 'browse/download/transition passed' : 'correctly unavailable on native Windows Local Shell'}.`);
  } catch (error) {
    console.error(error.stack);
    throw error;
  } finally {
    contents.removeListener('did-create-window', record);
    for (const child of created) if (!child.isDestroyed()) child.destroy();
  }
}

module.exports = { run };
