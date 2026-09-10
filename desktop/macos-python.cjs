'use strict';

const path = require('node:path');

const MACOS_HELP = 'Install native macOS Python 3.10+ with venv and ensurepip support first.\n\n'
  + 'Python must match this app’s CPU architecture. StandTerm checks Homebrew, MacPorts and PATH, '
  + 'or lets you select an installed interpreter. Apple’s /usr/bin/python3 developer-tools stub is not launched. '
  + 'StandTerm does not install Python, Homebrew, Rosetta or system packages.';

function macPythonCandidates(saved, env = {}) {
  // Finder launch has a minimal PATH; known package-manager locations remain usable.
  return [...new Set([saved, '/opt/homebrew/bin/python3', '/opt/local/bin/python3', '/usr/local/bin/python3',
    ...(env.PATH || '').split(':').filter(directory => path.posix.isAbsolute(directory))
      .map(directory => path.posix.join(directory, 'python3'))])]
    .filter(candidate => typeof candidate === 'string' && path.posix.isAbsolute(candidate)
      && candidate !== '/usr/bin/python3' && !/[\r\n\0]/.test(candidate)).slice(0, 16);
}

function validMacPython(info, arch) {
  if (!['arm64', 'x64'].includes(arch)) return false;
  return info?.type === 'python_info' && info.platform === 'darwin' && info.bits === 64 && info.venv === true
    && Array.isArray(info.version) && info.version[0] === 3 && Number.isInteger(info.version[1]) && info.version[1] >= 10
    && ({ arm64: 'arm64', x64: 'x86_64' })[arch] === info.machine
    && typeof info.executable === 'string' && path.posix.isAbsolute(info.executable)
    && info.executable !== '/usr/bin/python3' && !/[\r\n\0]/.test(info.executable);
}

module.exports = { MACOS_HELP, macPythonCandidates, validMacPython };
