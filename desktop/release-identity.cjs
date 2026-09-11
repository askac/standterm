'use strict';

// Build-time identity comes from the staged package and hashed Core, never a live backend.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');
const NUMBER = '(?:0|[1-9][0-9]*)';
const PRE = `(?:${NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)`;
const SEMVER = new RegExp(`^${NUMBER}\\.${NUMBER}\\.${NUMBER}(?:-${PRE}(?:\\.${PRE})*)?(?:\\+[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?$`);
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

function readFile(directory, name) {
  const file = path.join(directory, name);
  if (!fs.lstatSync(file).isFile()) throw new Error(`Release input must be a regular file: ${name}`);
  return fs.readFileSync(file);
}

function releaseIdentity(directory) {
  const pkg = JSON.parse(readFile(directory, 'package.json'));
  const lock = JSON.parse(readFile(directory, 'package-lock.json'));
  if (typeof pkg.version !== 'string' || !SEMVER.test(pkg.version)
      || lock.version !== pkg.version || lock.packages?.['']?.version !== pkg.version) {
    throw new Error('Desktop package and lockfile must have matching independent SemVer versions.');
  }
  const manifest = JSON.parse(readFile(directory, 'bundle/manifest.json'));
  if (manifest.version !== 1 || !manifest.files || Array.isArray(manifest.files)
      || manifest.id !== sha256(JSON.stringify(manifest.files))) {
    throw new Error('Invalid Core manifest identity.');
  }
  const core = readFile(directory, 'bundle/core/core_version.py');
  if (manifest.files['core_version.py'] !== sha256(core)) throw new Error('Core version does not match its manifest hash.');
  const assignments = [...core.toString('utf8').matchAll(/^CORE_VERSION\s*=\s*(['"])([^'"\r\n]+)\1\s*$/gm)];
  if (assignments.length !== 1 || !SEMVER.test(assignments[0][2])) {
    throw new Error('Bundled Core must declare one literal SemVer CORE_VERSION.');
  }
  const desktopVersion = pkg.version;
  const coreVersion = assignments[0][2];
  return { schemaVersion: 1, desktopVersion, coreVersion,
    releaseLabel: `${desktopVersion}-${coreVersion}`, coreBundleId: manifest.id };
}

function stagedReleaseIdentity(directory) {
  const actual = releaseIdentity(directory);
  const recorded = JSON.parse(readFile(directory, 'release-identity.json'));
  if (!isDeepStrictEqual(actual, recorded)) throw new Error('Staged release identity changed; stage again before building.');
  return actual;
}

function artifactNames(identity, platform, architecture) {
  if (!['darwin', 'win32'].includes(platform) || !['arm64', 'x64'].includes(architecture)
      || (platform === 'win32' && architecture !== 'x64')) throw new Error('Unsupported release target.');
  const prefix = `StandTerm-Desktop-${identity.releaseLabel}`;
  const target = `${platform === 'darwin' ? 'mac' : 'win32'}-${architecture}`;
  const installer = `${prefix}-${target}${platform === 'darwin' ? '.dmg' : '-Setup.exe'}`;
  return { installer, checksum: `${installer}.sha256`, archive: `${prefix}-${target}-delivery.tar.gz`,
    patch: `desktop-${identity.releaseLabel}.patch`, tag: `desktop-v${identity.releaseLabel}` };
}

module.exports = { releaseIdentity, stagedReleaseIdentity, artifactNames };
