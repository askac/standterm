'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { agentMenu } = require('../agent-menu.cjs');

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
