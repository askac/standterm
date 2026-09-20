'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

function fixture(confirmStop, restartRequest = { action: 'recover' }) {
  const calls = { confirm: [], cancelSetup: 0, stopBackend: 0, resetBrowser: 0,
    flushBrowser: 0, queue: [], relaunch: 0, exit: [], errors: [], destroyTray: 0 };
  const forbidden = name => () => { throw new Error(`Unexpected fixture operation: ${name}`); };
  const app = new EventEmitter();
  Object.assign(app, {
    isPackaged: false,
    commandLine: { getSwitchValue: () => '', appendSwitch: () => {} },
    setName: () => {}, setAppUserModelId: () => {}, enableSandbox: () => {},
    getPath: () => path.join(__dirname, 'unused-profile'), getVersion: () => 'fixture',
    requestSingleInstanceLock: () => true,
    whenReady: () => new Promise(() => {}),
    quit: forbidden('app.quit'),
    relaunch: () => { calls.relaunch++; },
    exit: code => { calls.exit.push(code); },
  });
  const processStub = new EventEmitter();
  Object.assign(processStub, { platform: 'win32', argv: ['node', 'main.cjs'], env: {} });
  const electron = { app,
    BrowserWindow: forbidden('BrowserWindow'), WebContentsView: forbidden('WebContentsView'),
    dialog: { showErrorBox: (title, message) => { calls.errors.push({ title, message }); } },
  };
  const modules = {
    './installer.cjs': { installerRequest: () => null },
    './desktop-mode.cjs': { APP_ID: 'fixture', desktopMode: () => 'windows' },
    './language.cjs': { createLanguage: () => ({ t: key => key, locale: 'en' }) },
    './diagnostics.cjs': { createDiagnostics: () => ({ write: () => {} }) },
    './setup.cjs': { confirmSetupQuit: async () => true,
      cancelSetup: () => { calls.cancelSetup++; } },
    './backend-stop.cjs': { stopOwnedBackend: async () => { calls.stopBackend++; } },
    './browser-session.cjs': { resetBrowserAuthentication: async () => { calls.resetBrowser++; } },
  };
  const context = vm.createContext({
    process: processStub, console, Buffer, URL, setTimeout, clearTimeout,
    __dirname: path.resolve(__dirname, '..'),
    require: name => {
      if (name === 'electron') return electron;
      if (Object.hasOwn(modules, name)) return modules[name];
      if (name === 'node:child_process') return { spawn: forbidden('spawn') };
      if (name === 'node:http') return { request: forbidden('http.request') };
      if (name === 'node:fs') return new Proxy({}, { get: (_target, method) => forbidden(`fs.${String(method)}`) });
      if (name.startsWith('./')) return {};
      return require(name);
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
  vm.runInContext('(function () {\n' + source + `
    globalThis.injectShutdownState = state => {
      capture = state.capture;
      child = state.child;
      desktopSession = state.desktopSession;
      coreStore = state.coreStore;
      tray = state.tray;
      restartRequest = state.restartRequest;
    };
    globalThis.readShutdownState = () => ({ quitting, stopped, restartRequest });
  })()`, context);
  context.injectShutdownState({
    capture: { active: true, confirmStop: async action => {
      calls.confirm.push(action);
      return confirmStop();
    } },
    child: {},
    desktopSession: { flushStorageData: () => { calls.flushBrowser++; } },
    coreStore: { queue: async action => { calls.queue.push(action); } },
    tray: { destroy: () => { calls.destroyTray++; } },
    restartRequest,
  });
  let prevented = 0;
  return { calls, state: () => context.readShutdownState(),
    quit: () => app.emit('before-quit', { preventDefault: () => { prevented++; } }),
    prevented: () => prevented,
    settle: () => new Promise(setImmediate),
  };
}

function assertKeptRunning(f) {
  assert.equal(f.calls.cancelSetup, 0);
  assert.equal(f.calls.stopBackend, 0);
  assert.equal(f.calls.resetBrowser, 0);
  assert.equal(f.calls.flushBrowser, 0);
  assert.deepEqual(f.calls.queue, []);
  assert.equal(f.calls.relaunch, 0);
  assert.deepEqual(f.calls.exit, []);
  assert.equal(f.calls.destroyTray, 0);
  assert.equal(f.state().quitting, false);
  assert.equal(f.state().stopped, false);
  assert.equal(f.state().restartRequest, null);
}

test('cancelled recording confirmation keeps Desktop running and clears restart intent', async () => {
  const f = fixture(async () => false);
  f.quit();
  await f.settle();
  assert.equal(f.prevented(), 1);
  assert.equal(f.calls.confirm.length, 1);
  assertKeptRunning(f);
  f.quit();
  await f.settle();
  assert.equal(f.calls.confirm.length, 2);
  assertKeptRunning(f);
});

test('rejected recording confirmation keeps Desktop running and clears restart intent', async () => {
  const f = fixture(async () => { throw new Error('Synthetic confirmation failure'); });
  f.quit();
  await f.settle();
  assert.equal(f.prevented(), 1);
  assert.equal(f.calls.confirm.length, 1);
  assertKeptRunning(f);
  f.quit();
  await f.settle();
  assert.equal(f.calls.confirm.length, 2);
  assertKeptRunning(f);
});

for (const restartRequest of [null, { action: 'recover' }]) {
  test(`confirmed recording shutdown ${restartRequest ? 'relaunches' : 'exits'} once despite duplicate quit events`, async () => {
    let resolveConfirmation;
    const confirmation = new Promise(resolve => { resolveConfirmation = resolve; });
    const f = fixture(() => confirmation, restartRequest);
    f.quit();
    await f.settle();
    f.quit();
    assert.equal(f.state().quitting, true);
    assert.equal(f.calls.confirm.length, 1);
    assert.equal(f.calls.stopBackend, 0);
    assert.deepEqual(f.calls.exit, []);
    resolveConfirmation(true);
    await f.settle();
    assert.equal(f.calls.cancelSetup, 1);
    assert.equal(f.calls.stopBackend, 1);
    assert.equal(f.calls.resetBrowser, 1);
    assert.equal(f.calls.flushBrowser, 1);
    assert.deepEqual(f.calls.queue, restartRequest ? ['recover'] : []);
    assert.equal(f.calls.relaunch, restartRequest ? 1 : 0);
    assert.deepEqual(f.calls.exit, [0]);
    assert.equal(f.calls.destroyTray, 1);
    assert.equal(f.state().stopped, true);
    assert.deepEqual(f.calls.errors, []);
    f.quit();
    await f.settle();
    assert.equal(f.calls.confirm.length, 1);
    assert.equal(f.calls.stopBackend, 1);
    assert.deepEqual(f.calls.exit, [0]);
  });
}
