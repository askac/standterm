'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { coreController } = require('../core-source.cjs');
const { create } = require('../i18n.js');

function fixture(locale, { source = 'bundled', status = { git_available: true, workspace: 'present', commit: 'a'.repeat(40), dirty: true }, responses = [], issue, pending = null, t = create(locale).t } = {}) {
  const dialogs = [];
  const restarts = [];
  const managed = [];
  let logs = 0;
  const controller = coreController({ t,
    store: { read: async () => ({ source }), consume: async () => ({ source, pending }), select: async () => assert.fail('Dialogs must not select a source') },
    manage: async action => { managed.push(action); if (issue) throw new Error(issue); return status; },
    restart: action => restarts.push(action), openLogs: async () => { logs++; },
    dialog: { showMessageBox: async options => {
      assert.equal(options.defaultId, 0);
      assert.equal(options.cancelId, 0);
      assert.equal(options.noLink, true);
      dialogs.push(options);
      assert.ok(responses.length, 'Unexpected additional dialog');
      return { response: responses.shift() };
    } },
  });
  return { controller, dialogs, restarts, managed, get logs() { return logs; } };
}

for (const locale of ['en', 'zh-TW']) {
  const { t } = create(locale);
  test(`${locale}: Core manager preserves every typed action across source and Git states`, async () => {
    const cases = [
      ['bundled', false, 'absent', ['cancel', 'recover', 'logs']],
      ['bundled', true, 'absent', ['cancel', 'enable', 'recover', 'logs']],
      ['bundled', true, 'present', ['cancel', 'enable', 'prepare', 'update', 'recover', 'logs']],
      ['git', true, 'present', ['cancel', 'prepare', 'update', 'recover', 'logs']],
    ];
    for (const [source, git_available, workspace, actions] of cases) {
      for (let index = 0; index < actions.length; index++) {
        const action = actions[index];
        const changesSource = !['cancel', 'logs'].includes(action);
        const f = fixture(locale, { source, status: { git_available, workspace }, responses: changesSource ? [index, 1] : [index] });
        await f.controller.showManager();
        assert.equal(f.dialogs[0].buttons.length, actions.length);
        assert.equal(f.dialogs[0].title, t('desktop.core_source.manager_title'));
        assert.equal(f.dialogs[0].message, t('desktop.core_source.source', { source: source === 'git' ? 'Git' : t('desktop.core_source.bundled') }));
        assert.deepEqual(f.managed, ['status']);
        assert.deepEqual(f.restarts, changesSource ? [action] : []);
        assert.equal(f.logs, action === 'logs' ? 1 : 0);
        if (changesSource) {
          const confirm = f.dialogs[1];
          assert.deepEqual(confirm.buttons, [t('desktop.common.cancel'), t('desktop.core_source.restart_continue')]);
          assert.equal(confirm.message, t(action === 'recover' ? 'desktop.core_source.confirm_recover' : 'desktop.core_source.confirm_git'));
          assert.ok(confirm.detail.includes(t('desktop.core_source.restart_notice')));
          assert.ok(confirm.detail.includes(t('desktop.core_source.reauthorization')));
          assert.ok(confirm.detail.includes(t(action === 'recover' ? 'desktop.core_source.recovery_retention' : 'desktop.core_source.git_policy')));
        }
      }
    }
    const canceled = fixture(locale, { responses: [1, 0] });
    assert.equal(await canceled.controller.showManager(), false);
    assert.deepEqual(canceled.restarts, []);
  });

  test(`${locale}: status renders known enums while preserving raw data and unknown values`, async () => {
    const commit = 'raw-{workspace}-<commit>';
    for (const workspace of ['absent', 'present', 'unavailable', 'invalid', 'future-{commit}']) {
      const f = fixture(locale, { status: { git_available: true, workspace, commit, dirty: true }, responses: [0] });
      await f.controller.showManager();
      const detail = f.dialogs[0].detail;
      const label = workspace.startsWith('future-') ? workspace : t(`desktop.core_source.workspace_${workspace}`);
      assert.ok(detail.includes(t('desktop.core_source.workspace', { workspace: label })));
      assert.ok(detail.includes(t('desktop.core_source.commit_dirty', { commit })));
      assert.ok(detail.includes(commit));
    }
    const issue = 'raw failure {source} <path>';
    const f = fixture(locale, { issue, responses: [0] });
    await f.controller.showManager();
    assert.ok(f.dialogs[0].detail.includes(issue));
    assert.ok(f.dialogs[0].detail.includes(t('desktop.core_source.git_unavailable')));
  });

  test(`${locale}: recovery and retry keep failure-dialog response semantics`, async () => {
    const issue = 'Original failure {commit} <raw>';
    const quit = fixture(locale, { responses: [0] });
    assert.equal(await quit.controller.failure(new Error(issue)), 'quit');
    assert.equal(quit.dialogs[0].message, issue);
    assert.deepEqual(quit.dialogs[0].buttons, ['quit', 'retry', 'restore_bundled', 'manager_title', 'open_logs'].map(key => t(`desktop.core_source.${key}`)));
    assert.equal(quit.dialogs[0].detail, t('desktop.core_source.failure_choices'));
    assert.deepEqual(quit.restarts, []);
    const retry = fixture(locale, { responses: [1], issue, pending: 'update' });
    await assert.rejects(retry.controller.prepare(), /Original failure/);
    assert.equal(await retry.controller.failure(new Error(issue)), 'restart');
    assert.deepEqual(retry.restarts, ['update']);
    const recovery = fixture(locale, { responses: [2, 1] });
    assert.equal(await recovery.controller.failure(new Error(issue)), 'restart');
    assert.deepEqual(recovery.restarts, ['recover']);
    const cancelRecovery = fixture(locale, { responses: [2, 0, 0] });
    assert.equal(await cancelRecovery.controller.failure(new Error(issue)), 'quit');
    assert.deepEqual(cancelRecovery.restarts, []);
    const logs = fixture(locale, { responses: [4, 0] });
    assert.equal(await logs.controller.failure(new Error(issue)), 'quit');
    assert.equal(logs.logs, 1);
    assert.equal(logs.dialogs.length, 2);
    const manager = fixture(locale, { responses: [3, 1, 1] });
    assert.equal(await manager.controller.failure(new Error(issue)), 'restart');
    assert.deepEqual(manager.restarts, ['enable']);
  });
}

test('duplicate translated labels cannot select a different Core action', async () => {
  const f = fixture('en', { t: () => 'same display label', responses: [3, 1] });
  await f.controller.showManager();
  assert.deepEqual(f.restarts, ['update']);
});
