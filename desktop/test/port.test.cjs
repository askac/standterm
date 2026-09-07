'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { startWithPort, parsePortConflict, checkHostPort } = require('../port.cjs');

async function fixture(t, saved) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'standterm-port-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const settingsPath = path.join(root, 'port.json');
  if (saved !== undefined) await fs.writeFile(settingsPath, JSON.stringify(saved));
  const calls = [];
  const options = { settingsPath,
    launch: async port => { calls.push(port); return { origin: `http://127.0.0.1:${port || 45678}` }; },
    verify: async () => {}, stop: async () => {},
    confirm: async () => { throw new Error('Unexpected port prompt'); },
    notify: async () => {},
  };
  return { options, calls, read: async () => JSON.parse(await fs.readFile(settingsPath, 'utf8')) };
}

const conflict = port => Object.assign(new Error('Untrusted display text'), {
  code: 'PORT_IN_USE', port, suggestedPort: port + 1,
});

test('first desktop launch requests an automatic port, saves after verification and reuses it', async t => {
  const { options, calls, read } = await fixture(t);
  options.verify = async () => assert.rejects(fs.readFile(options.settingsPath), { code: 'ENOENT' });
  await startWithPort(options);
  assert.deepEqual(calls, [0]);
  assert.deepEqual(await read(), { version: 1, port: 45678 });
  options.verify = async () => {};
  await startWithPort(options);
  assert.deepEqual(calls, [0, 45678]);
});

test('occupied saved ports require approval, retry bind races, and remember only the final port', async t => {
  const { options, calls, read } = await fixture(t, { version: 1, port: 45678 });
  options.launch = async port => {
    calls.push(port);
    if (port < 45680) throw conflict(port);
    return { origin: `http://127.0.0.1:${port}` };
  };
  let stops = 0;
  options.stop = async () => { stops++; };
  options.confirm = async (port, candidate) => { assert.equal(candidate, port + 1); return 'remember'; };
  await startWithPort(options);
  assert.deepEqual(calls, [45678, 45679, 45680]);
  assert.equal(stops, 2);
  assert.equal((await read()).port, 45680);
});

test('cancel and use-once preserve saved settings; verification failure never saves', async t => {
  for (const choice of ['cancel', 'once', 'remember']) {
    const { options, read } = await fixture(t, { version: 1, port: 45678 });
    const launch = options.launch;
    options.launch = async port => { if (port === 45678) throw conflict(port); return launch(port); };
    options.confirm = async () => choice;
    if (choice === 'remember') options.verify = async () => { throw new Error('Instance verification failed'); };
    if (choice === 'once') await startWithPort(options);
    else await assert.rejects(startWithPort(options));
    assert.equal((await read()).port, 45678);
  }
});

test('invalid settings use automatic allocation; persistence failure reports without stopping startup', async t => {
  const { options, calls, read } = await fixture(t, { version: 1, port: true });
  const warnings = [];
  options.notify = async message => warnings.push(message);
  await startWithPort(options);
  assert.deepEqual(calls, [0]);
  assert.equal(warnings.length, 1);
  assert.equal((await read()).port, 45678);
  await fs.unlink(options.settingsPath);
  await fs.mkdir(options.settingsPath);
  await startWithPort(options);
  assert.equal(warnings.length, 3);
});

test('bind errors use validated structured codes, not display text', () => {
  const frame = { type: 'standterm_desktop_bind_error', version: 1, code: 'address_in_use', port: 45678, suggested_port: 55679 };
  assert.equal(parsePortConflict(JSON.stringify(frame), 45678).code, 'PORT_IN_USE');
  assert.equal(parsePortConflict(JSON.stringify({ ...frame, suggested_port: null }), 45678).suggestedPort, null);
  for (const patch of [{ version: 2 }, { code: 'permission_denied' }, { port: 5000 },
    { suggested_port: true }, { suggested_port: 0 }, { suggested_port: 45678 }, { suggested_port: 49151 }, { suggested_port: 65536 }]) {
    assert.throws(() => parsePortConflict(JSON.stringify({ ...frame, ...patch }), 45678));
  }
  assert.equal(parsePortConflict(JSON.stringify({ type: 'log', message: 'address_in_use' }), 45678), null);
  assert.equal(parsePortConflict(JSON.stringify({ ...frame, port: 65000, suggested_port: 50000 }), 65000).suggestedPort, 50000);
});

const hostConflict = port => Object.assign(new Error('Untrusted host display text'), {
  code: 'HOST_PORT_UNAVAILABLE', port, reason: 'host_permission_denied',
});

test('Windows reserved saved ports prompt only with a verified replacement; choices preserve authority', async t => {
  for (const choice of ['remember', 'once', 'cancel']) {
    const { options, calls, read } = await fixture(t, { version: 1, port: 51761 });
    const phases = [];
    let verified = false;
    let stops = 0;
    options.checkHost = async (port, { backendBound }) => {
      phases.push([port, backendBound]);
      if (port === 51761) throw hostConflict(port);
    };
    options.verify = async () => { verified = true; assert.equal((await read()).port, 51761); };
    options.stop = async () => { stops++; };
    options.confirm = async (port, candidate, reason) => {
      assert.equal(verified, true);
      assert.deepEqual([port, candidate, reason], [51761, 45678, 'host_permission_denied']);
      assert.equal((await read()).port, 51761);
      return choice;
    };
    if (choice === 'cancel') await assert.rejects(startWithPort(options), { code: 'SETUP_CANCELED' });
    else await startWithPort(options);
    assert.deepEqual(calls, [0], 'never launch the backend on the Windows-reserved saved port');
    assert.deepEqual(phases, [[51761, false], [45678, true]]);
    assert.equal((await read()).port, choice === 'remember' ? 45678 : 51761);
    assert.equal(stops, choice === 'cancel' ? 2 : 1);
  }
});

test('automatic WSL candidates rejected by Windows are stopped and retried without saving or prompting', async t => {
  const { options, read } = await fixture(t);
  let launches = 0;
  let stops = 0;
  options.launch = async port => {
    assert.equal(port, 0);
    return { origin: `http://127.0.0.1:${++launches === 1 ? 51761 : 55001}` };
  };
  options.checkHost = async (port, { backendBound }) => {
    assert.equal(backendBound, true);
    if (port === 51761) throw hostConflict(port);
  };
  options.stop = async () => { stops++; };
  options.verify = async frame => { assert.equal(frame.origin, 'http://127.0.0.1:55001'); };
  await startWithPort(options);
  assert.equal(stops, 1);
  assert.equal((await read()).port, 55001);
});

test('host retry budget, verification failure and prompt failure retain settings and clean up', async t => {
  for (const failure of ['budget', 'verification', 'prompt']) {
    const { options, read } = await fixture(t, { version: 1, port: 51761 });
    let checks = 0;
    let stops = 0;
    options.checkHost = async port => {
      checks++;
      if (failure === 'budget' || port === 51761) throw hostConflict(port);
    };
    options.stop = async () => { stops++; };
    if (failure === 'verification') options.verify = async () => { throw new Error('Instance mismatch'); };
    options.confirm = async () => { throw new Error('Prompt unavailable'); };
    await assert.rejects(startWithPort(options));
    assert.equal((await read()).port, 51761);
    assert.equal(checks, failure === 'budget' ? 20 : 2);
    assert.equal(stops, failure === 'budget' ? 20 : 2);
  }
});

test('host probing uses typed socket errors and allows an existing relay only after backend binding', async () => {
  for (const backendBound of [false, true]) {
    for (const code of ['EACCES', 'EADDRINUSE', 'EIO', null]) {
      let closed = false;
      const createServer = () => {
        const server = new EventEmitter();
        server.listen = (options, callback) => {
          assert.deepEqual(options, { host: '127.0.0.1', port: 51761, exclusive: true });
          if (code) server.emit('error', Object.assign(new Error('EACCES is display data'), { code }));
          else callback();
        };
        server.close = callback => { closed = true; callback(); };
        return server;
      };
      const result = checkHostPort(51761, { backendBound }, createServer);
      if (!code || (backendBound && code === 'EADDRINUSE')) await result;
      else await assert.rejects(result, { code: code === 'EIO' ? 'EIO' : 'HOST_PORT_UNAVAILABLE' });
      assert.equal(closed, code === null);
    }
  }
  await assert.rejects(checkHostPort(0));
});
