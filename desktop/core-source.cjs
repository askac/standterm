'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const ACTIONS = ['enable', 'update', 'prepare', 'recover'];

function sourceStore(profile, identity) {
  if (typeof identity !== 'string' || !/^[^\r\n\0:]{1,80}:[a-f0-9]{64}$/.test(identity)) throw new Error('Invalid installation identity.');
  const file = path.join(profile, 'core-source.json');
  const defaults = () => ({ version: 1, identity, source: 'bundled', pending: null });
  async function write(value) {
    await fs.mkdir(profile, { recursive: true });
    const temporary = `${file}.${randomUUID()}.tmp`;
    await fs.writeFile(temporary, JSON.stringify(value, null, 2), { flag: 'wx', mode: 0o600 });
    await fs.rename(temporary, file);
    return value;
  }
  async function read() {
    try {
      if ((await fs.stat(file)).size > 4096) throw new SyntaxError();
      const value = JSON.parse(await fs.readFile(file, 'utf8'));
      if (value && value.version === 1 && value.identity === identity && ['bundled', 'git'].includes(value.source)
          && (value.pending === null || ACTIONS.includes(value.pending))) {
        return { version: 1, identity, source: value.source, pending: value.pending };
      }
    } catch (error) { if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error; }
    // Installation changes discard source AND pending intent in the same write.
    return write(defaults());
  }
  return { read, reset: () => write(defaults()),
    async queue(action) {
      if (!ACTIONS.includes(action)) throw new Error('Invalid Core action.');
      return write({ ...await read(), pending: action });
    },
    async consume() {
      const value = await read();
      if (value.pending) await write({ ...value, pending: null });
      return value;
    },
    async select(source) {
      if (!['bundled', 'git'].includes(source)) throw new Error('Invalid Core source.');
      return write({ ...await read(), source, pending: null });
    },
  };
}

async function installedStore(profile, resources, version) {
  const metadata = JSON.parse(await fs.readFile(path.join(resources, 'bundle', 'manifest.json'), 'utf8'));
  return sourceStore(profile, `${version}:${metadata.id}`);
}

function coreController({ store, prepareBundled, manage, dialog, openLogs, restart }) {
  let lastAction = null;
  let managing = false;
  async function confirm(action) {
    const result = await dialog.showMessageBox({ type: 'warning', title: 'Change StandTerm Core',
      message: action === 'recover' ? 'Restore the Core bundled with this Desktop installation?' : 'Use the advanced Git Core environment?',
      detail: 'This restarts StandTerm and closes terminal sessions. Python dependencies may be downloaded and installed. '
        + (action === 'recover' ? 'Core files and data in the previous environment are retained. Recovery does not roll back user data. '
          : 'Git uses the official askac/standterm repository, main branch. Its files are not checked against the installed bundle hashes. '
            + 'Local changes are allowed, but updates refuse to overwrite them. The Desktop shell stays installed. ')
        + 'Authorization and recovery data are local to each Core source; switching may require reauthorization.',
      buttons: ['Cancel', 'Restart and continue'], defaultId: 0, cancelId: 0, noLink: true });
    if (result.response === 1) restart(action);
    return result.response === 1;
  }
  async function showManager() {
    if (managing) return;
    managing = true;
    try {
      const settings = await store.read();
      let status;
      let issue = '';
      try { status = await manage('status'); } catch (error) { issue = error.message; }
      const actions = [{ id: 'cancel', label: 'Close' }];
      if (status?.git_available) {
        actions.push({ id: settings.source === 'git' ? 'prepare' : 'enable',
          label: settings.source === 'git' ? 'Prepare Git environment' : 'Enable Git Core' });
        if (status.workspace === 'present') {
          if (settings.source !== 'git') actions.push({ id: 'prepare', label: 'Prepare Git environment' });
          actions.push({ id: 'update', label: 'Update Git Core' });
        }
      }
      actions.push({ id: 'recover', label: 'Restore bundled Core' }, { id: 'logs', label: 'Open Desktop logs' });
      const answer = await dialog.showMessageBox({ type: 'info', title: 'Core source (Advanced)',
        message: `Core source: ${settings.source === 'git' ? 'Git' : 'Bundled with Desktop'}`,
        detail: `Git in this backend environment: ${status?.git_available ? 'Available' : 'Unavailable'}\n`
          + (status?.commit ? `Commit: ${status.commit}${status.dirty ? ' (local changes)' : ''}\n` : '')
          + (status ? `Workspace: ${status.workspace}\n` : '') + issue
          + '\nGit updates are manual. Install Git in the selected Windows, macOS or WSL environment to enable them.',
        buttons: actions.map(item => item.label), defaultId: 0, cancelId: 0, noLink: true });
      const action = actions[answer.response]?.id;
      if (action === 'logs') await openLogs();
      else if (ACTIONS.includes(action)) return await confirm(action);
    } finally { managing = false; }
  }
  return { showManager,
    async prepare() {
      const settings = await store.consume();
      lastAction = settings.pending;
      if (settings.pending) {
        const command = await manage(settings.pending);
        await store.select(settings.pending === 'recover' ? 'bundled' : 'git');
        lastAction = null;
        return command;
      }
      return settings.source === 'git' ? manage('check') : prepareBundled();
    },
    async failure(error) {
      while (true) {
        const answer = await dialog.showMessageBox({ type: 'error', title: 'StandTerm Core is unavailable',
          message: error.message, detail: 'The Desktop shell can retry or restore its installed Core. Existing files are retained.',
          buttons: ['Quit', 'Retry', 'Restore bundled Core', 'Core source (Advanced)', 'Open Desktop logs'],
          defaultId: 0, cancelId: 0, noLink: true });
        if (answer.response === 4) { await openLogs(); continue; }
        if (answer.response === 3) { if (await showManager()) return 'restart'; continue; }
        if (answer.response === 2) { if (await confirm('recover')) return 'restart'; continue; }
        if (answer.response === 1) restart(lastAction);
        return answer.response === 0 ? 'quit' : 'restart';
      }
    },
  };
}

module.exports = { sourceStore, installedStore, coreController };
