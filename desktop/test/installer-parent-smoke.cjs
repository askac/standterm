'use strict';

// Run with Windows Node, not Electron. Only an owned dummy installer is stopped.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { watchInstaller } = require('../installer.cjs');

async function main() {
  assert.equal(process.platform, 'win32');
  const parent = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'],
    { windowsHide: true, stdio: 'ignore' });
  let lost;
  const finished = new Promise(resolve => { lost = resolve; });
  const watcher = watchInstaller(parent.pid, lost);
  let timer;
  try {
    await watcher.started;
    assert.equal(watcher.alive(), true);
    parent.kill();
    await Promise.race([finished, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Installer loss was not observed')), 15000);
    })]);
    assert.equal(watcher.alive(), false);
    console.log('Owned Windows installer handle lifetime: passed');
  } finally { clearTimeout(timer); watcher.stop(); parent.kill(); }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
