'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

async function fixture({ consent = true, pythonMissing = false, ready = false, native = false,
  macos = false, machine = 'arm64', wrongVenv = false,
  coreError = null,
  holdPrepare = false, cleanupConfirm = false, closeDecision = async () => ({ response: 0 }) } = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'standterm-setup-test-'));
  await fs.mkdir(path.join(root, 'bundle'));
  const id = 'a'.repeat(64);
  await fs.writeFile(path.join(root, 'bundle', 'manifest.json'), JSON.stringify({ id }));
  const calls = [];
  let progressWindow;
  let preparedChild;
  let completePrepare;
  let signalPreparing;
  let closeDialogs = 0;
  const preparing = new Promise(resolve => { signalPreparing = resolve; });
  class Window extends EventEmitter {
    constructor(options) {
      super(); assert.equal(options.webPreferences.sandbox, true);
      assert.equal(options.webPreferences.nodeIntegration, false);
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
      this.webContents.executeJavaScript = async () => {};
      progressWindow = this;
    }
    async loadURL() {}
    isDestroyed() { return !!this.destroyed; }
    setTitle(title) { this.title = title; }
    destroy() { this.destroyed = true; this.emit('closed'); }
  }
  const electron = {
    app: { getPath: () => root }, BrowserWindow: Window,
    dialog: { showMessageBox: async (...args) => {
      const options = args.at(-1);
      if (options.title === 'Confirm environment cleanup') {
        assert.equal(options.defaultId, 0);
        assert.equal(options.cancelId, 0);
        assert.match(options.detail, /C:\\Runtime\\tools\\.venv_win/);
        return { response: cleanupConfirm ? 1 : 0 };
      }
      if (options.title === 'Cancel StandTerm setup?') {
        closeDialogs++;
        assert.equal(args[0], progressWindow);
        assert.equal(options.defaultId, 0);
        assert.equal(options.cancelId, 0);
        return closeDecision();
      }
      assert.equal(options.cancelId, options.defaultId);
      if (options.title === 'StandTerm Desktop: select WSL') return { response: 0 };
      if (options.title === 'StandTerm Desktop: Python required') return { response: 0 };
      assert.match(options.detail, /Requires Python 3.10\+/);
      return { response: consent ? 1 : 0 };
    } },
    session: { fromPartition: () => ({ setPermissionRequestHandler() {}, setPermissionCheckHandler() {},
      webRequest: { onBeforeRequest() {} } }) },
  };
  const spawn = (executable, args, options) => {
    assert.ok(macos ? executable.startsWith('/') && !executable.startsWith('/usr/bin/')
      : native ? ['where.exe', 'C:\\Python\\python.exe'].includes(executable) : executable === 'wsl.exe');
    assert.equal(options.shell, false);
    calls.push(args);
    const child = new EventEmitter();
    child.stdin = new EventEmitter(); child.stdin.end = () => { child.cancelRequested = true; };
    child.stdout = new EventEmitter(); child.stderr = { resume() {} }; child.kill = () => {};
    queueMicrotask(() => {
      let bytes;
      let code = 0;
      if (args.includes('--action')) {
        const action = args[args.indexOf('--action') + 1];
        if (coreError) { code = 1; bytes = Buffer.from(JSON.stringify({ type: 'error', code: coreError }) + '\n'); }
        else bytes = Buffer.from(JSON.stringify(action === 'status' ? { type: 'core_status', git_available: false, workspace: 'absent' }
          : { type: 'ready', source: action === 'recover' ? 'bundled' : 'git', bundle_id: id,
            root: native ? 'C:\\Runtime' : '/home/test/runtime',
            core_root: native ? 'C:\\Runtime\\repo' : '/home/test/runtime/repo', commit: 'b'.repeat(40), dirty: true,
            python: native ? 'C:\\Runtime\\tools\\.venv_win\\Scripts\\python.exe'
              : `/home/test/runtime/tools/${macos ? '.venv_macos' : '.venv_wsl'}/bin/python` }) + '\n');
      }
      else if (executable === 'where.exe') bytes = Buffer.from('C:\\Python\\python.exe\r\n');
      else if (args.includes('--inventory')) bytes = Buffer.from(JSON.stringify({ type: 'cleanup_inventory',
        results: [{ id, status: 'candidate', source: 'C:\\Runtime\\tools\\.venv_win' }] }) + '\n');
      else if (args.includes('--detach-idle-venvs')) bytes = Buffer.from(JSON.stringify({ type: 'cleanup_summary',
        results: [{ id, status: 'detached' }] }) + '\n');
      else if (args.includes('--list')) bytes = Buffer.from('Ubuntu Test\r\n', 'utf16le');
      else if (args.includes('wslpath')) bytes = Buffer.from('/mnt/c/Program Files/bundle\n');
      else if (pythonMissing) { bytes = Buffer.alloc(0); code = 127; }
      else if (args.includes('-c')) bytes = Buffer.from(JSON.stringify({ type: 'python_info',
        executable: macos ? '/opt/local/bin/python3' : 'C:\\Python\\python.exe', platform: macos ? 'darwin' : 'win32',
        machine, bits: 64, venv: true, version: [3, 12] }) + '\n');
      else if (args.includes('--prepare') || ready) bytes = Buffer.from(JSON.stringify({
        type: 'ready', bundle_id: id, root: native ? 'C:\\Runtime' : '/home/test/runtime',
        python: native ? 'C:\\Runtime\\tools\\.venv_win\\Scripts\\python.exe'
          : `/home/test/runtime/tools/${macos && !wrongVenv ? '.venv_macos' : '.venv_wsl'}/bin/python`,
      }) + '\n');
      else bytes = Buffer.from(JSON.stringify({ type: 'needs_setup', bundle_id: id }) + '\n');
      const complete = () => { child.stdout.emit('data', bytes); child.emit('close', code); };
      if (holdPrepare && args.includes('--prepare')) {
        preparedChild = child;
        completePrepare = complete;
        signalPreparing();
      } else complete();
    });
    return child;
  };
  const context = vm.createContext({ module: { exports: {} }, __dirname: path.join(__dirname, '..'),
    require: name => name === 'electron' ? electron : name === 'node:child_process' ? { spawn }
      : name === './macos-python.cjs' ? require('../macos-python.cjs') : require(name),
    process: { resourcesPath: root, arch: 'arm64', env: {} }, Buffer, setTimeout, clearTimeout });
  vm.runInContext(await fs.readFile(path.join(__dirname, '..', 'setup.cjs'), 'utf8'), context);
  return { run: options => context.module.exports.preparePackagedBackend(macos ? 'macos' : native ? 'windows' : 'wsl', options), root, calls,
    manage: action => context.module.exports.manageCore(macos ? 'macos' : native ? 'windows' : 'wsl', action),
    cleanup: () => context.module.exports.cleanupManagedVenvs(native ? 'windows' : 'wsl'),
    preparing, window: () => progressWindow, child: () => preparedChild, complete: () => completePrepare(),
    closeDialogs: () => closeDialogs, quit: () => context.module.exports.confirmSetupQuit() };
}

test('Core management probes the selected base environment without preparing bundled Core', async () => {
  for (const options of [{ native: true }, { macos: true }, {}]) {
    const f = await fixture(options);
    assert.equal((await f.manage('status')).git_available, false);
    assert.equal(f.calls.some(args => args.some(arg => arg.endsWith('/bootstrap.py') || arg.endsWith('\\bootstrap.py'))), false);
    assert.equal(f.calls.some(args => args.includes('--prepare')), false);
    const command = await f.manage('check');
    assert.equal(command.source, 'git');
    assert.match(command.coreSource, /local changes/);
    assert.equal(command.args.includes('--git-core'), true);
    const bridge = command.args[command.args.indexOf('-u') + 1];
    assert.match(bridge, /bundle[\\/]backend\.py$/);
    assert.equal(bridge.includes('repo'), false);
    assert.equal(command.args.includes('--distribution'), !options.native && !options.macos);
  }
});

test('Core recovery uses bundled identity and typed Git failures survive the adapter', async () => {
  const f = await fixture({ native: true });
  const restored = await f.manage('recover');
  assert.equal(restored.source, 'bundled');
  assert.equal(restored.args.includes('--git-core'), false);
  const failed = await fixture({ native: true, coreError: 'git_dirty' });
  await assert.rejects(failed.manage('update'), { code: 'git_dirty' });
});

test('setup requires explicit consent before installing and stores only non-secret settings', async () => {
  const f = await fixture();
  const command = await f.run();
  assert.equal(command.executable, 'wsl.exe');
  assert.ok(command.args.includes('Ubuntu Test'));
  assert.equal(f.calls.filter(args => args.includes('--prepare')).length, 1);
  const saved = JSON.parse(await fs.readFile(path.join(f.root, 'launcher.json'), 'utf8'));
  assert.deepEqual(saved, { version: 1, distro: 'Ubuntu Test' });
});

test('cancel or missing Python never starts installation or stores a ready preference', async () => {
  for (const options of [{ consent: false }, { pythonMissing: true }]) {
    const f = await fixture(options);
    await assert.rejects(f.run(), /canceled|Python 3.10/);
    assert.equal(f.calls.some(args => args.includes('--prepare')), false);
    await assert.rejects(fs.stat(path.join(f.root, 'launcher.json')), { code: 'ENOENT' });
  }
});

test('ready managed environments are reused without running pip again', async () => {
  const f = await fixture({ ready: true });
  await f.run();
  assert.equal(f.calls.some(args => args.includes('--prepare')), false);
});

test('macOS prepares a native runtime and reuses it with only Python launcher metadata', async () => {
  for (const ready of [false, true]) {
    const f = await fixture({ macos: true, ready });
    const command = await f.run();
    assert.equal(command.executable, '/home/test/runtime/tools/.venv_macos/bin/python');
    assert.deepEqual(Array.from(command.args), ['-u', '/home/test/runtime/desktop/backend.py']);
    assert.equal(f.calls.filter(args => args.includes('--prepare')).length, Number(!ready));
    assert.equal(f.calls.some(args => args.includes('--distribution')), false);
    assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root, 'launcher.json'), 'utf8')),
      { version: 1, python: '/opt/local/bin/python3' });
  }
});

test('macOS refuses missing or mismatched Python, cancellation and WSL runtime responses', async () => {
  for (const options of [{ pythonMissing: true }, { machine: 'x86_64' }, { consent: false },
    { ready: true, wrongVenv: true }]) {
    const f = await fixture({ macos: true, ...options });
    await assert.rejects(f.run(), /canceled|Invalid managed runtime/);
    assert.equal(f.calls.some(args => args.includes('--prepare')), false);
    await assert.rejects(fs.stat(path.join(f.root, 'launcher.json')), { code: 'ENOENT' });
  }
});

test('native Windows uses its own interpreter and never starts WSL', async () => {
  const f = await fixture({ native: true });
  const command = await f.run();
  assert.equal(command.executable, 'C:\\Runtime\\tools\\.venv_win\\Scripts\\python.exe');
  assert.equal(command.cwd, 'C:\\Runtime');
  assert.equal(f.calls.some(args => args.includes('--distribution')), false);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root, 'launcher.json'), 'utf8')),
    { version: 1, python: 'C:\\Python\\python.exe' });
});

test('missing native Python does not fall back to WSL or install dependencies', async () => {
  const f = await fixture({ native: true, pythonMissing: true });
  await assert.rejects(f.run(), /canceled/);
  assert.equal(f.calls.some(args => args.includes('--prepare') || args.includes('--distribution')), false);
});

test('installer preparation writes the fixed mode profile, not its maintenance userData', async () => {
  for (const native of [true, false]) {
    const f = await fixture({ native, ready: true });
    await f.run({ installer: true });
    const selected = path.join(f.root, 'StandTermDesktopEvaluation', native ? 'windows' : 'wsl', 'launcher.json');
    assert.equal(JSON.parse(await fs.readFile(selected, 'utf8')).version, 1);
    await assert.rejects(fs.stat(path.join(f.root, 'launcher.json')), { code: 'ENOENT' });
    const other = path.join(f.root, 'StandTermDesktopEvaluation', native ? 'wsl' : 'windows', 'launcher.json');
    await assert.rejects(fs.stat(other), { code: 'ENOENT' });
  }
});

test('cleanup confirmation defaults to keep; approval passes only bounded runtime IDs', async () => {
  for (const cleanupConfirm of [false, true]) {
    const f = await fixture({ native: true, ready: true, cleanupConfirm });
    await f.run({ installer: true });
    f.calls.length = 0;
    const result = await f.cleanup();
    assert.equal(f.calls.filter(args => args.includes('--inventory')).length, 1);
    const moves = f.calls.filter(args => args.includes('--detach-idle-venvs'));
    assert.equal(moves.length, Number(cleanupConfirm));
    if (cleanupConfirm) {
      assert.deepEqual(JSON.parse(moves[0].at(-1)), ['a'.repeat(64)]);
      assert.equal(result.results[0].status, 'detached');
    } else assert.equal(result.status, 'retained');
  }
});

test('closing preparation defaults to keeping it open and coalesces repeated clicks', async () => {
  let answer;
  const f = await fixture({ holdPrepare: true, closeDecision: () => new Promise(resolve => { answer = resolve; }) });
  const running = f.run();
  await f.preparing;
  let prevented = 0;
  f.window().emit('close', { preventDefault: () => { prevented++; } });
  f.window().emit('close', { preventDefault: () => { prevented++; } });
  assert.equal(prevented, 2);
  assert.equal(f.closeDialogs(), 1);
  answer({ response: 0 });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(f.child().cancelRequested, undefined);
  assert.equal(f.window().isDestroyed(), false);
  f.complete();
  await running;
});

test('confirmed quit waits for child cleanup and does not save a completed preference', async () => {
  const f = await fixture({ holdPrepare: true, closeDecision: async () => ({ response: 1 }) });
  const running = assert.rejects(f.run(), /canceled/);
  await f.preparing;
  let quitFinished = false;
  const quitting = f.quit().then(result => { quitFinished = true; return result; });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(f.child().cancelRequested, true);
  assert.equal(f.window().isDestroyed(), false);
  assert.equal(quitFinished, false);
  f.child().emit('close', 1);
  await running;
  assert.equal(await quitting, true);
  assert.equal(f.window().isDestroyed(), true);
  await assert.rejects(fs.stat(path.join(f.root, 'launcher.json')), { code: 'ENOENT' });
});

test('a cancellation answer arriving after successful setup is ignored', async () => {
  let answer;
  const f = await fixture({ holdPrepare: true, closeDecision: () => new Promise(resolve => { answer = resolve; }) });
  const running = f.run();
  await f.preparing;
  const quitting = f.quit();
  f.complete();
  await running;
  answer({ response: 1 });
  assert.equal(await quitting, false);
  assert.equal(f.child().cancelRequested, undefined);
  assert.ok(await fs.stat(path.join(f.root, 'launcher.json')));
});
