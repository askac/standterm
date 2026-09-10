'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { CaptureSettings, captureName } = require('../capture-settings.cjs');

test('capture folders persist independently of backend origin and may diverge', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-capture-settings-'));
  const file = path.join(root, 'capture.json');
  const prefs = new CaptureSettings(file);
  assert.equal(prefs.get('png'), null);
  prefs.set('png', path.join(root, 'screenshots'));
  assert.equal(prefs.get('webm'), prefs.get('png'));
  prefs.set('webm', path.join(root, 'recordings'));
  assert.equal(new CaptureSettings(file).get('png'), path.join(root, 'screenshots'));
  assert.equal(new CaptureSettings(file).get('webm'), path.join(root, 'recordings'));
  assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(file, 'utf8'))).sort(), ['png', 'version', 'webm']);
  assert.throws(() => prefs.set('png', 'relative'), /absolute/);
  assert.throws(() => prefs.set('unknown', root), /format/);
});

test('invalid settings require selection and generated names avoid collisions', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-capture-invalid-'));
  const file = path.join(root, 'capture.json');
  for (const content of ['invalid', '{"version":2}', '{"version":1,"png":"relative","webm":42}']) {
    fs.writeFileSync(file, content);
    assert.equal(new CaptureSettings(file).get('png'), null);
    assert.equal(new CaptureSettings(file).get('webm'), null);
  }
  const now = new Date('2026-09-10T00:00:00.000Z');
  const name = captureName('png', now);
  assert.match(name, /^StandTerm-2026-09-10T00-00-00-000Z-[a-f0-9]{8}\.png$/);
  assert.notEqual(name, captureName('png', now));
  assert.throws(() => captureName('../png'), /format/);
});
