'use strict';

// Read-only payload verification. Supply an extracted resources directory and
// the matching build stage; the inspection tool's module path is explicit.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { validateCoreFiles } = require('../core-files.cjs');
const { stagedReleaseIdentity } = require('../release-identity.cjs');
const [resourcesArg, stageArg, asarModule] = process.argv.slice(2);
if (!resourcesArg || !stageArg || !asarModule) throw new Error('Pass resources, stage and @electron/asar module paths.');
const resources = path.resolve(resourcesArg);
const stage = path.resolve(stageArg);
const asar = require(path.resolve(asarModule));
const archive = path.join(resources, 'app.asar');
const manifest = JSON.parse(fs.readFileSync(path.join(resources, 'bundle', 'manifest.json'), 'utf8'));
const stagedManifest = JSON.parse(fs.readFileSync(path.join(stage, 'bundle', 'manifest.json'), 'utf8'));
assert.deepEqual(manifest, stagedManifest);
validateCoreFiles(path.join(resources, 'bundle', 'core'), Object.keys(manifest.files));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
for (const [file, expected] of Object.entries(manifest.files)) {
  assert.equal(hash(fs.readFileSync(path.join(resources, 'bundle', 'core', file))), expected, file);
}
const names = asar.listPackage(archive).map(name => name.replaceAll('\\', '/'));
for (const name of names) {
  assert.ok(!/(?:node_modules|venv|handover_|base64d|\.pyc|feedback)/i.test(name), name);
  const relative = name.replace(/^\//, '');
  const input = path.join(stage, relative);
  if (fs.existsSync(input) && fs.statSync(input).isFile() && relative !== 'package.json') {
    assert.deepEqual(asar.extractFile(archive, relative), fs.readFileSync(input), relative);
  }
}
for (const file of ['main.cjs', 'agent-menu.cjs', 'browser-session.cjs', 'diagnostics.cjs', 'diagnostics-window.cjs',
  'external-links.cjs', 'floating-windows.cjs', 'test/external-links-smoke.cjs', 'browser-access.cjs',
  'capture-settings.cjs', 'ui-commands.cjs', 'toolbar.cjs', 'toolbar-preload.cjs',
  'toolbar.html', 'toolbar.js', 'toolbar.css', 'test/toolbar-smoke.cjs', 'release-identity.json']) {
  assert.ok(names.includes('/' + file), `Missing ${file}`);
}
const metadata = JSON.parse(asar.extractFile(archive, 'package.json'));
assert.equal(metadata.version, JSON.parse(fs.readFileSync(path.join(stage, 'package.json'), 'utf8')).version);
const identity = stagedReleaseIdentity(stage);
assert.deepEqual(JSON.parse(asar.extractFile(archive, 'release-identity.json')), identity);
assert.equal(metadata.version, identity.desktopVersion);
assert.equal(manifest.id, identity.coreBundleId);
const coreFiles = [];
function walk(directory, relative = '') {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const name = relative ? relative + '/' + entry.name : entry.name;
    if (entry.isDirectory()) walk(path.join(directory, entry.name), name);
    else { assert.ok(entry.isFile(), name); coreFiles.push(name); }
  }
}
walk(path.join(resources, 'bundle', 'core'));
assert.deepEqual(coreFiles.sort(), Object.keys(manifest.files).sort());
console.log(`Package verified: Desktop ${metadata.version}, ${coreFiles.length} exact Core files, explicit shell helpers; bundle ${manifest.id}.`);
