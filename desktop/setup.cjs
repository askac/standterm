'use strict';

const { app, BrowserWindow, dialog, session } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { macPythonCandidates, validMacPython, MACOS_HELP } = require('./macos-python.cjs');

const SETUP_URL = pathToFileURL(path.join(__dirname, 'setup.html')).href;
const HELP = 'Install WSL and Python 3.10+ with venv support in the selected distribution first.\n\n'
  + 'For Ubuntu/Debian, run this yourself in WSL:\nsudo apt install python3 python3-venv\n\n'
  + 'StandTerm never runs sudo or installs system Python automatically.';
const WINDOWS_HELP = 'Install 64-bit Python 3.10+ with venv support on Windows first.\n\n'
  + 'StandTerm can locate python.exe on PATH or let you select it. Microsoft Store aliases and py.exe '
  + 'are not launched automatically. No system Python installation or administrator access is requested.';
const PYTHON_PROBE = 'import sys, struct, importlib.util, json; print(json.dumps({"type":"python_info",'
  + '"executable":sys.executable,"platform":sys.platform,"version":list(sys.version_info[:2]),'
  + '"machine":__import__("platform").machine(),"bits":struct.calcsize("P")*8,"venv":bool(importlib.util.find_spec("venv") and importlib.util.find_spec("ensurepip"))}))';
const ERRORS = {
  python_required: HELP,
  venv_failed: `Python could not create the private venv.\n\n${HELP}`,
  dependencies_failed: 'Dependency installation or verification failed. Check internet access and the runtime setup.log, then retry.',
  setup_busy: 'This runtime is in use by StandTerm or another setup. Quit that desktop mode before retrying.',
  modified_runtime: 'The managed Core contains modified files. Setup will not overwrite them.',
  unsafe_runtime_path: 'The runtime directory is not safe to use. Setup will not overwrite unrelated files or follow directory links.',
  invalid_bundle: 'The bundled Core failed its integrity check. Reinstall StandTerm Desktop.',
  setup_canceled: 'Setup was canceled. Restart StandTerm to retry.',
  setup_timeout: 'Setup timed out. Check internet access and retry.',
  git_required: 'Git is unavailable in the selected backend environment. Install Git there, or restore the bundled Core.',
  git_dirty: 'The Git Core has local changes. Keep them or resolve them in its checkout before updating. Nothing was reset or stashed.',
  git_diverged: 'The Git Core cannot fast-forward to the official branch. Local history is retained. Restore bundled Core or resolve the checkout manually.',
  git_source_changed: 'The managed Git origin or branch has changed. Expected the official repository and main branch.',
  invalid_git_workspace: 'The private Git checkout is incomplete or damaged. It is retained. Restore the bundled Core or repair that checkout manually.',
  git_needs_setup: 'The Git Core requirements changed or its environment is missing. Use Prepare Git environment from Core source (Advanced).',
  git_failed: 'Git could not complete the operation. Check network access and the runtime setup.log. Prepared files are retained.',
  invalid_archive: 'The recovery archive failed verification. Reinstall StandTerm Desktop if its installed bundle is also damaged.',
  setup_failed: 'Setup failed. Check the selected Python environment and available disk space.',
};
let window;
let current;
let canceled = false;
let closeRequest;
let setupFinished;
let executionFinished;
const canceledError = () => Object.assign(new Error(ERRORS.setup_canceled), { code: 'SETUP_CANCELED' });

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
  closeRequest = dialog.showMessageBox(target, {
    type: 'question', title: 'Cancel StandTerm setup?',
    message: 'The Python environment is still being prepared.',
    detail: 'Keep this window open or minimize it to continue. Canceling stops the owned installation processes; '
      + 'prepared files are retained so you can retry on the next launch.',
    buttons: ['Keep preparing', 'Cancel setup'], defaultId: 0, cancelId: 0, noLink: true,
  }).then(answer => {
    // Setup may finish while the confirmation is open. Do not cancel a
    // completed setup or apply its stale answer to a subsequent window.
    if (window !== target || target.isDestroyed() || answer.response !== 1) return false;
    cancelSetup();
    target.setTitle('StandTerm Desktop - Canceling setup');
    void target.webContents.executeJavaScript(
      "document.getElementById('stage').textContent = 'Canceling setup. Waiting for installation processes to stop...';",
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

function execute(executable, args, { encoding = 'utf8', timeout = 20000, progress = null, stream = false, help = HELP } = {}) {
  const running = new Promise((resolve, reject) => {
    if (canceled) { reject(canceledError()); return; }
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
    child.cancelSetup = () => abort(canceledError());
    const timer = setTimeout(() => abort(new Error(ERRORS.setup_timeout)), timeout);
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
      if (canceled) reject(canceledError());
      else if (abortError) reject(abortError);
      else if (protocolError || (stream && output.length)) reject(new Error('Invalid setup control response.'));
      else if (stream) {
        const result = frames.at(-1);
        if (code !== 0 || !result || result.type === 'error') reject(Object.assign(new Error(
          ['python_required', 'venv_failed'].includes(result?.code) ? help : ERRORS[result?.code] || help), { code: result?.code }));
        else resolve(result);
      } else if (code !== 0) reject(new Error(help));
      else resolve(output.toString(encoding).replace(/^\uFEFF/, '').trim());
    });
  });
  executionFinished = running.then(() => {}, () => {});
  return running;
}

async function windowsPython(saved) {
  const candidates = [];
  if (typeof saved === 'string') candidates.push(saved);
  try {
    candidates.push(...(await execute('where.exe', ['python.exe'], { help: WINDOWS_HELP })).split(/\r?\n/));
  } catch { /* Manual selection remains available when PATH has no Python. */ }
  async function probe(candidate) {
    if (!path.win32.isAbsolute(candidate) || /\\Microsoft\\WindowsApps\\/i.test(candidate)
        || !/^python(?:3(?:\.\d+)?)?\.exe$/i.test(path.win32.basename(candidate))) throw new Error(WINDOWS_HELP);
    const result = await execute(candidate, ['-I', '-c', PYTHON_PROBE], { stream: true, help: WINDOWS_HELP });
    if (result.type !== 'python_info' || result.platform !== 'win32' || result.bits !== 64 || !result.venv
        || !Array.isArray(result.version) || result.version[0] !== 3 || result.version[1] < 10
        || !path.win32.isAbsolute(result.executable)) throw new Error(WINDOWS_HELP);
    return result.executable;
  }
  for (const candidate of [...new Set(candidates)].slice(0, 5)) {
    try { return await probe(candidate.trim()); } catch { if (canceled) throw canceledError(); }
  }
  const answer = await dialog.showMessageBox({ type: 'info', title: 'StandTerm Desktop: Python required',
    message: WINDOWS_HELP, buttons: ['Cancel', 'Select installed python.exe...'], defaultId: 0, cancelId: 0 });
  if (answer.response !== 1) throw canceledError();
  const selected = await dialog.showOpenDialog({ title: 'Select an installed 64-bit Python interpreter',
    properties: ['openFile'], filters: [{ name: 'Python executable', extensions: ['exe'] }] });
  if (selected.canceled || selected.filePaths.length !== 1) throw canceledError();
  return probe(selected.filePaths[0]);
}

async function macosPython(saved) {
  async function probe(candidate) {
    if (!path.posix.isAbsolute(candidate) || /[\r\n\0]/.test(candidate) || candidate === '/usr/bin/python3') {
      throw new Error(MACOS_HELP);
    }
    const result = await execute(candidate, ['-I', '-c', PYTHON_PROBE], { stream: true, help: MACOS_HELP });
    if (!validMacPython(result, process.arch)) throw new Error(MACOS_HELP);
    return result.executable;
  }
  for (const candidate of macPythonCandidates(saved, process.env)) {
    try { return await probe(candidate); } catch { if (canceled) throw canceledError(); }
  }
  const answer = await dialog.showMessageBox({ type: 'info', title: 'StandTerm Desktop: Python required',
    message: MACOS_HELP, buttons: ['Cancel', 'Select installed Python...'], defaultId: 0, cancelId: 0 });
  if (answer.response !== 1) throw canceledError();
  const selected = await dialog.showOpenDialog({ title: 'Select a native macOS Python 3.10+ interpreter',
    properties: ['openFile'] });
  if (selected.canceled || selected.filePaths.length !== 1) throw canceledError();
  return probe(selected.filePaths[0]);
}

async function setupEnvironment(mode, installer = false) {
  if (!['windows', 'wsl', 'macos'].includes(mode)) throw new Error('Choose a supported desktop backend.');
  const windows = mode === 'windows';
  const macos = mode === 'macos';
  const native = windows || macos;
  const help = macos ? MACOS_HELP : windows ? WINDOWS_HELP : HELP;
  const bundle = path.join(process.resourcesPath, 'bundle');
  const metadata = JSON.parse(await fs.readFile(path.join(bundle, 'manifest.json'), 'utf8'));
  if (!/^[a-f0-9]{64}$/.test(metadata.id)) throw new Error(ERRORS.invalid_bundle);
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
    executable = await (macos ? macosPython(settings?.python) : windowsPython(settings?.python));
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
    const distributions = (await execute('wsl.exe', ['--list', '--quiet'], { encoding: 'utf16le' }))
      .split(/\r?\n/).map(item => item.trim()).filter(Boolean);
    if (!distributions.length) throw new Error(HELP);
    distro = settings?.distro;
    if (installer || !distributions.includes(distro)) {
      const choices = distributions.slice(0, 12);
      const result = await dialog.showMessageBox({
        type: 'question', title: 'StandTerm Desktop: select WSL',
        message: 'Select an existing WSL distribution for StandTerm Core.', detail: HELP,
        buttons: [...choices, 'Cancel'], cancelId: choices.length, defaultId: choices.length,
        noLink: true,
      });
      if (result.response >= choices.length) throw canceledError();
      distro = choices[result.response];
    }
    const prefix = ['--distribution', distro, '--exec'];
    const linuxBundle = await execute('wsl.exe', [...prefix, 'wslpath', '-u', bundle]);
    if (!linuxBundle.startsWith('/') || /[\r\n\0]/.test(linuxBundle)) throw new Error(ERRORS.invalid_bundle);
    executable = 'wsl.exe';
    args = [...prefix, 'python3', '-I', `${linuxBundle}/bootstrap.py`, '--bundle', linuxBundle];
    saved = { version: 1, distro };
  }
  return { windows, macos, native, help, bundle, metadata, settingsPath, executable, args, saved, distro };
}

async function preparePackagedBackend(mode, { installer = false } = {}) {
  const environment = await setupEnvironment(mode, installer);
  const { windows, macos, native, help, metadata, settingsPath, executable, args, saved, distro } = environment;
  let result = await execute(executable, args, { stream: true, timeout: 60000, help });
  if (result.type === 'needs_setup') {
    const answer = await dialog.showMessageBox({
      type: 'question', title: 'Prepare StandTerm Core',
      message: `Create a private StandTerm environment in ${macos ? 'macOS' : windows ? 'Windows' : distro}?`,
      detail: `Requires Python 3.10+ and venv support ${macos ? 'on native macOS' : windows ? 'on Windows (64-bit)' : 'inside WSL'}.\n\n`
        + `This copies the bundled Core into ${macos ? '~/Library/Application Support/StandTermDesktop/runtimes/' : windows ? '%LOCALAPPDATA%\\StandTermDesktop\\runtimes\\' : '~/.local/share/standterm-desktop/runtimes/'}, creates its own venv, `
        + 'and downloads and installs Python dependencies from your configured package index. '
        + 'Dependencies can execute installation code. Internet access and disk space are required.\n\n'
        + 'No system Python installation, sudo, Git checkout changes or existing-session interruption. '
        + 'Failed setup is retained for retry. Uninstall keeps environments by default; optional cleanup moves only verified idle venvs to a recovery folder. Core and user data are retained.',
      buttons: ['Cancel', 'Create environment and install dependencies'], defaultId: 0, cancelId: 0,
    });
    if (answer.response !== 1) throw canceledError();
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

async function cleanupManagedVenvs(mode) {
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
    const linuxBundle = await execute('wsl.exe', [...prefix, 'wslpath', '-u', bundle]);
    if (!linuxBundle.startsWith('/') || /[\r\n\0]/.test(linuxBundle)) throw new Error(ERRORS.invalid_bundle);
    executable = 'wsl.exe';
    args = [...prefix, 'python3', '-I', `${linuxBundle}/runtime_cleanup.py`];
  }
  const inventory = await execute(executable, [...args, '--inventory'], { stream: true, timeout: 60000,
    help: 'Environment inventory was unavailable. No venv cleanup was started.' });
  if (inventory.type !== 'cleanup_inventory' || !Array.isArray(inventory.results) || inventory.results.length > 32
      || inventory.results.some(item => !/^[a-f0-9]{64}$/.test(item.id) || typeof item.source !== 'string'
        || !['retained', 'candidate'].includes(item.status))) throw new Error('Invalid cleanup inventory.');
  const candidates = inventory.results.filter(item => item.status === 'candidate');
  if (!candidates.length) return { mode, status: 'checked', results: inventory.results };
  const answer = await dialog.showMessageBox({ type: 'question', title: 'Confirm environment cleanup',
    message: `Move these idle venvs to recovery in ${mode === 'windows' ? 'Windows' : `WSL: ${settings.distro}`}?`,
    detail: candidates.map(item => item.source).join('\n') + '\n\nNo disk space is freed. Core and settings are retained. '
      + 'Any environment that becomes busy or fails verification will be retained. Cancel keeps all listed venvs.',
    buttons: ['Keep environments', 'Move listed venvs to recovery'], defaultId: 0, cancelId: 0, noLink: true });
  if (answer.response !== 1) return { mode, status: 'retained', reason: 'User kept environments.' };
  const result = await execute(executable, [...args, '--detach-idle-venvs', JSON.stringify(candidates.map(item => item.id))],
    { stream: true, timeout: 60000, help: 'Cleanup did not report completion. Check venv-recovery before retrying.' });
  if (result.type !== 'cleanup_summary' || !Array.isArray(result.results) || result.results.length > 32
      || result.results.some(item => !/^[a-f0-9]{64}$/.test(item.id) || !['retained', 'detached'].includes(item.status))) {
    throw new Error('Invalid cleanup response.');
  }
  return { mode, status: 'checked', results: result.results };
}

module.exports = { preparePackagedBackend, focusSetup, cancelSetup, confirmSetupQuit,
  stopSetup, cleanupManagedVenvs, modeProfile, manageCore };

async function manageCore(mode, action) {
  if (!['status', 'enable', 'update', 'prepare', 'check', 'recover'].includes(action)) throw new Error('Invalid Core action.');
  // Select the base interpreter without requiring either Core or venv to work.
  const environment = await setupEnvironment(mode);
  const { windows, macos, native, help, executable, args, metadata, settingsPath, saved, distro } = environment;
  const runtimePath = windows ? path.win32 : path.posix;
  const scriptIndex = args.indexOf('-I') + 1;
  const bundle = args[args.indexOf('--bundle') + 1];
  const managedArgs = [...args];
  managedArgs[scriptIndex] = runtimePath.join(bundle, 'core_manager.py');
  managedArgs.push('--action', action);
  const result = ['status', 'check'].includes(action)
    ? await execute(executable, managedArgs, { stream: true, timeout: 60000, help })
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
  const { executable, help, macos, windows, distro } = environment;
  const isolated = session.fromPartition('standterm-setup');
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: details.url !== SETUP_URL }));
  window = new BrowserWindow({ title: 'StandTerm Desktop - Preparing environment',
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
    await window.webContents.executeJavaScript(`document.getElementById('requirements').textContent = ${JSON.stringify(
      macos ? 'Preparing Core for native macOS.' : windows ? 'Preparing Core for native Windows.' : `Preparing Core inside WSL: ${distro}.`)}`);
    return await execute(executable, args, { stream: true, timeout: 30 * 60 * 1000, help, progress: stage => {
      const labels = { git: 'Updating the private Git checkout...', copy: 'Copying verified Core files...', venv: 'Creating the private Python environment...',
        dependencies: 'Installing Python dependencies. This can take several minutes...', verify: 'Verifying the installed dependencies...' };
      if (labels[stage] && !canceled && !window.isDestroyed()) void window.webContents.executeJavaScript(
        `document.getElementById('stage').textContent = ${JSON.stringify(labels[stage])}`,
      ).catch(() => {});
    } });
  } finally {
    if (!window.isDestroyed()) { window.removeListener('closed', cancelSetup); window.destroy(); }
    window = null;
    finishSetup();
    setupFinished = null;
  }
}
