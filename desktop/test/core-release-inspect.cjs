'use strict';

// Compare a staged Core payload with its pinned source and public release version.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { coreFiles, validateCoreFiles } = require('../core-files.cjs');
const { stagedReleaseIdentity } = require('../release-identity.cjs');
const [stageArg, coreRef, sourceRef = coreRef] = process.argv.slice(2);
if (!stageArg || !coreRef) throw new Error('Pass the build stage, Core release tag and optional source revision.');
const root = path.resolve(__dirname, '../..');
const git = args => execFileSync('git', args, { cwd: root, maxBuffer: 16 * 1024 * 1024 });
const resolveCommit = ref => git(['rev-parse', '--verify', '--end-of-options', `${ref}^{commit}`]).toString().trim();
const releaseCommit = resolveCommit(coreRef);
const commit = sourceRef === coreRef ? releaseCommit : resolveCommit(sourceRef);
if (commit !== releaseCommit) git(['merge-base', '--is-ancestor', releaseCommit, commit]);
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
console.log(JSON.stringify({ coreRef, coreReleaseCommit: releaseCommit,
  coreCommit: commit, coreFiles: selected.length, ...identity }, null, 2));
