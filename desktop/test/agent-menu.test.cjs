'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { agentMenu } = require('../agent-menu.cjs');
const { create } = require('../i18n.js');

test('Agent menu delegates terminal authority to Core and offers guidance', () => {
  const calls = [];
  const uiItems = [
    { id: 'ui-agentPanel', click: () => calls.push('panel') },
    { id: 'ui-pauseAgent', click: () => calls.push('pause') },
    { type: 'separator' },
  ];
  const menu = agentMenu({ uiItems, showHelp: () => calls.push('help') });
  assert.equal(menu.label, 'Agent');
  assert.deepEqual(calls, []);
  assert.deepEqual(menu.submenu.map(item => item.id).filter(Boolean),
    ['ui-agentPanel', 'ui-pauseAgent', 'agent-help']);
  for (const item of menu.submenu) if (item.click) item.click();
  assert.deepEqual(calls, ['panel', 'pause', 'help']);
});

test('translated Agent menu labels preserve command identities and help callback', () => {
  let called = 0;
  const { t } = create('zh-TW');
  const menu = agentMenu({ t, showHelp: () => called++ });
  assert.equal(menu.id, 'agent-menu');
  assert.equal(menu.submenu[0].id, 'agent-help');
  assert.equal(menu.submenu[0].label, t('desktop.agent.getting_started'));
  menu.submenu[0].click();
  assert.equal(called, 1);
});
