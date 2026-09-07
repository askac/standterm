'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createDiagnostics, diagnosticsMenu, agentConnectionInfo, openDeveloperTools, MAX_LOG_BYTES } = require('../diagnostics.cjs');
const { statusHtml } = require('../diagnostics-window.cjs');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-diagnostics-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, logger: createDiagnostics(directory, { mode: 'wsl', version: '0.3.3' }) };
}

test('diagnostics persist only bounded structured metadata, never payloads or credentials', t => {
  const { logger } = fixture(t);
  logger.write('backend_verify_retry', { port: 51761, attempt: 2, elapsedMs: 2000,
    code: 'ECONNREFUSED', token: 'secret-value', password: 'secret-value',
    message: 'secret-value', stderr: 'secret-value', origin: 'http://127.0.0.1/?token=secret-value' });
  logger.write('unknown-event-secret-value', { port: 5000 });
  logger.write('backend_exit', { code: 'secret-value', exitCode: null, port: Infinity, expected: true });
  const text = fs.readFileSync(logger.file, 'utf8');
  assert.ok(!text.includes('secret-value'));
  const [retry, exited] = text.trim().split('\n').map(JSON.parse);
  assert.equal(retry.port, 51761);
  assert.equal(retry.code, 'ECONNREFUSED');
  assert.equal(retry.mode, 'wsl');
  assert.equal(retry.attempt, 2);
  assert.equal(exited.expected, true);
  assert.equal(Object.hasOwn(exited, 'exitCode'), false);
  assert.equal(Object.hasOwn(exited, 'port'), false);
});

test('diagnostic rotation is bounded and write failure does not block startup', t => {
  const { directory, logger } = fixture(t);
  fs.writeFileSync(logger.file, 'x'.repeat(MAX_LOG_BYTES));
  logger.write('startup');
  const previous = path.join(directory, 'startup.previous.jsonl');
  assert.equal(fs.statSync(previous).size, MAX_LOG_BYTES);
  fs.writeFileSync(logger.file, 'y'.repeat(MAX_LOG_BYTES));
  logger.write('startup');
  assert.equal(fs.readFileSync(previous, 'utf8')[0], 'y');
  assert.ok(fs.statSync(logger.file).size < 1024);
  const blocked = createDiagnostics(logger.file, { mode: 'windows', version: '0.3.3' });
  assert.doesNotThrow(() => blocked.write('startup'));
  assert.equal(blocked.available, false);
});

test('diagnostics menu shows the actual owned endpoint without an access URL', t => {
  const { logger } = fixture(t);
  const options = { origin: 'http://127.0.0.1:64487', mode: 'wsl', version: '0.3.3', logger,
    instanceId: 'test-instance', openLogs: () => {}, openTools: () => {} };
  const menu = diagnosticsMenu(options);
  const endpoint = menu.submenu.find(item => item.id === 'diagnostics-origin');
  assert.equal(endpoint.label, 'URL: http://127.0.0.1:64487');
  assert.equal(endpoint.enabled, false);
  assert.equal(menu.submenu.find(item => item.id === 'diagnostics-core-version').label, 'Core version: Unknown (older Core)');
  assert.equal(diagnosticsMenu({ ...options, coreVersion: '2.11.0-dev' }).submenu
    .find(item => item.id === 'diagnostics-core-version').label, 'Core version: 2.11.0-dev');
  for (const origin of ['http://127.0.0.1:64487/?token=secret', 'https://example.com', 'http://secret@127.0.0.1:64487']) {
    assert.throws(() => diagnosticsMenu({ ...options, origin }));
  }
});

test('connection copy uses the exact instance and never includes credentials or page payloads', t => {
  const { logger } = fixture(t);
  const options = { origin: 'http://127.0.0.1:64487', mode: 'wsl', instanceId: 'desktop-test-1',
    token: 'secret', session_token: 'secret', launcher_token: 'secret', terminal: 'private content' };
  const info = agentConnectionInfo(options);
  assert.deepEqual(info, { schema: 'standterm_agent_connection', schema_version: 1,
    base_url: options.origin, agentinfo_url: options.origin + '/agentinfo',
    instance_id: 'desktop-test-1', backend_mode: 'wsl' });
  const copied = [];
  const menu = diagnosticsMenu({ ...options, version: '0.4.0', logger, copyText: value => copied.push(value) });
  assert.deepEqual(copied, []);
  menu.submenu.find(item => item.id === 'diagnostics-copy-origin').click();
  menu.submenu.find(item => item.id === 'diagnostics-copy-agent').click();
  assert.equal(copied[0], options.origin);
  assert.deepEqual(JSON.parse(copied[1]), info);
  for (const invalid of [{ origin: options.origin + '/?token=secret' }, { instanceId: '' },
    { instanceId: 'id\nsecret' }, { mode: 'unknown' }]) {
    assert.throws(() => agentConnectionInfo({ ...options, ...invalid }));
  }
});

test('Developer Tools opens only after explicit consent and never for destroyed contents', async () => {
  const calls = [];
  const contents = { isDestroyed: () => false, openDevTools: options => calls.push(options) };
  assert.equal(await openDeveloperTools(contents, async () => false), false);
  assert.equal(await openDeveloperTools(contents, async () => 'true'), false);
  assert.equal(calls.length, 0);
  assert.equal(await openDeveloperTools(contents, async () => true), true);
  assert.deepEqual(calls, [{ mode: 'detach' }]);
  contents.isDestroyed = () => true;
  assert.equal(await openDeveloperTools(contents, async () => true), false);
  assert.equal(calls.length, 1);
});

test('diagnostics page escapes display data and snapshots remain bounded and detached', t => {
  const { logger } = fixture(t);
  for (let n = 0; n < 220; n++) logger.write('startup', { port: n, token: 'secret' });
  const snapshot = logger.snapshot();
  assert.equal(snapshot.length, 200);
  snapshot[0].port = 'modified';
  assert.equal(logger.snapshot()[0].port, 20);
  const html = statusHtml([['Injected', '<img src="https://example.com" onerror="alert(1)">']], logger.snapshot());
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('secret'));
  assert.ok(html.includes("default-src 'none'"));
  assert.ok(html.includes('&lt;img'));
});
