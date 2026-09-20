'use strict';

const { app, BrowserWindow, WebContentsView, Menu, Tray, nativeImage, dialog, session, shell, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { backendCommand, parseHandoff, allowedRequest, allowedNavigation } = require('./policy.cjs');
const { DesktopCapture } = require('./capture.cjs');
const { preparePackagedBackend, manageCore, focusSetup, cancelSetup, confirmSetupQuit } = require('./setup.cjs');
const { installedStore, coreController } = require('./core-source.cjs');
const { stopOwnedBackend } = require('./backend-stop.cjs');
const { isSquirrelEvent, handleSquirrelEvent } = require('./squirrel-events.cjs');
const { MODES, APP_ID, desktopMode } = require('./desktop-mode.cjs');
const { startWithPort, parsePortConflict, checkHostPort } = require('./port.cjs');
const { installerRequest, runInstaller } = require('./installer.cjs');
const { installFloatingWindows } = require('./floating-windows.cjs');
const { createDiagnostics, diagnosticsMenu, agentConnectionInfo, openDeveloperTools } = require('./diagnostics.cjs');
const { agentMenu } = require('./agent-menu.cjs');
const { browserSessionOptions, resetBrowserAuthentication } = require('./browser-session.cjs');
const { createStatusWindow } = require('./diagnostics-window.cjs');
const { createExternalOpener } = require('./external-links.cjs');
const { createUiCommands } = require('./ui-commands.cjs');
const { installToolbar } = require('./toolbar.cjs');
const { createBrowserAccess } = require('./browser-access.cjs');
const { installContextPaste } = require('./context-paste.cjs');
const { createLanguage } = require('./language.cjs');

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
if (process.platform === 'win32') {
  app.setAppUserModelId(`${APP_ID}.${mode}`);
  // Native occlusion can leave a restored Core view unable to receive input or capture.
  // Keep explicit focus, hide and minimize guards; reassess after Electron upgrades.
  const disabledFeatures = app.commandLine.getSwitchValue('disable-features');
  app.commandLine.appendSwitch('disable-features', [disabledFeatures, 'CalculateNativeWinOcclusion'].filter(Boolean).join(','));
}
app.enableSandbox();
const captureSmoke = process.argv.includes('--desktop-capture-smoke');
const smoke = process.argv.includes('--desktop-smoke') || captureSmoke;
// Denied camera/microphone probes must not enumerate the operator's real devices.
// This supplies synthetic devices, not permission grants or a fake chooser.
if (smoke) app.commandLine.appendSwitch('use-fake-device-for-media-stream');
// Tests must never focus an existing operator window through the instance lock.
if (smoke) app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-desktop-test-')));
else if (app.isPackaged) {
  const profile = path.join(app.getPath('appData'), 'StandTermDesktopEvaluation', mode);
  fs.mkdirSync(profile, { recursive: true });
  app.setPath('userData', profile);
}
const language = createLanguage(path.join(app.getPath('userData'), 'language.json'));
const { t } = language;
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
let coreStore;
let coreManager;
let restartRequest;
let failurePending;
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
      'Backend startup timed out. Check the selected Python environment and backend availability.',
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
      else if (!booting && !quitting && !expectedBackendExits.has(backend)) {
        void handleCoreFailure(new Error('The owned Core backend stopped unexpectedly.'));
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
    { label: t('desktop.menu.open'), click: showWindow },
    { type: 'separator' },
    { label: t('desktop.menu.quit'), click: () => app.quit() },
  ]));
  tray.on('click', showWindow);
  return icon;
}

async function start() {
  diagnostics.write('setup_start');
  if (app.isPackaged && !smoke) {
    coreStore = await installedStore(app.getPath('userData'), process.resourcesPath, app.getVersion());
    coreManager = coreController({ store: coreStore, prepareBundled: () => preparePackagedBackend(mode),
      manage: action => manageCore(mode, action), dialog,
      openLogs: () => shell.openPath(path.dirname(diagnostics.file)),
      restart: action => { restartRequest = { action }; app.quit(); },
    });
  }
  const prepared = coreManager ? await coreManager.prepare() : null;
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
  // Retain launcher authority only in the main process for explicit native
  // Browser Access actions; it is never passed to either renderer or persisted.
  const launcherToken = handoff.launcher_token;
  handoff.launcher_token = '';
  let icon;
  if (!smoke) {
    try { icon = createTray(); } catch { tray = null; }
  }
  win = new BrowserWindow({
    title: MODES[mode], width: 1280, height: 850,
    minWidth: 640, minHeight: 480, show: false, icon,
    webPreferences: {
      partition: 'standterm-desktop-toolbar', preload: path.join(__dirname, 'toolbar-preload.cjs'),
      // The trusted status strip must keep timers/notices current when unfocused.
      // Capture focus/visibility guards and Core's own preferences stay unchanged.
      backgroundThrottling: false,
      nodeIntegration: false, contextIsolation: true,
      sandbox: true, webSecurity: true, webviewTag: false,
      allowRunningInsecureContent: false, devTools: true,
    },
  });
  const coreView = new WebContentsView({ webPreferences: {
    session: desktopSession, nodeIntegration: false, contextIsolation: true,
    sandbox: true, webSecurity: true, webviewTag: false, allowRunningInsecureContent: false, devTools: true,
  } });
  const contents = coreView.webContents;
  const commands = createUiCommands(win, contents, handoff.origin, t);
  let toolbar;
  const updateTitle = () => {
    if (!win.isDestroyed()) win.setTitle(`${captureTitle ? `[${captureTitle}] ` : ''}${MODES[mode]} - ${pageTitle}`);
  };
  capture = new DesktopCapture(win, {
    contents, t,
    onDiagnostic: state => diagnostics.write('capture_failed', state),
    onChange: (value, state) => { captureTitle = value; updateTitle(); toolbar?.send(state); },
    notify: async (message, error) => toolbar?.notify(message, error),
    ...(smoke ? { notify: async (message, error) => {
      if (error) console.error(message);
    } } : {}),
  });
  toolbar = installToolbar(win, coreView, capture, commands, language.locale);
  installContextPaste(win, contents, handoff.origin, toolbar.notify);
  const browserAccess = createBrowserAccess({ origin: handoff.origin, session: desktopSession, launcherToken,
    t,
    available: () => !win.isDestroyed() && !contents.isDestroyed() && allowedNavigation(contents.getURL(), handoff.origin),
    confirm: async () => {
      const result = await dialog.showMessageBox(win, { type: 'warning', title: t('desktop.browser_access.confirm_title'),
        message: t('desktop.browser_access.confirm_message'), detail: t('desktop.browser_access.confirm_detail'),
        buttons: [t('desktop.common.cancel'), t('desktop.browser_access.continue')], defaultId: 0, cancelId: 0, noLink: true });
      return result.response === 1;
    },
    copy: value => clipboard.writeText(value), open: value => shell.openExternal(value), notify: toolbar.notify,
  });
  win.once('closed', browserAccess.dispose);
  contents.on('page-title-updated', (event, title) => {
    event.preventDefault();
    pageTitle = title;
    updateTitle();
  });
  const connectionInfo = agentConnectionInfo({ origin: handoff.origin, instanceId: handoff.instance_id, mode });
  const coreVersion = handoff.core_version || t('desktop.about.unknown_version');
  const coreBuild = prepared?.source === 'git' ? prepared.coreSource
    : handoff.core_bundle_id || t('desktop.about.unmanaged_source');
  const pythonVersion = handoff.python_version || t('desktop.about.unknown_version');
  const buildLabel = t(prepared?.source === 'git' ? 'desktop.about.git_revision' : 'desktop.about.bundle_sha256');
  const aboutDetails = t('desktop.about.details', { core_version: coreVersion, build_label: buildLabel,
    core_build: coreBuild, backend: MODES[mode], python_version: pythonVersion,
    electron_version: process.versions.electron, chromium_version: process.versions.chrome,
    node_version: process.versions.node, platform: process.platform, arch: process.arch });
  const openStatus = createStatusWindow(win, () => ({ rows: [
    [t('desktop.diagnostics.desktop_version'), app.getVersion()], [t('desktop.diagnostics.backend_mode'), MODES[mode]],
    [t('desktop.diagnostics.core_version'), coreVersion], [buildLabel, coreBuild], [t('desktop.diagnostics.python_version'), pythonVersion],
    [t('desktop.diagnostics.backend_url'), handoff.origin], [t('desktop.diagnostics.instance_id'), handoff.instance_id],
    [t('desktop.diagnostics.platform'), `${process.platform} / ${process.arch}`],
    [t('desktop.diagnostics.engines'), `${process.versions.electron} / ${process.versions.chrome} / ${process.versions.node}`],
    [t('desktop.diagnostics.backend_process'), t(child && child.exitCode === null && child.signalCode === null ? 'desktop.diagnostics.running' : 'desktop.diagnostics.stopped')],
    [t('desktop.diagnostics.renderer_process'), t(contents.isCrashed() ? 'desktop.diagnostics.crashed' : 'desktop.diagnostics.running')],
    [t('desktop.diagnostics.browser_storage'), t(smoke ? 'desktop.diagnostics.storage_temporary' : 'desktop.diagnostics.storage_persistent')],
    [t('desktop.diagnostics.profile_directory'), app.getPath('userData')], [t('desktop.diagnostics.port_settings'), settingsPath],
    [t('desktop.diagnostics.log_file'), diagnostics.file], [t('desktop.diagnostics.log_writable'), t(diagnostics.available ? 'desktop.diagnostics.log_write_ok' : 'desktop.diagnostics.log_write_failed')],
    [t('desktop.diagnostics.security'), t('desktop.diagnostics.security_detail')],
  ], events: diagnostics.snapshot() }), {
    copyUrl: () => clipboard.writeText(connectionInfo.base_url), i18n: language,
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { id: 'standterm', label: 'StandTerm', submenu: [
      commands.item('settings'),
      { id: 'desktop-language', label: t('desktop.language.menu'), click: () => { void language.choose(dialog, win); } },
      ...(coreManager ? [{ label: t('desktop.menu.core_source'), click: () => {
        void coreManager.showManager().catch(error => dialog.showErrorBox('Core management unavailable', error.message));
      } }] : []),
      { label: t('desktop.menu.capture_settings'), click: () => capture.configure() },
      browserAccess.menu,
      { type: 'separator' },
      commands.item('newTab'), commands.item('closeTab'), commands.item('closeAll'),
      commands.item('files'), commands.item('pip'),
      { type: 'separator' },
      { id: 'desktop-about', label: t('desktop.menu.about'), click: () => dialog.showMessageBox(win, {
        type: 'info', title: t('desktop.menu.about'), message: t('desktop.about.desktop_version', { version: app.getVersion() }),
        detail: aboutDetails, buttons: [t('desktop.common.ok')], noLink: true,
      }) },
      { label: t('desktop.menu.show'), click: showWindow },
      ...(process.platform === 'darwin' ? [{ role: 'services' }, { type: 'separator' }, { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' }] : []),
      { label: t('desktop.menu.quit'), accelerator: 'CommandOrControl+Q', click: () => app.quit() },
    ] },
    { id: 'edit', role: 'editMenu', label: t('desktop.toolbar.menu_edit') },
    agentMenu({
      t,
      uiItems: [commands.item('agentPanel'), commands.item('pauseAgent'), { type: 'separator' }],
      showHelp: () => dialog.showMessageBox(win, {
        type: 'info', title: 'StandTerm Agent', message: t('desktop.agent.help_title'),
        detail: ['desktop.agent.help_permissions', 'desktop.agent.help_environment',
          'desktop.agent.help_connection', 'desktop.agent.help_skills'].map(key => t(key)).join('\n\n'),
        buttons: [t('desktop.common.ok')], noLink: true,
      }),
    }),
    { id: 'view', label: t('desktop.toolbar.menu_view'), submenu: [{ role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' },
      { type: 'separator' },
      ...capture.menu().submenu,
    ] },
    diagnosticsMenu({ origin: handoff.origin, mode, instanceId: handoff.instance_id,
      t,
      version: app.getVersion(), coreVersion: handoff.core_version, logger: diagnostics, persistent: !smoke,
      copyText: text => clipboard.writeText(text),
      openStatus: () => openStatus().catch(() => {
        if (!win.isDestroyed()) dialog.showMessageBox(win, { type: 'warning', message: t('desktop.diagnostics.open_failed') });
      }),
      openLogs: async () => {
        const error = await shell.openPath(diagnostics.directory);
        if (error) await dialog.showMessageBox(win, { type: 'warning', message: t('desktop.diagnostics.folder_failed'), detail: diagnostics.directory });
      },
      openTools: async () => {
        const opened = await openDeveloperTools(contents, async () => {
          const answer = await dialog.showMessageBox(win, { type: 'warning', title: t('desktop.diagnostics.devtools_title'),
            message: t('desktop.diagnostics.devtools_message'), detail: t('desktop.diagnostics.devtools_detail'),
            buttons: [t('desktop.common.cancel'), t('desktop.diagnostics.devtools_open')], defaultId: 0, cancelId: 0, noLink: true });
          return answer.response === 1;
        });
        if (opened) diagnostics.write('devtools_opened');
      },
    }),
  ]));
  const openExternal = createExternalOpener({ origin: handoff.origin, owner: contents,
    confirm: async url => {
      const answer = await dialog.showMessageBox(win, { type: 'question', title: t('desktop.external_browser.title'),
        message: t('desktop.external_browser.message', { host: new URL(url).host }),
        detail: t('desktop.external_browser.detail', { url }),
        buttons: [t('desktop.common.cancel'), t('desktop.external_browser.open')], defaultId: 0, cancelId: 0, noLink: true });
      return answer.response === 1;
    },
    open: url => shell.openExternal(url),
    notify: () => dialog.showMessageBox(win, { type: 'warning', message: t('desktop.external_browser.failed'),
      detail: t('desktop.external_browser.failure_hint') }),
  });
  installFloatingWindows(win, handoff.origin, openExternal, contents, (result, owner) => {
    const completed = result.state === 'completed' && !!result.path;
    void dialog.showMessageBox(owner, {
      type: completed ? 'info' : 'warning', title: 'Files download',
      message: completed ? 'Download complete' : 'Download did not complete',
      detail: completed ? `Saved to:\n${result.path}` : 'The connection was interrupted. Retry the download from Files.',
      buttons: completed ? ['Close', 'Show in folder'] : ['Close'],
      defaultId: 0, cancelId: 0, noLink: true,
    }).then(answer => {
      if (completed && answer.response === 1) shell.showItemInFolder(result.path);
    }).catch(() => diagnostics.write('download_notice_failed'));
  });
  contents.on('will-navigate', (event, url) => {
    if (!allowedNavigation(url, handoff.origin)) event.preventDefault();
  });
  contents.on('will-redirect', (event, url) => {
    if (!allowedNavigation(url, handoff.origin)) event.preventDefault();
  });
  contents.on('will-attach-webview', event => event.preventDefault());
  win.on('close', event => {
    if (capture.active || capture.confirming || closePending) {
      event.preventDefault();
      if (!quitting && !closePending) {
        closePending = true;
        capture.confirmStop('close').then(allowed => {
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
  contents.on('did-start-navigation', (_event, _url, _inPlace, isMainFrame) => {
    if (isMainFrame && capture.active) void capture.stop();
  });
  contents.on('render-process-gone', () => {
    void handleCoreFailure(new Error('The Core renderer stopped unexpectedly.'));
  });
  await win.loadURL(toolbar.url);
  if (process.platform !== 'darwin') win.setMenuBarVisibility(false);
  toolbar.layout();
  await contents.loadURL(`${handoff.origin}/${smoke ? '?debug=1' : ''}`);
  if (child.exitCode !== null || child.signalCode !== null) throw new Error('The owned Core exited during startup.');
  diagnostics.write('window_ready', { port: Number(new URL(handoff.origin).port) });
  booting = false;
  if (smoke) {
    // WebContentsView visibility follows its owner; exercise a real visible UI.
    showWindow();
    contents.focus();
    await require('./smoke.cjs').run(win, handoff.origin, contents, browserAccess);
    if (captureSmoke) {
      showWindow();
      await require('./test/capture-smoke.cjs').run(win, capture, contents);
    }
    console.log('Desktop smoke: authenticated terminal, sandbox and navigation checks passed.');
    app.quit();
  } else {
    showWindow();
  }
}

async function stopBackend() {
  if (child) expectedBackendExits.add(child);
  await stopOwnedBackend(child);
}

async function handleCoreFailure(error) {
  if (failurePending || quitting) return;
  failurePending = true;
  booting = true;
  diagnostics.write('core_failed', { code: error.code });
  try {
    await stopBackend();
    if (coreManager) {
      if (await coreManager.failure(error) === 'quit') app.quit();
    } else {
      exitCode = 1;
      if (smoke) console.error(error.stack || error.message);
      else dialog.showErrorBox('StandTerm Desktop could not start', `${error.message}\n\nDiagnostics: ${diagnostics.file}`);
      app.quit();
    }
  } catch (failure) {
    restartRequest = null;
    dialog.showErrorBox('StandTerm Desktop could not recover', failure.message);
    app.quit();
  } finally { failurePending = false; }
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
    if (!await confirmSetupQuit()) { restartRequest = null; quitting = false; return; }
    if (capture?.active || capture?.confirming) {
      const allowed = smoke ? (await capture.stop(), true) : await capture.confirmStop('quit').catch(() => false);
      if (!allowed) { restartRequest = null; quitting = false; return; }
    }
    cancelSetup();
    await finishBackendAndBrowser();
    if (restartRequest) {
      if (restartRequest.action) await coreStore.queue(restartRequest.action);
      app.relaunch();
    }
    stopped = true;
    if (tray) tray.destroy();
    app.exit(exitCode);
  })().catch(error => {
    restartRequest = null;
    if (!smoke) dialog.showErrorBox('StandTerm could not complete shutdown', error.message);
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
    void handleCoreFailure(error);
  });
}
process.on('SIGINT', () => app.quit());
process.on('SIGTERM', () => app.quit());
