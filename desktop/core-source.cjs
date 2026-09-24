'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { create } = require('./i18n.js');

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

function coreController({ store, prepareBundled, manage, dialog, openLogs, restart, t = create('en').t }) {
  let lastAction = null;
  let managing = false;
  async function confirm(action) {
    const result = await dialog.showMessageBox({ type: 'warning', title: t('desktop.core_source.change_title'),
      message: t(action === 'recover' ? 'desktop.core_source.confirm_recover' : 'desktop.core_source.confirm_git'),
      detail: [t('desktop.core_source.restart_notice'),
        t(action === 'recover' ? 'desktop.core_source.recovery_retention' : 'desktop.core_source.git_policy'),
        t('desktop.core_source.reauthorization')].join('\n\n'),
      buttons: [t('desktop.common.cancel'), t('desktop.core_source.restart_continue')], defaultId: 0, cancelId: 0, noLink: true });
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
      const actions = [{ id: 'cancel', label: t('desktop.core_source.close') }];
      if (status?.git_available) {
        actions.push({ id: settings.source === 'git' ? 'prepare' : 'enable',
          label: t(settings.source === 'git' ? 'desktop.core_source.prepare_git' : 'desktop.core_source.enable_git') });
        if (status.workspace === 'present') {
          if (settings.source !== 'git') actions.push({ id: 'prepare', label: t('desktop.core_source.prepare_git') });
          actions.push({ id: 'update', label: t('desktop.core_source.update_git') });
        }
      }
      actions.push({ id: 'recover', label: t('desktop.core_source.restore_bundled') }, { id: 'logs', label: t('desktop.core_source.open_logs') });
      const workspace = ['absent', 'present', 'unavailable', 'invalid'].includes(status?.workspace)
        ? t(`desktop.core_source.workspace_${status.workspace}`) : status?.workspace;
      const answer = await dialog.showMessageBox({ type: 'info', title: t('desktop.core_source.manager_title'),
        message: t('desktop.core_source.source', { source: settings.source === 'git' ? 'Git' : t('desktop.core_source.bundled') }),
        detail: t(status?.git_available ? 'desktop.core_source.git_available' : 'desktop.core_source.git_unavailable') + '\n'
          + (status?.commit ? t(status.dirty ? 'desktop.core_source.commit_dirty' : 'desktop.core_source.commit', { commit: status.commit }) + '\n' : '')
          + (status ? t('desktop.core_source.workspace', { workspace }) + '\n' : '') + issue
          + '\n' + t('desktop.core_source.manual_updates'),
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
        const answer = await dialog.showMessageBox({ type: 'error', title: t('desktop.core_source.unavailable_title'),
          message: error.message, detail: t('desktop.core_source.failure_choices'),
          buttons: [t('desktop.core_source.quit'), t('desktop.core_source.retry'), t('desktop.core_source.restore_bundled'),
            t('desktop.core_source.manager_title'), t('desktop.core_source.open_logs')],
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
