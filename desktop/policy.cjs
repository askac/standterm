'use strict';

const path = require('node:path');

function backendCommand(root, platform, env) {
  if (platform === 'win32' && env.STANDTERM_DESKTOP_WSL_DISTRO) {
    const repo = env.STANDTERM_DESKTOP_WSL_REPO;
    if (!repo || !repo.startsWith('/')) {
      throw new Error('Set STANDTERM_DESKTOP_WSL_REPO to the absolute Linux repository path.');
    }
    return {
      executable: 'wsl.exe',
      args: ['--distribution', env.STANDTERM_DESKTOP_WSL_DISTRO, '--cd', repo,
        '--exec', env.STANDTERM_DESKTOP_WSL_PYTHON || `${repo}/tools/.venv_wsl/bin/python`,
        '-u', `${repo}/desktop/backend.py`],
      cwd: root,
    };
  }
  const venv = platform === 'win32' ? ['.venv', 'Scripts', 'python.exe']
    : [platform === 'darwin' ? '.venv_macos'
      : (env.WSL_DISTRO_NAME ? '.venv_wsl' : '.venv_linux'), 'bin', 'python'];
  return {
    executable: env.STANDTERM_DESKTOP_PYTHON || path.join(root, 'tools', ...venv),
    args: ['-u', path.join(root, 'desktop', 'backend.py')],
    cwd: root,
  };
}

function parseHandoff(line) {
  if (Buffer.byteLength(line) > 4096) throw new Error('Desktop handoff exceeds its size limit.');
  const data = JSON.parse(line);
  if (!data || data.type !== 'standterm_desktop_ready' || data.version !== 1) {
    throw new Error('Invalid desktop backend handshake.');
  }
  const url = new URL(data.origin);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port
      || url.origin !== data.origin || url.username || url.password) {
    throw new Error('Desktop backend must use an exact loopback origin.');
  }
  for (const key of ['instance_id', 'launcher_token', 'session_token', 'cookie_name']) {
    if (typeof data[key] !== 'string' || !/^[A-Za-z0-9_-]{1,256}$/.test(data[key])) {
      throw new Error('Invalid desktop authentication metadata.');
    }
  }
  for (const key of ['core_version', 'python_version']) {
    if (data[key] !== undefined && (typeof data[key] !== 'string' || data[key].length > 64
        || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(data[key]))) {
      throw new Error('Invalid backend version metadata.');
    }
  }
  if (data.core_bundle_id != null && (typeof data.core_bundle_id !== 'string'
      || !/^[a-f0-9]{64}$/.test(data.core_bundle_id))) throw new Error('Invalid Core build identity.');
  return data;
}

function allowedRequest(rawUrl, origin) {
  try {
    const url = new URL(rawUrl);
    if (url.username || url.password) return false;
    return url.origin === origin
      || (url.protocol === 'ws:' && `http://${url.host}` === origin)
      || (url.protocol === 'blob:' && url.origin === origin)
      || url.protocol === 'data:';
  } catch {
    return false;
  }
}

function allowedNavigation(rawUrl, origin) {
  try {
    const url = new URL(rawUrl);
    return url.origin === origin && !url.username && !url.password;
  } catch {
    return false;
  }
}

function allowedFloatingWindow(details, openerUrl, origin) {
  // This identifies our supported blank-child shape, not a privileged renderer.
  return details.url === 'about:blank' && details.disposition === 'new-window'
    && details.frameName === 'standterm-floating'
    && ['popup,width=720,height=620', 'popup,width=800,height=480'].includes(details.features) && !details.postBody
    && allowedNavigation(openerUrl, origin)
    // Core deliberately sends Referrer-Policy: no-referrer. The owned opener
    // URL is the authority; an absent referrer URL is not a foreign origin.
    && (details.referrer?.url === '' || allowedNavigation(details.referrer?.url, origin));
}

function allowedFilesDownload(rawUrl, origin) {
  try {
    const url = new URL(rawUrl);
    return allowedNavigation(rawUrl, origin) && !url.search && !url.hash
      && /^\/sftp\/download\/[A-Za-z0-9_-]{16,128}$/.test(url.pathname);
  } catch { return false; }
}

module.exports = { backendCommand, parseHandoff, allowedRequest, allowedNavigation, allowedFloatingWindow, allowedFilesDownload };
