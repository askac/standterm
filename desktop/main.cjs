'use strict';

const { app, BrowserWindow, Menu, Tray, nativeImage, dialog, session, shell, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { backendCommand, parseHandoff, allowedRequest, allowedNavigation } = require('./policy.cjs');
const { DesktopCapture } = require('./capture.cjs');
const { preparePackagedBackend, focusSetup, cancelSetup, confirmSetupQuit } = require('./setup.cjs');
const { isSquirrelEvent, handleSquirrelEvent } = require('./squirrel-events.cjs');
const { MODES, APP_ID, desktopMode } = require('./desktop-mode.cjs');
const { startWithPort, parsePortConflict, checkHostPort } = require('./port.cjs');
const { installerRequest, runInstaller } = require('./installer.cjs');
const { installFloatingWindows } = require('./floating-windows.cjs');
const { createDiagnostics, diagnosticsMenu, agentConnectionInfo, openDeveloperTools } = require('./diagnostics.cjs');
const { browserSessionOptions, resetBrowserAuthentication } = require('./browser-session.cjs');
const { createStatusWindow } = require('./diagnostics-window.cjs');
const { createExternalOpener } = require('./external-links.cjs');

let maintenance;
try { maintenance = installerRequest(process.argv); } catch (error) {
  app.whenReady().then(() => { dialog.showErrorBox('StandTerm installer', error.message); app.exit(1); });
  return;
}
if (maintenance) {
  if (process.platform !== 'win32' || !app.isPackaged) { app.exit(1); return; }
  app.enableSandbox();
  app.setName('StandTerm Desktop Setup');
  const profile = path.join(app.getPath('appData'), 'StandTermDesktopEvaluation', 'maintenance');
  fs.mkdirSync(profile, { recursive: true });
  app.setPath('userData', profile);
  if (!app.requestSingleInstanceLock()) { app.exit(3); return; }
  app.whenReady().then(() => runInstaller(maintenance)).then(code => app.exit(code)).catch(() => app.exit(1));
  return;
}

// Installation events must never start Core or open the first-run wizard.
if (process.platform === 'win32' && app.isPackaged && isSquirrelEvent(process.argv)) {
  handleSquirrelEvent(app, shell, process.argv).then(() => app.quit()).catch(error => {
    dialog.showErrorBox('StandTerm shortcut setup failed', error.message);
    app.exit(1);
  });
  return;
}

let mode;
try { mode = desktopMode(process.argv); } catch (error) {
  app.whenReady().then(() => { dialog.showErrorBox('StandTerm Desktop', error.message); app.exit(1); });
  return;
}
app.setName('StandTerm Desktop');
if (process.platform === 'win32') app.setAppUserModelId(`${APP_ID}.${mode}`);
app.enableSandbox();
const captureSmoke = process.argv.includes('--desktop-capture-smoke');
const smoke = process.argv.includes('--desktop-smoke') || captureSmoke;
// Tests must never focus an existing operator window through the instance lock.
if (smoke) app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-desktop-test-')));
else if (app.isPackaged) {
  const profile = path.join(app.getPath('appData'), 'StandTermDesktopEvaluation', mode);
  fs.mkdirSync(profile, { recursive: true });
  app.setPath('userData', profile);
}
let child;
let desktopSession;
const expectedBackendExits = new WeakSet();
let win;
let tray;
let capture;
let captureTitle = '';
let pageTitle = 'StandTerm';
let closePending = false;
let quitting = false;
let stopped = false;
let exitCode = 0;
let booting = true;
const diagnostics = createDiagnostics(path.join(app.getPath('userData'), 'diagnostics'), {
  mode, version: app.getVersion(),
});

function showWindow() {
  if (!win || win.isDestroyed()) { focusSetup(); return; }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function launchBackend(preparedCommand, port = 0) {
  diagnostics.write('backend_launch', { port });
  const root = smoke && process.env.STANDTERM_DESKTOP_TEST_ROOT
    ? path.resolve(process.env.STANDTERM_DESKTOP_TEST_ROOT) : path.resolve(__dirname, '..');
  const command = preparedCommand || backendCommand(root, process.platform, process.env);
  const backend = child = spawn(command.executable, [...command.args, '--port', String(port)], {
    cwd: command.cwd, windowsHide: true, shell: false,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  // Do not forward backend output: diagnostics and terminal metadata can be sensitive.
  child.stderr.resume();
  child.stdin.on('error', () => {});
  return new Promise((resolve, reject) => {
    let buffer = '';
    let received = false;
    const timer = setTimeout(() => reject(new Error(
      'Backend startup timed out. Check the selected Python environment and WSL availability.',
    )), 60000);
    child.once('error', error => {
      diagnostics.write('backend_spawn_failed', { code: error.code });
      clearTimeout(timer);
      reject(new Error('Cannot launch the backend. Prepare the platform venv described in desktop/README.md.'));
    });
    child.once('exit', code => {
      diagnostics.write('backend_exit', { exitCode: code, expected: expectedBackendExits.has(backend) || quitting });
      clearTimeout(timer);
      if (!received) reject(new Error('Backend exited before startup. Check its Python dependencies.'));
      else if (!quitting && !expectedBackendExits.has(backend)) {
        exitCode = 1;
        if (!smoke) dialog.showErrorBox('StandTerm backend stopped', 'Restart StandTerm Desktop to start a new session.');
        app.quit();
      }
    });
    child.stdout.on('data', chunk => {
      if (received) return;
      buffer += chunk.toString('utf8');
      if (Buffer.byteLength(buffer) > 4096) {
        clearTimeout(timer);
        reject(new Error('Invalid backend control response.'));
        return;
      }
      const end = buffer.indexOf('\n');
      if (end < 0) return;
      clearTimeout(timer);
      try {
        const conflict = parsePortConflict(buffer.slice(0, end), port);
        if (conflict) { buffer = ''; reject(conflict); return; }
        const handoff = parseHandoff(buffer.slice(0, end));
        if (port && Number(new URL(handoff.origin).port) !== port) throw new Error('Backend bound an unexpected port.');
        received = true;
        buffer = '';
        diagnostics.write('backend_ready', { port: Number(new URL(handoff.origin).port) });
        resolve(handoff);
      } catch {
        buffer = '';
        reject(new Error('Invalid backend control response.'));
      }
    });
  });
}

function requestBackendStatus(handoff) {
  return new Promise((resolve, reject) => {
    const req = http.get(`${handoff.origin}/launcher/status`, {
      headers: { 'X-StandTerm-Launcher-Token': handoff.launcher_token },
      timeout: 3000,
    }, res => {
      let body = '';
      res.on('data', chunk => {
        body += chunk;
        if (body.length > 8192) req.destroy(new Error('Invalid backend status.'));
      });
      res.on('end', () => {
        try {
          const data = JSON.parse(body);
          if (res.statusCode !== 200 || data.status !== 'ok'
              || data.instance_id !== handoff.instance_id) throw new Error();
          resolve();
        } catch { reject(new Error('The HTTP backend does not match the launched instance.')); }
      });
    });
    req.on('timeout', () => req.destroy(Object.assign(new Error('Backend connection timed out.'), { code: 'ETIMEDOUT' })));
    req.on('error', error => reject(Object.assign(
      new Error('Cannot reach the owned backend over loopback.'), { code: error.code },
    )));
  });
}

async function verifyBackend(handoff) {
  // Windows may establish WSL localhost forwarding after Python starts listening.
  const deadline = Date.now() + 15000;
  const started = Date.now();
  let attempt = 0;
  while (true) {
    try {
      attempt++;
      await requestBackendStatus(handoff);
      diagnostics.write('backend_verified', { port: Number(new URL(handoff.origin).port), attempt, elapsedMs: Date.now() - started });
      return;
    } catch (error) {
      diagnostics.write('backend_verify_retry', { port: Number(new URL(handoff.origin).port), code: error.code, attempt, elapsedMs: Date.now() - started });
      if (!['ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT'].includes(error.code)
          || Date.now() >= deadline || child.exitCode !== null) throw error;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
}

function createTray() {
  // A small code-native terminal glyph, also usable before production artwork exists.
  const size = 32;
  const pixels = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const glyph = (x >= 7 && x <= 14 && Math.abs(y - 15) === x - 7)
      || (y >= 22 && y <= 24 && x >= 18 && x <= 25);
    const index = (y * size + x) * 4;
    pixels[index] = glyph ? 255 : 35;
    pixels[index + 1] = glyph ? 255 : 35;
    pixels[index + 2] = glyph ? 255 : 35;
    pixels[index + 3] = 255;
  }
  const icon = nativeImage.createFromBitmap(pixels, { width: size, height: size });
  tray = new Tray(icon);
  tray.setToolTip('StandTerm Desktop');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open StandTerm', click: showWindow },
    { type: 'separator' },
    { label: 'Quit StandTerm', click: () => app.quit() },
  ]));
  tray.on('click', showWindow);
  return icon;
}

async function start() {
  diagnostics.write('setup_start');
  const prepared = app.isPackaged && !smoke ? await preparePackagedBackend(mode) : null;
  diagnostics.write('setup_ready');
  const settingsPath = path.join(app.getPath('userData'), `port-${mode}.json`);
  const testPort = smoke ? process.env.STANDTERM_DESKTOP_TEST_PORT : undefined;
  let testPortChanges = 0;
  if (testPort !== undefined) {
    const port = Number(testPort);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid smoke port.');
    fs.writeFileSync(settingsPath, JSON.stringify({ version: 1, port }), { flag: 'wx' });
  }
  const handoff = await startWithPort({
    settingsPath,
    launch: port => launchBackend(prepared, port), verify: verifyBackend, stop: stopBackend,
    checkHost: process.platform === 'win32' && mode === 'wsl' ? async (port, options) => {
      try { await checkHostPort(port, options); }
      catch (error) { diagnostics.write('host_port_rejected', { port, code: error.code }); throw error; }
    } : undefined,
    confirm: async (port, candidate, reason) => {
      diagnostics.write('port_change', { port, candidate });
      if (smoke) { testPortChanges++; console.log(`Port smoke: approved replacement ${port} -> ${candidate} (${reason || 'backend_address_in_use'}).`); return 'remember'; }
      const problem = reason === 'host_permission_denied' ? 'is reserved or denied by Windows'
        : reason === 'host_address_in_use' ? 'is already in use on Windows' : 'is already in use';
      const answer = await dialog.showMessageBox({ type: 'question', title: MODES[mode],
        message: `Port ${port} ${problem}. Use port ${candidate}?`,
        detail: 'No existing service will be stopped or reused. Changing the port changes the browser origin; '
          + 'browser settings and SSH keys are not migrated. Windows and WSL remember their ports separately.',
        buttons: ['Cancel', 'Use once', 'Use and remember'], defaultId: 0, cancelId: 0, noLink: true });
      return ['cancel', 'once', 'remember'][answer.response];
    },
    notify: message => smoke ? console.warn(message) : dialog.showMessageBox({ type: 'warning', title: MODES[mode], message }),
  });
  if (smoke) {
    const saved = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
    if (saved.port !== Number(new URL(handoff.origin).port)
        || (testPort !== undefined && (testPortChanges !== 1 || saved.port === Number(testPort)))) {
      throw new Error('Port smoke did not verify and remember the approved replacement.');
    }
  }
  const browserOptions = browserSessionOptions(mode, smoke);
  desktopSession = session.fromPartition(browserOptions.partition, browserOptions.options);
  await desktopSession.setProxy({ mode: 'direct' });
  desktopSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  desktopSession.setPermissionCheckHandler(() => false);
  desktopSession.setDevicePermissionHandler(() => false);
  desktopSession.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: !allowedRequest(details.url, handoff.origin) });
  });
  await resetBrowserAuthentication(desktopSession);
  await desktopSession.cookies.set({
    url: handoff.origin, name: handoff.cookie_name, value: handoff.session_token,
    httpOnly: true, sameSite: 'strict', path: '/',
  });
  handoff.session_token = '';
  handoff.launcher_token = '';
  let icon;
  if (!smoke) {
    try { icon = createTray(); } catch { tray = null; }
  }
  win = new BrowserWindow({
    title: MODES[mode], width: 1280, height: 850,
    minWidth: 640, minHeight: 480, show: false, icon,
    webPreferences: {
      session: desktopSession, nodeIntegration: false, contextIsolation: true,
      sandbox: true, webSecurity: true, webviewTag: false,
      allowRunningInsecureContent: false, devTools: true,
    },
  });
  const updateTitle = () => {
    if (!win.isDestroyed()) win.setTitle(`${captureTitle ? `[${captureTitle}] ` : ''}${MODES[mode]} - ${pageTitle}`);
  };
  capture = new DesktopCapture(win, {
    onChange: value => { captureTitle = value; updateTitle(); },
    ...(smoke ? { notify: async (message, error) => {
      if (error) console.error(message);
    } } : {}),
  });
  win.on('page-title-updated', (event, title) => {
    event.preventDefault();
    pageTitle = title;
    updateTitle();
  });
  const connectionInfo = agentConnectionInfo({ origin: handoff.origin, instanceId: handoff.instance_id, mode });
  const coreVersion = handoff.core_version || 'Unknown (older Core)';
  const coreBuild = handoff.core_bundle_id || 'Source checkout / no managed bundle identity';
  const pythonVersion = handoff.python_version || 'Unknown (older Core)';
  const aboutDetails = `Core version: ${coreVersion}\nCore bundle SHA-256: ${coreBuild}\n`
    + `Backend: ${MODES[mode]}\nPython: ${pythonVersion}\n`
    + `Electron: ${process.versions.electron}\nChromium: ${process.versions.chrome}\nNode.js: ${process.versions.node}\n`
    + `Platform: ${process.platform} / ${process.arch}\n\nEvaluation build; updates are installed manually.`;
  const openStatus = createStatusWindow(win, () => ({ rows: [
    ['Desktop version', app.getVersion()], ['Backend mode', MODES[mode]],
    ['Core version', coreVersion], ['Core bundle SHA-256', coreBuild], ['Python version', pythonVersion],
    ['Backend URL', handoff.origin], ['Instance ID', handoff.instance_id],
    ['Platform', `${process.platform} / ${process.arch}`],
    ['Electron / Chromium / Node', `${process.versions.electron} / ${process.versions.chrome} / ${process.versions.node}`],
    ['Backend process', child && child.exitCode === null && child.signalCode === null ? 'Running' : 'Stopped'],
    ['Renderer process', win.webContents.isCrashed() ? 'Crashed' : 'Running'],
    ['Browser storage', smoke ? 'Temporary test profile' : 'Persistent per origin; same as Core'],
    ['Profile directory', app.getPath('userData')], ['Port settings', settingsPath],
    ['Diagnostic log', diagnostics.file], ['Log writable', diagnostics.available ? 'Yes' : 'No'],
    ['Security', 'Sandbox on; Node/preload off; external preview network blocked'],
  ], events: diagnostics.snapshot() }), {
    copyUrl: () => clipboard.writeText(connectionInfo.base_url),
    copyConnectionInfo: () => clipboard.writeText(JSON.stringify(connectionInfo, null, 2)),
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { label: 'StandTerm', submenu: [
      { id: 'desktop-about', label: 'About StandTerm Desktop', click: () => dialog.showMessageBox(win, {
        type: 'info', title: 'About StandTerm Desktop', message: `StandTerm Desktop ${app.getVersion()}`,
        detail: aboutDetails, buttons: ['OK'], noLink: true,
      }) },
      { label: 'Show window', click: showWindow },
      { label: 'Quit StandTerm', accelerator: 'CommandOrControl+Q', click: () => app.quit() },
    ] },
    { role: 'editMenu' },
    capture.menu(),
    { label: 'View', submenu: [{ role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
    diagnosticsMenu({ origin: handoff.origin, mode, instanceId: handoff.instance_id,
      version: app.getVersion(), coreVersion: handoff.core_version, logger: diagnostics, persistent: !smoke,
      copyText: text => clipboard.writeText(text),
      openStatus: () => openStatus().catch(() => {
        if (!win.isDestroyed()) dialog.showMessageBox(win, { type: 'warning', message: 'Could not open the diagnostics page.' });
      }),
      openLogs: async () => {
        const error = await shell.openPath(diagnostics.directory);
        if (error) await dialog.showMessageBox(win, { type: 'warning', message: 'Could not open the diagnostics folder.', detail: diagnostics.directory });
      },
      openTools: async () => {
        const opened = await openDeveloperTools(win.webContents, async () => {
          const answer = await dialog.showMessageBox(win, { type: 'warning', title: 'Developer Tools',
            message: 'Open Developer Tools for this authenticated terminal UI?',
            detail: 'Console code can read page data and operate connected terminals. Only run code you trust. '
              + 'This opens a local frontend debugger, not a remote debugging port or a Node.js bridge.',
            buttons: ['Cancel', 'Open Developer Tools'], defaultId: 0, cancelId: 0, noLink: true });
          return answer.response === 1;
        });
        if (opened) diagnostics.write('devtools_opened');
      },
    }),
  ]));
  const openExternal = createExternalOpener({ origin: handoff.origin, owner: win.webContents,
    confirm: async url => {
      const answer = await dialog.showMessageBox(win, { type: 'question', title: 'Open in default browser',
        message: `Open ${new URL(url).host} in your default browser?`,
        detail: `${url}\n\nThe browser uses its own login. StandTerm does not add its token or cookies.`,
        buttons: ['Cancel', 'Open in browser'], defaultId: 0, cancelId: 0, noLink: true });
      return answer.response === 1;
    },
    open: url => shell.openExternal(url),
    notify: () => dialog.showMessageBox(win, { type: 'warning', message: 'Could not open the default browser.',
      detail: 'Check the default HTTP/HTTPS browser in your operating system settings.' }),
  });
  installFloatingWindows(win, handoff.origin, openExternal);
  win.webContents.on('will-navigate', (event, url) => {
    if (!allowedNavigation(url, handoff.origin)) event.preventDefault();
  });
  win.webContents.on('will-redirect', (event, url) => {
    if (!allowedNavigation(url, handoff.origin)) event.preventDefault();
  });
  win.webContents.on('will-attach-webview', event => event.preventDefault());
  win.on('close', event => {
    if (capture.active) {
      event.preventDefault();
      if (!quitting && !closePending) {
        closePending = true;
        capture.confirmStop('closing the window').then(allowed => {
          closePending = false;
          if (allowed) win.close();
        }).catch(() => { closePending = false; });
      }
      return;
    }
    if (!quitting && tray && !smoke) {
      event.preventDefault();
      win.hide();
    }
  });
  // A hidden/minimized page may stop producing frames. Finalize rather than
  // presenting a frozen recording as ongoing capture.
  win.on('minimize', () => { if (capture.active) void capture.stop(); });
  win.on('hide', () => { if (capture.active) void capture.stop(); });
  win.on('enter-full-screen', () => { if (capture.active) void capture.stop(); });
  win.webContents.on('did-start-navigation', (_event, _url, _inPlace, isMainFrame) => {
    if (isMainFrame && capture.active) void capture.stop();
  });
  win.webContents.on('render-process-gone', () => {
    exitCode = 1;
    app.quit();
  });
  await win.loadURL(`${handoff.origin}/${smoke ? '?debug=1' : ''}`);
  diagnostics.write('window_ready', { port: Number(new URL(handoff.origin).port) });
  booting = false;
  if (smoke) {
    await require('./smoke.cjs').run(win, handoff.origin);
    if (captureSmoke) {
      showWindow();
      await require('./test/capture-smoke.cjs').run(win, capture);
    }
    console.log('Desktop smoke: authenticated terminal, sandbox and navigation checks passed.');
    app.quit();
  } else {
    showWindow();
  }
}

async function stopBackend() {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  expectedBackendExits.add(child);
  await new Promise(resolve => {
    const timer = setTimeout(() => { child.kill(); resolve(); }, 5000);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
    child.stdin.end();
  });
}

async function finishBackendAndBrowser() {
  try { await stopBackend(); }
  finally {
    if (desktopSession) {
      try { await resetBrowserAuthentication(desktopSession); }
      finally { desktopSession.flushStorageData(); }
    }
  }
}

app.on('before-quit', event => {
  if (stopped) return;
  event.preventDefault();
  if (quitting) return;
  quitting = true;
  diagnostics.write('shutdown');
  (async () => {
    if (!await confirmSetupQuit()) { quitting = false; return; }
    if (capture?.active) {
      const allowed = smoke ? (await capture.stop(), true) : await capture.confirmStop('quitting StandTerm');
      if (!allowed) { quitting = false; return; }
    }
    cancelSetup();
    await finishBackendAndBrowser();
    stopped = true;
    if (tray) tray.destroy();
    app.exit(exitCode);
  })().catch(async () => {
    try { await finishBackendAndBrowser(); } catch { /* Next startup resets stale authentication before navigation. */ }
    stopped = true;
    app.exit(1);
  });
});
app.on('window-all-closed', () => { if (!tray && !booting) app.quit(); });
app.on('activate', showWindow);
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  diagnostics.write('startup');
  app.on('second-instance', showWindow);
  app.whenReady().then(start).catch(error => {
    diagnostics.write('startup_failed', { code: error.code });
    if (error.code === 'SETUP_CANCELED') { app.quit(); return; }
    exitCode = 1;
    if (smoke) console.error(error.message);
    else dialog.showErrorBox('StandTerm Desktop could not start', `${error.message}\n\nDiagnostics: ${diagnostics.file}`);
    app.quit();
  });
}
process.on('SIGINT', () => app.quit());
process.on('SIGTERM', () => app.quit());
