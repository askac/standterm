'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { coreFiles, validateCoreFiles, REQUIRED } = require('../core-files.cjs');

const root = path.resolve(__dirname, '../..');
const tracked = execFileSync('git', ['ls-files', '-z'], { cwd: root, encoding: 'utf8' }).split('\0').filter(Boolean);

test('Core bundle includes public launchers, skills, references and helpers', () => {
  const files = coreFiles(tracked);
  validateCoreFiles(root, files);
  for (const file of REQUIRED.filter(file => file.startsWith('docs/'))) assert.ok(files.includes(file), file);
  for (const file of ['.git/config', 'desktop/dist/private.json', 'tools/.venv_wsl/bin/python',
    'handover_20260909.md', 'AGENTS.md', 'tests/agent_backend_smoke.py']) assert.ok(!coreFiles([file]).length, file);
  assert.ok(!files.includes('scripts/base64d.sh'));
  assert.ok(!files.includes('scripts/base64d_probe.sh'));
  for (const required of REQUIRED) {
    assert.throws(() => validateCoreFiles(root, files.filter(file => file !== required)), /Missing required Core input/);
  }
});

test('Core selection excludes private documents even when tracked internally', () => {
  const privateFiles = ['docs/internal/handover_20260909.md', 'docs/internal/access.md',
    'docs/examples/private-agent/SKILL.md', 'README_INTERNAL.md', 'AGENTS.md'];
  for (const file of privateFiles) assert.ok(!coreFiles([...tracked, ...privateFiles]).includes(file), file);
});

test('Core validation rejects missing, non-file and incomplete skill payloads', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-core-files-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  for (const file of REQUIRED) {
    const target = path.join(directory, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, 'fixture');
  }
  validateCoreFiles(directory, REQUIRED);
  const skill = path.join(directory, 'docs/examples/standterm-external-agent-skill/SKILL.md');
  fs.writeFileSync(skill, '[Missing](references/missing.md)');
  assert.throws(() => validateCoreFiles(directory, REQUIRED), /Missing bundled skill reference/);
  fs.writeFileSync(skill, 'fixture');
  fs.unlinkSync(skill);
  assert.throws(() => validateCoreFiles(directory, REQUIRED), /ENOENT/);
  fs.mkdirSync(skill);
  assert.throws(() => validateCoreFiles(directory, REQUIRED), /Not a regular Core input/);
});
