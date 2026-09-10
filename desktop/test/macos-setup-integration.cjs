'use strict';

// Run with checkout Electron, a prepared project venv and a staged bundle.
// Real setup/progress UI and Python children; only user choices and runtime paths
// are redirected, so no installed app, account runtime or live profile is used.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const syncFs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawn } = require('node:child_process');
const electron = require('electron');
const { app } = electron;
const directory = syncFs.mkdtempSync(path.join(__dirname, '..', 'dist', 'macos-setup-test-'));
const profile = path.join(directory, 'profile');
syncFs.mkdirSync(profile);
app.enableSandbox();
app.setPath('userData', profile);
app.on('window-all-closed', () => {});

async function main() {
  assert.equal(process.platform, 'darwin');
  const [python, stage] = process.argv.slice(2);
  if (!python || !stage) throw new Error('Pass the prepared venv Python and macOS stage directory.');
  const runtime = path.join(directory, 'runtime');
  // Pin the project venv; tests never execute project code with system Python.
  await fs.writeFile(path.join(profile, 'launcher.json'), JSON.stringify({ version: 1, python }));
  await app.whenReady();
  let preparations = 0;
  let confirmations = 0;
  const setupElectron = { ...electron, dialog: { ...electron.dialog,
    showMessageBox: async options => {
      assert.equal(options.title, 'Prepare StandTerm Core');
      assert.match(options.message, /macOS/);
      assert.equal(options.defaultId, 0);
      confirmations++;
      return { response: 1 };
    },
  } };
  const setupSpawn = (executable, args, options) => {
    assert.equal(executable, python);
    const bootstrap = args.some(value => value === path.join(stage, 'bundle', 'bootstrap.py'));
    if (args.includes('--prepare')) preparations++;
    return spawn(executable, bootstrap ? [...args, '--test-root', runtime] : args, options);
  };
  const context = vm.createContext({ module: { exports: {} }, __dirname: path.resolve(__dirname, '..'),
    require: name => name === 'electron' ? setupElectron : name === 'node:child_process' ? { spawn: setupSpawn }
      : name.startsWith('./') ? require(path.join(__dirname, '..', name)) : require(name),
    process: { resourcesPath: stage, arch: process.arch, env: process.env }, Buffer, setTimeout, clearTimeout });
  vm.runInContext(await fs.readFile(path.join(__dirname, '..', 'setup.cjs'), 'utf8'), context);
  const command = await context.module.exports.preparePackagedBackend('macos');
  assert.equal(command.executable, path.join(runtime, 'tools', '.venv_macos', 'bin', 'python'));
  assert.equal(command.cwd, runtime);
  assert.equal(preparations, 1);
  assert.equal(confirmations, 1);
  const reused = await context.module.exports.preparePackagedBackend('macos');
  assert.equal(reused.executable, command.executable);
  assert.equal(preparations, 1, 'ready reuse must not run pip again');
  assert.equal(confirmations, 1);
  assert.equal(electron.BrowserWindow.getAllWindows().length, 0);
  console.log(`macOS setup integration passed: real progress window, dependency install and reuse; ${runtime}`);
  app.exit(0);
}

main().catch(error => { console.error(error.stack); app.exit(1); });
