'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { releaseIdentity, stagedReleaseIdentity, artifactNames } = require('../release-identity.cjs');

function fixture(t, coreVersion = '2.11.0-dev', desktopVersion = '0.4.4') {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-release-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const write = (name, value) => fs.writeFileSync(path.join(directory, name), typeof value === 'string' ? value : JSON.stringify(value));
  fs.mkdirSync(path.join(directory, 'bundle/core'), { recursive: true });
  write('package.json', { version: desktopVersion });
  write('package-lock.json', { version: desktopVersion, packages: { '': { version: desktopVersion } } });
  const core = `"""Version fixture."""\nCORE_VERSION = '${coreVersion}'\n`;
  const hash = bytes => createHash('sha256').update(bytes).digest('hex');
  const files = { 'core_version.py': hash(core) };
  write('bundle/core/core_version.py', core);
  const manifest = { version: 1, id: hash(JSON.stringify(files)), files };
  write('bundle/manifest.json', manifest);
  return { directory, write, manifest };
}

test('paired filenames preserve stable and development Core versions on both platforms', t => {
  for (const core of ['2.11.0', '2.11.0-dev', '2.11.0-rc.1+build.2']) {
    const f = fixture(t, core);
    const identity = releaseIdentity(f.directory);
    assert.equal(identity.releaseLabel, `0.4.4-${core}`);
    assert.equal(identity.desktopVersion, '0.4.4');
    assert.equal(identity.coreVersion, core);
    assert.equal(identity.coreBundleId, f.manifest.id);
    f.write('release-identity.json', identity);
    assert.deepEqual(stagedReleaseIdentity(f.directory), identity);
    for (const [platform, suffix] of [['darwin', 'mac-arm64.dmg'], ['win32', 'win32-x64-Setup.exe']]) {
      const names = artifactNames(identity, platform, platform === 'darwin' ? 'arm64' : 'x64');
      assert.equal(names.installer, `StandTerm-Desktop-0.4.4-${core}-${suffix}`);
      assert.equal(names.checksum, names.installer + '.sha256');
      assert.ok(names.archive.includes(identity.releaseLabel));
      assert.ok(names.patch.includes(identity.releaseLabel));
      assert.equal(names.tag, `desktop-v0.4.4-${core}`);
    }
  }
});

test('release identity rejects missing, malformed and inconsistent build inputs', t => {
  const f = fixture(t);
  assert.throws(() => stagedReleaseIdentity(f.directory), /ENOENT/);
  f.write('release-identity.json', { ...releaseIdentity(f.directory), coreVersion: '2.11.0' });
  assert.throws(() => stagedReleaseIdentity(f.directory), /identity changed/);
  f.write('bundle/core/core_version.py', "CORE_VERSION = '2.11.0'\n");
  assert.throws(() => releaseIdentity(f.directory), /manifest hash/);
  f.write('bundle/manifest.json', { ...f.manifest, id: '0'.repeat(64) });
  assert.throws(() => releaseIdentity(f.directory), /manifest identity/);
  const lock = fixture(t);
  lock.write('package-lock.json', { version: '0.4.3', packages: { '': { version: '0.4.4' } } });
  assert.throws(() => releaseIdentity(lock.directory), /package and lockfile/);
  for (const core of ['unknown', '2.11', '02.11.0', '2.11.0-01', '../2.11.0']) {
    assert.throws(() => releaseIdentity(fixture(t, core).directory), /literal SemVer/);
  }
  assert.throws(() => artifactNames({}, 'linux', 'x64'), /Unsupported/);
});

test('builder uses staged paired names without changing installer SemVer ordering', t => {
  const f = fixture(t);
  f.write('release-identity.json', releaseIdentity(f.directory));
  for (const name of ['electron-builder.cjs', 'release-identity.cjs']) {
    fs.copyFileSync(path.join(__dirname, '..', name), path.join(f.directory, name));
  }
  const builder = require(path.join(f.directory, 'electron-builder.cjs'));
  assert.equal(builder.mac.artifactName, 'StandTerm-Desktop-0.4.4-2.11.0-dev-mac-${arch}.${ext}');
  assert.equal(builder.win.artifactName, 'StandTerm-Desktop-0.4.4-2.11.0-dev-win32-x64-Setup.exe');
  assert.equal(builder.extraMetadata, undefined);
  assert.equal(builder.buildVersion, undefined);
  assert.equal(builder.mac.bundleVersion, undefined);
  const semver = require('semver'); // The locked build toolchain's actual parser.
  assert.ok(semver.gt(releaseIdentity(f.directory).desktopVersion, '0.4.3'));
  assert.equal(semver.prerelease(releaseIdentity(f.directory).desktopVersion), null);
  assert.ok(semver.lt('0.4.4-2.11.0-dev', '0.4.4'));
});
