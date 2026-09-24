'use strict';

const { app, BrowserWindow, dialog, session } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { macPythonCandidates, validMacPython } = require('./macos-python.cjs');
const { create } = require('./i18n.js');
const { createLanguage } = require('./language.cjs');

const SETUP_URL = pathToFileURL(path.join(__dirname, 'setup.html')).href;
const PYTHON_PROBE = 'import sys, struct, importlib.util, json; print(json.dumps({"type":"python_info",'
  + '"executable":sys.executable,"platform":sys.platform,"version":list(sys.version_info[:2]),'
  + '"machine":__import__("platform").machine(),"bits":struct.calcsize("P")*8,"venv":bool(importlib.util.find_spec("venv") and importlib.util.find_spec("ensurepip"))}))';
const ERROR_CODES = ['dependencies_failed', 'setup_busy', 'modified_runtime', 'unsafe_runtime_path',
  'invalid_bundle', 'setup_canceled', 'setup_timeout', 'git_required', 'git_dirty', 'git_diverged',
  'git_source_changed', 'invalid_git_workspace', 'git_needs_setup', 'git_failed', 'invalid_archive', 'setup_failed'];
let window;
let setupLanguage = create('en');
let current;
let canceled = false;
let closeRequest;
let setupFinished;
let executionFinished;
const canceledError = (t = setupLanguage.t) => Object.assign(new Error(t('desktop.setup.error_setup_canceled')), { code: 'SETUP_CANCELED' });

function focusSetup() { if (window && !window.isDestroyed()) { window.show(); window.focus(); } }
function cancelSetup() { canceled = true; current?.cancelSetup?.(); }
async function stopSetup() { cancelSetup(); await executionFinished; }

function modeProfile(mode) {
  if (!['windows', 'wsl', 'macos'].includes(mode)) throw new Error('Invalid desktop mode.');
  return path.join(app.getPath('appData'), 'StandTermDesktopEvaluation', mode);
}

async function requestSetupCancel() {
  if (!window || window.isDestroyed() || canceled) return true;
  if (closeRequest) return closeRequest;
  const target = window;
  const { t } = setupLanguage;
  closeRequest = dialog.showMessageBox(target, {
    type: 'question', title: t('desktop.setup.cancel_title'),
    message: t('desktop.setup.cancel_message'),
    detail: t('desktop.setup.cancel_detail'),
    buttons: [t('desktop.setup.keep_preparing'), t('desktop.setup.cancel_setup')], defaultId: 0, cancelId: 0, noLink: true,
  }).then(answer => {
    // Setup may finish while the confirmation is open. Do not cancel a
    // completed setup or apply its stale answer to a subsequent window.
    if (window !== target || target.isDestroyed() || answer.response !== 1) return false;
    cancelSetup();
    target.setTitle(t('desktop.setup.canceling_title'));
    void target.webContents.executeJavaScript(
      `document.getElementById('stage').textContent = ${JSON.stringify(t('desktop.setup.canceling_status'))}`,
    ).catch(() => {});
    return true;
  }).catch(() => false).finally(() => { closeRequest = null; });
  return closeRequest;
}

async function confirmSetupQuit() {
  const finished = setupFinished;
  if (!await requestSetupCancel()) return false;
  // Keep the parent alive until cooperative cancellation finishes, so the
  // progress window does not disappear while installation is still running.
  await finished;
  return true;
}

function execute(executable, args, { encoding = 'utf8', timeout = 20000, progress = null, stream = false, t = create('en').t, help = t('desktop.setup.help_wsl') } = {}) {
  const running = new Promise((resolve, reject) => {
    if (canceled) { reject(canceledError(t)); return; }
    const child = spawn(executable, args, { windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHON_MANAGER_AUTOMATIC_INSTALL: 'false' } });
    current = child;
    child.stdin.on('error', () => {});
    child.stderr.resume();
    let output = Buffer.alloc(0);
    let protocolError = false;
    let abortError;
    let forceTimer;
    const frames = [];
    function abort(error) {
      if (abortError) return;
      abortError = error;
      child.stdin.end();
      // Allow the bootstrap to reap its owned process group/job before killing
      // the outer launcher. A new setup cannot race a still-closing process.
      forceTimer = setTimeout(() => { child.kill(); reject(abortError); }, 25000);
    }
    child.cancelSetup = () => abort(canceledError(t));
    const timer = setTimeout(() => abort(new Error(t('desktop.setup.error_setup_timeout'))), timeout);
    child.on('error', () => { clearTimeout(timer); clearTimeout(forceTimer); reject(new Error(help)); });
    child.stdout.on('data', bytes => {
      output = Buffer.concat([output, bytes]);
      if (output.length > 65536) { protocolError = true; abort(new Error('Invalid setup control response.')); return; }
      if (!stream) return;
      let end;
      while ((end = output.indexOf(10)) >= 0) {
        const line = output.subarray(0, end).toString('utf8'); output = output.subarray(end + 1);
        try {
          const frame = JSON.parse(line);
          if (!['progress', 'ready', 'needs_setup', 'error', 'python_info', 'cleanup_inventory', 'cleanup_summary', 'core_status'].includes(frame.type) || frames.length >= 32) throw new Error();
          frames.push(frame);
          if (frame.type === 'progress') progress?.(frame.stage);
        } catch { protocolError = true; abort(new Error('Invalid setup control response.')); }
      }
    });
    child.on('close', code => {
      clearTimeout(timer);
      clearTimeout(forceTimer);
      if (current === child) current = null;
      if (canceled) reject(canceledError(t));
      else if (abortError) reject(abortError);
      else if (protocolError || (stream && output.length)) reject(new Error('Invalid setup control response.'));
      else if (stream) {
        const result = frames.at(-1);
        if (code !== 0 || !result || result.type === 'error') reject(Object.assign(new Error(
          ERROR_CODES.includes(result?.code) ? t(`desktop.setup.error_${result.code}`) : help), { code: result?.code }));
        else resolve(result);
      } else if (code !== 0) reject(new Error(help));
      else resolve(output.toString(encoding).replace(/^\uFEFF/, '').trim());
    });
  });
  executionFinished = running.then(() => {}, () => {});
  return running;
}

async function windowsPython(saved, t) {
  const help = t('desktop.setup.help_windows');
  const candidates = [];
  if (typeof saved === 'string') candidates.push(saved);
  try {
    candidates.push(...(await execute('where.exe', ['python.exe'], { help, t })).split(/\r?\n/));
  } catch { /* Manual selection remains available when PATH has no Python. */ }
  async function probe(candidate) {
    if (!path.win32.isAbsolute(candidate) || /\\Microsoft\\WindowsApps\\/i.test(candidate)
        || !/^python(?:3(?:\.\d+)?)?\.exe$/i.test(path.win32.basename(candidate))) throw new Error(help);
    const result = await execute(candidate, ['-I', '-c', PYTHON_PROBE], { stream: true, help, t });
    if (result.type !== 'python_info' || result.platform !== 'win32' || result.bits !== 64 || !result.venv
        || !Array.isArray(result.version) || result.version[0] !== 3 || result.version[1] < 10
        || !path.win32.isAbsolute(result.executable)) throw new Error(help);
    return result.executable;
  }
  for (const candidate of [...new Set(candidates)].slice(0, 5)) {
    try { return await probe(candidate.trim()); } catch { if (canceled) throw canceledError(t); }
  }
  const answer = await dialog.showMessageBox({ type: 'info', title: t('desktop.setup.python_required_title'),
    message: help, buttons: [t('desktop.common.cancel'), t('desktop.setup.select_python_windows')], defaultId: 0, cancelId: 0 });
  if (answer.response !== 1) throw canceledError(t);
  const selected = await dialog.showOpenDialog({ title: t('desktop.setup.select_python_windows_title'),
    properties: ['openFile'], filters: [{ name: t('desktop.setup.python_executable'), extensions: ['exe'] }] });
  if (selected.canceled || selected.filePaths.length !== 1) throw canceledError(t);
  return probe(selected.filePaths[0]);
}

async function macosPython(saved, t) {
  const help = t('desktop.setup.help_macos');
  async function probe(candidate) {
    if (!path.posix.isAbsolute(candidate) || /[\r\n\0]/.test(candidate) || candidate === '/usr/bin/python3') {
      throw new Error(help);
    }
    const result = await execute(candidate, ['-I', '-c', PYTHON_PROBE], { stream: true, help, t });
    if (!validMacPython(result, process.arch)) throw new Error(help);
    return result.executable;
  }
  for (const candidate of macPythonCandidates(saved, process.env)) {
    try { return await probe(candidate); } catch { if (canceled) throw canceledError(t); }
  }
  const answer = await dialog.showMessageBox({ type: 'info', title: t('desktop.setup.python_required_title'),
    message: help, buttons: [t('desktop.common.cancel'), t('desktop.setup.select_python_macos')], defaultId: 0, cancelId: 0 });
  if (answer.response !== 1) throw canceledError(t);
  const selected = await dialog.showOpenDialog({ title: t('desktop.setup.select_python_macos_title'),
    properties: ['openFile'] });
  if (selected.canceled || selected.filePaths.length !== 1) throw canceledError(t);
  return probe(selected.filePaths[0]);
}

async function setupEnvironment(mode, installer, language) {
  const { t } = language;
  if (!['windows', 'wsl', 'macos'].includes(mode)) throw new Error('Choose a supported desktop backend.');
  const windows = mode === 'windows';
  const macos = mode === 'macos';
  const native = windows || macos;
  const help = t(`desktop.setup.help_${mode}`);
  const bundle = path.join(process.resourcesPath, 'bundle');
  const metadata = JSON.parse(await fs.readFile(path.join(bundle, 'manifest.json'), 'utf8'));
  if (!/^[a-f0-9]{64}$/.test(metadata.id)) throw new Error(t('desktop.setup.error_invalid_bundle'));
  const settingsPath = path.join(installer ? modeProfile(mode) : app.getPath('userData'), 'launcher.json');
  let settings;
  try {
    settings = JSON.parse(await fs.readFile(settingsPath, 'utf8'));
    if (settings.version !== 1) settings = null;
  } catch (error) { if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error; }
  let distro;
  let executable;
  let args;
  let saved;
  if (native) {
    executable = await (macos ? macosPython(settings?.python, t) : windowsPython(settings?.python, t));
    args = ['-I', path.join(bundle, 'bootstrap.py'), '--bundle', bundle];
    saved = { version: 1, python: executable };
  } else {
    // Preserve the selected distro from the WSL-only evaluation without migrating
    // browser profiles, credentials or native-mode settings.
    if (!settings) {
      try {
        const legacy = JSON.parse(await fs.readFile(path.join(path.dirname(settingsPath), '..', 'launcher.json'), 'utf8'));
        if (legacy.version === 1 && typeof legacy.distro === 'string') settings = legacy;
      } catch { /* A fresh selection is safe if the legacy file is absent/invalid. */ }
    }
    const distributions = (await execute('wsl.exe', ['--list', '--quiet'], { encoding: 'utf16le', t, help }))
      .split(/\r?\n/).map(item => item.trim()).filter(Boolean);
    if (!distributions.length) throw new Error(help);
    distro = settings?.distro;
    if (installer || !distributions.includes(distro)) {
      const choices = distributions.slice(0, 12);
      const result = await dialog.showMessageBox({
        type: 'question', title: t('desktop.setup.select_wsl_title'),
        message: t('desktop.setup.select_wsl_message'), detail: help,
        buttons: [...choices, t('desktop.common.cancel')], cancelId: choices.length, defaultId: choices.length,
        noLink: true,
      });
      if (result.response >= choices.length) throw canceledError(t);
      distro = choices[result.response];
    }
    const prefix = ['--distribution', distro, '--exec'];
    const linuxBundle = await execute('wsl.exe', [...prefix, 'wslpath', '-u', bundle], { t });
    if (!linuxBundle.startsWith('/') || /[\r\n\0]/.test(linuxBundle)) throw new Error(t('desktop.setup.error_invalid_bundle'));
    executable = 'wsl.exe';
    args = [...prefix, 'python3', '-I', `${linuxBundle}/bootstrap.py`, '--bundle', linuxBundle];
    saved = { version: 1, distro };
  }
  return { language, t, windows, macos, native, help, bundle, metadata, settingsPath, executable, args, saved, distro };
}

async function preparePackagedBackend(mode, { installer = false,
  language = createLanguage(path.join(installer ? modeProfile(mode) : app.getPath('userData'), 'language.json')) } = {}) {
  const environment = await setupEnvironment(mode, installer, language);
  const { t, windows, macos, native, help, metadata, settingsPath, executable, args, saved, distro } = environment;
  let result = await execute(executable, args, { stream: true, timeout: 60000, help, t });
  if (result.type === 'needs_setup') {
    const answer = await dialog.showMessageBox({
      type: 'question', title: t('desktop.setup.prepare_title'),
      message: t('desktop.setup.prepare_message', { platform: macos ? 'macOS' : windows ? 'Windows' : distro }),
      detail: t('desktop.setup.prepare_detail', {
        requirements: t(`desktop.setup.requirements_${mode}`),
        path: macos ? '~/Library/Application Support/StandTermDesktop/runtimes/'
          : windows ? '%LOCALAPPDATA%\\StandTermDesktop\\runtimes\\' : '~/.local/share/standterm-desktop/runtimes/',
      }),
      buttons: [t('desktop.common.cancel'), t('desktop.setup.create_environment')], defaultId: 0, cancelId: 0,
    });
    if (answer.response !== 1) throw canceledError(t);
    result = await runPreparation(environment, [...args, '--prepare']);
  }
  const runtimePath = windows ? path.win32 : path.posix;
  if (result.type !== 'ready' || result.bundle_id !== metadata.id
      || typeof result.root !== 'string' || !runtimePath.isAbsolute(result.root)
      || result.python !== (windows ? runtimePath.join(result.root, 'tools', '.venv_win', 'Scripts', 'python.exe')
        : runtimePath.join(result.root, 'tools', macos ? '.venv_macos' : '.venv_wsl', 'bin', 'python'))) throw new Error('Invalid managed runtime response.');
  await fs.mkdir(path.dirname(settingsPath), { recursive: true });
  // This file contains only non-secret launcher metadata, never credentials.
  const temporary = `${settingsPath}.tmp`;
  await fs.writeFile(temporary, JSON.stringify(saved, null, 2), { mode: 0o600 });
  await fs.rename(temporary, settingsPath);
  if (native) return { executable: result.python, args: ['-u', runtimePath.join(result.root, 'desktop', 'backend.py')], cwd: result.root };
  return { executable: 'wsl.exe', args: ['--distribution', distro, '--cd', result.root, '--exec', result.python,
    '-u', `${result.root}/desktop/backend.py`], cwd: process.resourcesPath };
}

async function cleanupManagedVenvs(mode, { language = createLanguage(path.join(modeProfile(mode), 'language.json')) } = {}) {
  const { t } = language;
  if (!['windows', 'wsl'].includes(mode)) throw new Error('Environment cleanup is available through the Windows installer only.');
  // Only the configured interpreter/distribution is considered. Never discover
  // other projects or provision a WSL distribution during uninstallation.
  const settingsPath = path.join(modeProfile(mode), 'launcher.json');
  let settings;
  try { settings = JSON.parse(await fs.readFile(settingsPath, 'utf8')); }
  catch { return { mode, status: 'retained', reason: 'No readable launcher preference.' }; }
  if (settings.version !== 1) return { mode, status: 'retained', reason: 'Unknown launcher preference.' };
  const bundle = path.join(process.resourcesPath, 'bundle');
  let executable;
  let args;
  if (mode === 'windows') {
    if (typeof settings.python !== 'string' || !path.win32.isAbsolute(settings.python)
        || /\\Microsoft\\WindowsApps\\/i.test(settings.python)
        || !/^python(?:3(?:\.\d+)?)?\.exe$/i.test(path.win32.basename(settings.python))) {
      return { mode, status: 'retained', reason: 'No usable saved Python interpreter.' };
    }
    executable = settings.python;
    args = ['-I', path.join(bundle, 'runtime_cleanup.py')];
  } else {
    if (typeof settings.distro !== 'string' || !settings.distro || /[\r\n\0]/.test(settings.distro)) {
      return { mode, status: 'retained', reason: 'No configured WSL distribution.' };
    }
    const prefix = ['--distribution', settings.distro, '--exec'];
    const linuxBundle = await execute('wsl.exe', [...prefix, 'wslpath', '-u', bundle], { t });
    if (!linuxBundle.startsWith('/') || /[\r\n\0]/.test(linuxBundle)) throw new Error(t('desktop.setup.error_invalid_bundle'));
    executable = 'wsl.exe';
    args = [...prefix, 'python3', '-I', `${linuxBundle}/runtime_cleanup.py`];
  }
  const inventory = await execute(executable, [...args, '--inventory'], { stream: true, timeout: 60000,
    t, help: t('desktop.setup.cleanup_inventory_unavailable') });
  if (inventory.type !== 'cleanup_inventory' || !Array.isArray(inventory.results) || inventory.results.length > 32
      || inventory.results.some(item => !/^[a-f0-9]{64}$/.test(item.id) || typeof item.source !== 'string'
        || !['retained', 'candidate'].includes(item.status))) throw new Error('Invalid cleanup inventory.');
  const candidates = inventory.results.filter(item => item.status === 'candidate');
  if (!candidates.length) return { mode, status: 'checked', results: inventory.results };
  const answer = await dialog.showMessageBox({ type: 'question', title: t('desktop.setup.cleanup_title'),
    message: t('desktop.setup.cleanup_message', { platform: mode === 'windows' ? 'Windows' : `WSL: ${settings.distro}` }),
    detail: t('desktop.setup.cleanup_detail', { paths: candidates.map(item => item.source).join('\n') }),
    buttons: [t('desktop.setup.cleanup_keep'), t('desktop.setup.cleanup_move')], defaultId: 0, cancelId: 0, noLink: true });
  if (answer.response !== 1) return { mode, status: 'retained', reason: 'User kept environments.' };
  const result = await execute(executable, [...args, '--detach-idle-venvs', JSON.stringify(candidates.map(item => item.id))],
    { stream: true, timeout: 60000, t, help: t('desktop.setup.cleanup_unconfirmed') });
  if (result.type !== 'cleanup_summary' || !Array.isArray(result.results) || result.results.length > 32
      || result.results.some(item => !/^[a-f0-9]{64}$/.test(item.id) || !['retained', 'detached'].includes(item.status))) {
    throw new Error('Invalid cleanup response.');
  }
  return { mode, status: 'checked', results: result.results };
}

module.exports = { preparePackagedBackend, focusSetup, cancelSetup, confirmSetupQuit,
  stopSetup, cleanupManagedVenvs, modeProfile, manageCore };

async function manageCore(mode, action, { language = createLanguage(path.join(app.getPath('userData'), 'language.json')) } = {}) {
  if (!['status', 'enable', 'update', 'prepare', 'check', 'recover'].includes(action)) throw new Error('Invalid Core action.');
  // Select the base interpreter without requiring either Core or venv to work.
  const environment = await setupEnvironment(mode, false, language);
  const { t, windows, macos, native, help, executable, args, metadata, settingsPath, saved, distro } = environment;
  const runtimePath = windows ? path.win32 : path.posix;
  const scriptIndex = args.indexOf('-I') + 1;
  const bundle = args[args.indexOf('--bundle') + 1];
  const managedArgs = [...args];
  managedArgs[scriptIndex] = runtimePath.join(bundle, 'core_manager.py');
  managedArgs.push('--action', action);
  const result = ['status', 'check'].includes(action)
    ? await execute(executable, managedArgs, { stream: true, timeout: 60000, help, t })
    : await runPreparation(environment, managedArgs);
  if (action === 'status') {
    if (result.type !== 'core_status' || typeof result.git_available !== 'boolean'
        || !['absent', 'present', 'unavailable', 'invalid'].includes(result.workspace)
        || (result.workspace === 'present' && (!/^[a-f0-9]{40,64}$/.test(result.commit)
          || typeof result.dirty !== 'boolean'))) throw new Error('Invalid Core status.');
    return result;
  }
  const git = action !== 'recover';
  if (result.type !== 'ready' || result.source !== (git ? 'git' : 'bundled')
      || typeof result.root !== 'string' || !runtimePath.isAbsolute(result.root) || /[\r\n\0]/.test(result.root)
      || result.python !== runtimePath.join(result.root, 'tools', windows ? '.venv_win' : macos ? '.venv_macos' : '.venv_wsl',
        windows ? 'Scripts' : 'bin', windows ? 'python.exe' : 'python')
      || (git ? result.core_root !== runtimePath.join(result.root, 'repo')
        || !/^[a-f0-9]{40,64}$/.test(result.commit) || typeof result.dirty !== 'boolean' : result.bundle_id !== metadata.id)) {
    throw new Error('Invalid Core runtime response.');
  }
  await fs.mkdir(path.dirname(settingsPath), { recursive: true });
  await fs.writeFile(`${settingsPath}.tmp`, JSON.stringify(saved, null, 2), { mode: 0o600 });
  await fs.rename(`${settingsPath}.tmp`, settingsPath);
  const backendArgs = git ? ['-u', runtimePath.join(bundle, 'backend.py'), '--git-core', result.core_root]
    : ['-u', runtimePath.join(result.root, 'desktop', 'backend.py')];
  const command = native ? { executable: result.python, args: backendArgs, cwd: result.root }
    : { executable: 'wsl.exe', args: ['--distribution', distro, '--cd', result.root, '--exec', result.python, ...backendArgs],
      cwd: process.resourcesPath };
  return { ...command, source: git ? 'git' : 'bundled',
    coreSource: git ? `Git ${result.commit}${result.dirty ? ' (local changes)' : ''}` : 'Bundled' };
}

async function runPreparation(environment, args) {
  const { language, t, executable, help, macos, windows, distro } = environment;
  setupLanguage = language;
  const isolated = session.fromPartition('standterm-setup');
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: details.url !== SETUP_URL }));
  window = new BrowserWindow({ title: t('desktop.setup.progress_title'),
    width: 700, height: 500, resizable: false, autoHideMenuBar: true,
    webPreferences: { session: isolated, sandbox: true, contextIsolation: true, nodeIntegration: false, devTools: false } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.on('close', event => {
    event.preventDefault();
    void requestSetupCancel();
  });
  window.on('closed', cancelSetup);
  let finishSetup;
  setupFinished = new Promise(resolve => { finishSetup = resolve; });
  try {
    await window.loadURL(SETUP_URL);
    await window.webContents.executeJavaScript(`(() => {
      const copy = ${JSON.stringify({
        locale: language.locale, title: t('desktop.setup.progress_title'),
        heading: t('desktop.setup.progress_heading'),
        requirements: t(`desktop.setup.preparation_${macos ? 'macos' : windows ? 'windows' : 'wsl'}`, { distro }),
        stage: t('desktop.setup.progress_starting'), detail: t('desktop.setup.progress_detail'),
        closeHint: t('desktop.setup.progress_close_hint'), scope: t('desktop.setup.progress_scope'),
        aria: t('desktop.setup.progress_aria'),
      })};
      document.documentElement.lang = copy.locale;
      document.title = copy.title;
      for (const id of ['heading', 'requirements', 'stage', 'detail', 'closeHint', 'scope']) {
        document.getElementById(id).textContent = copy[id];
      }
      document.querySelector('progress').setAttribute('aria-label', copy.aria);
    })()`);
    return await execute(executable, args, { stream: true, timeout: 30 * 60 * 1000, help, t, progress: stage => {
      if (['git', 'copy', 'venv', 'dependencies', 'verify'].includes(stage) && !canceled && !window.isDestroyed()) {
        void window.webContents.executeJavaScript(
          `document.getElementById('stage').textContent = ${JSON.stringify(t(`desktop.setup.stage_${stage}`))}`,
        ).catch(() => {});
      }
    } });
  } finally {
    if (!window.isDestroyed()) { window.removeListener('closed', cancelSetup); window.destroy(); }
    window = null;
    finishSetup();
    setupFinished = null;
  }
}
