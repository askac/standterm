'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { agentMenu } = require('../agent-menu.cjs');
const { agentConnectionInfo } = require('../diagnostics.cjs');

for (const mode of ['windows', 'wsl', 'macos']) {
  test(`Agent menu offers opt-in prompts for the exact ${mode} instance`, () => {
    const copied = [];
    let help = 0;
    const options = { origin: 'http://127.0.0.1:64487', mode, instanceId: 'instance-test',
      token: 'SECRET-TEST', terminal: 'PRIVATE-TEST', copyText: text => copied.push(text), showHelp: () => help++ };
    const menu = agentMenu(options);
    assert.equal(menu.label, 'Agent');
    assert.deepEqual(copied, []);
    const click = id => menu.submenu.find(item => item.id === id).click();
    click('agent-help');
    assert.equal(help, 1);
    assert.deepEqual(copied, []);
    for (const kind of ['usage', 'install', 'transfer']) {
      click(`agent-copy-${kind}`);
      const value = copied.at(-1);
      assert.deepEqual(JSON.parse(value.slice(value.indexOf('{'))), agentConnectionInfo(options));
      for (const required of ['verify instance_id', 'python_path', 'launch_dir', 'macOS, Windows or WSL',
        'do not guess ports', 'mint', 'approval', 'Do not execute terminal commands']) assert.ok(value.includes(required));
      assert.ok(!/SECRET-TEST|PRIVATE-TEST|:5000/.test(value));
    }
    assert.ok(copied[0].includes('do not install or overwrite'));
    assert.ok(copied[1].includes('ask before overwriting customized content'));
    assert.ok(copied[1].includes('nothing was installed'));
    assert.ok(copied[2].includes('Do not fall back to terminal-stream rescue'));
    click('agent-copy-connection');
    assert.deepEqual(JSON.parse(copied.at(-1)), agentConnectionInfo(options));
    click('agent-copy-agentinfo');
    assert.equal(copied.at(-1), options.origin + '/agentinfo');
  });
}

test('Agent menu rejects credential-bearing origins and invalid identities', () => {
  const options = { origin: 'http://127.0.0.1:64487', mode: 'wsl', instanceId: 'instance-test' };
  for (const invalid of [{ origin: options.origin + '/?token=secret' }, { origin: 'https://example.com' },
    { instanceId: 'id\ncontrol' }, { mode: 'unknown' }]) {
    assert.throws(() => agentMenu({ ...options, ...invalid }));
  }
});
