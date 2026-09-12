'use strict';

// Compare a staged Core payload with an independently selected public release.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { coreFiles, validateCoreFiles } = require('../core-files.cjs');
const { stagedReleaseIdentity } = require('../release-identity.cjs');
const [stageArg, coreRef] = process.argv.slice(2);
if (!stageArg || !coreRef) throw new Error('Pass the build stage and expected Core release tag.');
const root = path.resolve(__dirname, '../..');
const git = args => execFileSync('git', args, { cwd: root, maxBuffer: 16 * 1024 * 1024 });
const commit = git(['rev-parse', '--verify', '--end-of-options', `${coreRef}^{commit}`]).toString().trim();
const selected = coreFiles(git(['ls-tree', '-r', '--name-only', '-z', commit]).toString().split('\0').filter(Boolean));
const stage = path.resolve(stageArg);
const directory = path.join(stage, 'bundle', 'core');
const manifest = JSON.parse(fs.readFileSync(path.join(stage, 'bundle', 'manifest.json'), 'utf8'));
assert.deepEqual(Object.keys(manifest.files).sort(), selected);
validateCoreFiles(directory, selected);
const hash = data => createHash('sha256').update(data).digest('hex');
for (const file of selected) {
  const expected = git(['show', `${commit}:${file}`]);
  assert.deepEqual(fs.readFileSync(path.join(directory, file)), expected, file);
  assert.equal(manifest.files[file], hash(expected), file);
}
assert.equal(manifest.id, hash(JSON.stringify(manifest.files)));
const identity = stagedReleaseIdentity(stage);
assert.equal(coreRef, `v${identity.coreVersion}`);
console.log(JSON.stringify({ coreRef, coreCommit: commit, coreFiles: selected.length, ...identity }, null, 2));
