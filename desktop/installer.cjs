'use strict';

const { spawn } = require('node:child_process');
const path = require('node:path');
const { shortcutPlan, applyShortcuts } = require('./installer-shortcuts.cjs');

function installerRequest(argv) {
  const flags = argv.filter(value => value.startsWith('--installer-'));
  if (!flags.length) return null;
  const prepare = flags.filter(value => /^--installer-prepare=(windows|both|wsl)$/.test(value));
  const uninstall = flags.includes('--installer-uninstall');
  const cleanup = flags.includes('--installer-cleanup-venvs');
  const parents = flags.filter(value => /^--installer-parent=[1-9][0-9]{0,9}$/.test(value));
  if (parents.length !== 1 || prepare.length + Number(uninstall) !== 1 || (cleanup && !uninstall)
      || flags.length !== 2 + Number(cleanup)) throw new Error('Invalid installer request.');
  const parent = Number(parents[0].split('=')[1]);
  if (parent > 0xffffffff) throw new Error('Invalid installer parent.');
  return { action: uninstall ? 'uninstall' : 'prepare', cleanup, parent,
    modes: uninstall ? [] : prepare[0].endsWith('=both') ? ['windows', 'wsl'] : [prepare[0].split('=')[1]] };
}

function watchInstaller(parent, onLost, launch = spawn) {
  // Hold a real Windows process handle, not a PID-only polling loop susceptible
  // to PID reuse. The fixed script receives only a validated decimal process ID.
  if (!Number.isInteger(parent) || parent < 1 || parent > 0xffffffff) throw new Error('Invalid installer parent.');
  const command = `$ErrorActionPreference='Stop'; try { $p=Get-Process -Id ${parent}; `
    + `$h=$p.Handle; if ($p.HasExited) { exit 1 }; [Console]::Out.WriteLine('{"type":"parent_ready"}'); `
    + '$p.WaitForExit(); exit 0 } catch { exit 1 }';
  const watcher = launch(path.join(process.env.SystemRoot || 'C:\\Windows',
    'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
  ['-NoProfile', '-NonInteractive', '-Command', command],
  { windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'ignore'] });
  let stopping = false;
  let lost = false;
  let ready = false;
  let output = '';
  let timer;
  const started = new Promise((resolve, reject) => {
    const fail = () => {
      if (stopping || lost) return;
      lost = true;
      clearTimeout(timer);
      reject(new Error('The owning installer exited or could not be monitored.'));
      onLost();
    };
    timer = setTimeout(fail, 15000);
    watcher.once('error', fail);
    watcher.once('exit', fail);
    watcher.stdout.on('data', bytes => {
      output += bytes.toString('utf8');
      if (output.length > 1024) { fail(); return; }
      if (!output.includes('\n')) return;
      try {
        if (ready || JSON.parse(output.trim()).type !== 'parent_ready') throw new Error();
        ready = true;
        clearTimeout(timer);
        resolve();
      } catch { fail(); }
    });
  });
  return { started, alive: () => ready && !lost,
    stop: () => { stopping = true; clearTimeout(timer); watcher.kill(); } };
}

async function performMaintenance(request, { prepare, cleanup, shortcuts, ensureAlive, report }) {
  ensureAlive();
  if (request.action === 'prepare') {
    for (const mode of request.modes) {
      await prepare(mode, { installer: true });
      ensureAlive();
    }
    await shortcuts(request.modes);
    ensureAlive();
    return;
  }
  if (request.cleanup) {
    const results = [];
    for (const mode of ['windows', 'wsl']) {
      ensureAlive();
      try { results.push(await cleanup(mode)); }
      catch { results.push({ mode, status: 'unknown', reason: 'Cleanup incomplete or unavailable; inspect venv-recovery.' }); }
    }
    ensureAlive();
    await report(results);
  }
  ensureAlive();
  await shortcuts([]);
}

async function runInstaller(request) {
  const { app, dialog, shell } = require('electron');
  const { preparePackagedBackend, cleanupManagedVenvs, stopSetup, confirmSetupQuit } = require('./setup.cjs');
  let exiting = false;
  const watcher = watchInstaller(request.parent, () => {
    void stopSetup().finally(() => { exiting = true; app.exit(2); });
  });
  app.on('window-all-closed', () => {});
  app.on('before-quit', event => {
    if (exiting) return;
    event.preventDefault();
    void confirmSetupQuit().then(async allowed => {
      if (allowed) { await stopSetup(); exiting = true; app.exit(2); }
    });
  });
  try {
    await watcher.started;
    const ensureAlive = () => { if (!watcher.alive()) throw new Error('The owning installer exited.'); };
    await performMaintenance(request, {
      prepare: preparePackagedBackend, cleanup: cleanupManagedVenvs, ensureAlive,
      shortcuts: selected => applyShortcuts(shell, shortcutPlan(process.execPath, app.getPath('desktop'),
        path.join(app.getPath('appData'), 'Microsoft', 'Windows', 'Start Menu', 'Programs'), selected)),
      report: async results => {
        const detached = results.flatMap(result => result.results || []).filter(item => item.status === 'detached').length;
        const retained = results.flatMap(result => result.results || [result]).filter(item => item.status === 'retained').length;
        const unknown = results.filter(result => result.status === 'unknown').length;
        await dialog.showMessageBox({ type: 'info', title: 'StandTerm environment cleanup',
          message: `${detached} confirmed move(s); ${retained} retained entry/entries; ${unknown} unconfirmed environment(s).`,
          detail: 'In-use, legacy, unverified and unavailable environments are retained. Only Windows and the configured WSL distribution were checked.\n\n'
            + 'Recovery folders (disk space is not freed):\n%LOCALAPPDATA%\\StandTermDesktop\\venv-recovery\n'
            + '~/.local/share/standterm-desktop/venv-recovery\n\nIf a result is unconfirmed, inspect these folders; some moves may already have completed. '
            + 'Core, settings, captures, system Python and WSL distributions are untouched.',
          buttons: ['Continue uninstall'] });
      },
    });
    ensureAlive();
    return 0;
  } catch (error) {
    await stopSetup();
    if (watcher.alive()) await dialog.showMessageBox({ type: 'error', title: 'StandTerm setup did not complete',
      message: error.message,
      detail: 'Prepared environments and application files are retained. Run the installer again to retry. No terminal backend was started.',
      buttons: ['Return to installer'] });
    return error.code === 'SETUP_CANCELED' ? 2 : 1;
  } finally { exiting = true; watcher.stop(); }
}

module.exports = { installerRequest, watchInstaller, performMaintenance, runInstaller };
