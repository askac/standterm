'use strict';

// Run with native Node and an Electron executable; each renderer phase runs in
// a different Electron process. Only disposable profiles and synthetic keys.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const net = require('node:net');
const { randomUUID } = require('node:crypto');
const { backendCommand } = require('../policy.cjs');
const { browserSessionOptions, resetBrowserAuthentication } = require('../browser-session.cjs');
const root = path.resolve(__dirname, '../..');

async function childPhase() {
  console.error('Storage child: starting.');
  const { app, BrowserWindow, session } = require('electron');
  app.enableSandbox();
  app.setPath('userData', process.argv.at(-2));
  const input = new Promise((resolve, reject) => {
    let text = '';
    const socket = net.connect(process.argv.at(-1));
    socket.on('error', reject);
    socket.on('data', chunk => {
      text += chunk;
      if (text.includes('\n')) {
        socket.end();
        resolve(JSON.parse(text.split('\n')[0]));
      }
    });
  });
  await app.whenReady();
  console.error('Storage child: app ready.');
  const { handoff, mode, phase, expected } = await input;
  console.error('Storage child: private input received.');
  const options = browserSessionOptions(mode);
  const ses = session.fromPartition(options.partition, options.options);
  if (phase === 'read') assert.ok((await ses.cookies.get({ name: 'storage-test-old' })).length);
  await resetBrowserAuthentication(ses);
  assert.equal((await ses.cookies.get({})).length, 0);
  await ses.cookies.set({ url: handoff.origin, name: handoff.cookie_name, value: handoff.session_token,
    httpOnly: true, sameSite: 'strict', path: '/' });
  const win = new BrowserWindow({ show: false, webPreferences: {
    session: ses, sandbox: true, contextIsolation: true, nodeIntegration: false,
  } });
  await win.loadURL(handoff.origin + '/?debug=1');
  console.error('Storage child: Core loaded.');
  const run = code => win.webContents.executeJavaScript(code);
  const deadline = Date.now() + 20000;
  while (!await run('!!window.terminalTest')) {
    if (Date.now() >= deadline) throw new Error('Core UI readiness timed out');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (phase === 'write') {
    console.error('Storage child: writing synthetic profile and key.');
    await run(`localStorage.setItem('terminal.pref.v1', JSON.stringify({ colorScheme: 'oneHalfLight' }));
      window.terminalTest.setSshSessionState({ profiles: [{ id: 'storage-test', name: 'Storage test',
        host: 'example.invalid', username: 'fixture', port: '22' }], history: [] })`);
    await run("window.terminalTest.createBrowserSshKeyForProfileForTest('storage-test')");
  }
  const profiles = await run('window.terminalTest.getSshSessionState()');
  if (phase === 'empty') {
    assert.equal(profiles.profiles.length, 0);
    assert.equal(await run("localStorage.getItem('terminal.pref.v1')"), null);
  } else {
    assert.equal(profiles.profiles[0].id, 'storage-test');
    assert.equal(await run("JSON.parse(localStorage.getItem('terminal.pref.v1')).colorScheme"), 'oneHalfLight');
  }
  const identity = await run(`loadBrowserIdentity().then(async value => {
    const data = new TextEncoder().encode('restart-proof');
    const algorithm = { name: 'ECDSA', hash: 'SHA-256' };
    const signature = await crypto.subtle.sign(algorithm, value.privateKey, data);
    return { browserId: value.browserId, extractable: value.privateKey.extractable,
      verified: await crypto.subtle.verify(algorithm, value.publicKey, signature, data) };
  })`);
  assert.equal(identity.extractable, false);
  assert.equal(identity.verified, true);
  let key;
  if (phase !== 'empty') {
    key = await run("window.terminalTest.getBrowserSshKeyMetadataForTest('storage-test')");
    assert.equal(key.privateKeyExtractable, false);
    assert.equal(await run("window.terminalTest.signBrowserSshChallengeForTest('storage-test', btoa('restart-proof')).then(value => atob(value).length)"), 64);
    assert.equal(await run(`loadSshKeyRecord(${JSON.stringify(key.keyId)}).then(record => crypto.subtle.exportKey('pkcs8', record.privateKey)
        .then(() => false, () => true))`), true);
  }
  if (phase === 'read') {
    assert.equal(identity.browserId, expected.browserId);
    assert.equal(key.fingerprint, expected.fingerprint);
  } else if (phase === 'empty') assert.notEqual(identity.browserId, expected.browserId);
  if (phase === 'write') {
    // Leave a persisted stale cookie to exercise next-start crash recovery.
    await ses.cookies.set({ url: handoff.origin, name: 'storage-test-old', value: 'synthetic-stale-cookie',
      expirationDate: Date.now() / 1000 + 3600 });
    await ses.cookies.flushStore();
  } else await resetBrowserAuthentication(ses);
  ses.flushStorageData();
  process.stdout.write(JSON.stringify({ type: 'storage_result', browserId: identity.browserId,
    fingerprint: key?.fingerprint }) + '\n');
  win.destroy();
  app.exit(0);
}

async function parent() {
  const electron = process.argv[2];
  if (!electron) throw new Error('Pass the Electron executable. Set the platform test venv environment first.');
  const directory = fs.mkdtempSync(path.join(root, 'desktop', 'dist', 'browser-storage-smoke-'));
  const profile = path.join(directory, 'profile');
  fs.mkdirSync(profile);
  let backend;
  let backendExit;
  async function stop() {
    if (!backend) return;
    backend.stdin.end();
    await backendExit;
    backend = null;
  }
  async function start(port = 0) {
    const command = backendCommand(root, process.platform, process.env);
    backend = spawn(command.executable, [...command.args, '--port', String(port)], {
      cwd: command.cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'],
      env: { ...process.env, STANDTERM_AGENT_RUNTIME_DIR: path.join(directory, 'agent'),
        STANDTERM_SESSION_RECOVERY_STORE: path.join(directory, 'recovery.json') },
    });
    backendExit = once(backend, 'exit');
    return new Promise((resolve, reject) => {
      let text = '';
      const timer = setTimeout(() => reject(new Error('Backend readiness timed out')), 60000);
      backend.once('exit', () => { clearTimeout(timer); reject(new Error('Backend exited before readiness')); });
      backend.stdout.on('data', chunk => {
        text += chunk;
        if (text.includes('\n')) {
          clearTimeout(timer);
          const frame = JSON.parse(text.split('\n')[0]);
          if (frame.type !== 'standterm_desktop_ready') reject(new Error('Backend did not bind'));
          else resolve(frame);
        }
      });
    });
  }
  async function phase(handoff, mode, action, expected) {
    // Electron's Windows GUI executable does not reliably read redirected stdin.
    // Use a one-shot private pipe, never a credential in argv or a disk file.
    const pipe = process.platform === 'win32' ? `\\\\.\\pipe\\standterm-storage-${randomUUID()}`
      : path.join(directory, 'handoff.sock');
    const server = net.createServer(socket => {
      server.close();
      socket.end(JSON.stringify({ handoff, mode, phase: action, expected }) + '\n');
    });
    server.listen(pipe);
    await once(server, 'listening');
    const proc = spawn(electron, [__filename, '--child', profile, pipe], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const closed = once(proc, 'close');
    let output = '';
    let errors = '';
    proc.stdout.on('data', chunk => { output += chunk; });
    proc.stderr.on('data', chunk => { errors += chunk; process.stderr.write(chunk); });
    const timer = setTimeout(() => proc.kill(), 60000);
    const [code] = await closed;
    clearTimeout(timer);
    if (server.listening) server.close();
    assert.equal(code, 0, errors.slice(-1500));
    const line = output.split(/\r?\n/).find(value => value.startsWith('{"type":"storage_result"'));
    assert.ok(line, 'Storage probe did not report completion');
    return JSON.parse(line);
  }
  try {
    let handoff = await start();
    const port = Number(new URL(handoff.origin).port);
    const expected = await phase(handoff, 'wsl', 'write');
    const oldToken = handoff.session_token;
    await stop();
    handoff = await start(port);
    assert.notEqual(handoff.session_token, oldToken);
    await phase(handoff, 'wsl', 'read', expected);
    await phase(handoff, 'windows', 'empty', expected);
    await stop();
    for (let attempt = 0; attempt < 5; attempt++) {
      handoff = await start();
      if (Number(new URL(handoff.origin).port) !== port) break;
      await stop();
    }
    assert.notEqual(Number(new URL(handoff.origin).port), port, 'Could not allocate a different test origin');
    await phase(handoff, 'wsl', 'empty', expected);
    console.log('Storage smoke passed: full process/backend restart, preferences, profiles, both CryptoKeys, stale-cookie reset and mode/origin isolation.');
  } finally { await stop(); }
}

if (process.versions.electron) childPhase().catch(error => { console.error(error.stack); require('electron').app.exit(1); });
else parent().catch(error => { console.error(error.stack); process.exitCode = 1; });
