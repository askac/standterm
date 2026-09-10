'use strict';

const fs = require('node:fs');
const path = require('node:path');
const EVENTS = new Set(['startup', 'setup_start', 'setup_ready', 'backend_launch', 'backend_ready',
  'backend_exit', 'backend_spawn_failed', 'backend_verify_retry', 'backend_verified',
  'host_port_rejected', 'port_change', 'window_ready', 'startup_failed', 'shutdown', 'devtools_opened']);
const CODES = new Set(['EACCES', 'EADDRINUSE', 'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT',
  'ENOENT', 'EPIPE', 'HOST_PORT_UNAVAILABLE', 'PORT_IN_USE', 'SETUP_CANCELED']);
const MAX_LOG_BYTES = 256 * 1024;

function createDiagnostics(directory, { mode, version }) {
  const file = path.join(directory, 'startup.jsonl');
  const previous = path.join(directory, 'startup.previous.jsonl');
  let available = true;
  const recent = [];
  function write(event, details = {}) {
    if (!EVENTS.has(event)) return;
    const record = { time: new Date().toISOString(), event,
      mode: ['wsl', 'macos'].includes(mode) ? mode : 'windows' };
    if (/^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$/.test(version)) record.version = version;
    // Whitelist structured fields. Never persist errors, stdout/stderr, URLs,
    // terminal data, environment values, credential objects or arbitrary text.
    for (const key of ['port', 'candidate', 'attempt', 'elapsedMs', 'exitCode']) {
      if (Number.isSafeInteger(details[key]) && details[key] >= 0 && details[key] <= 2147483647) record[key] = details[key];
    }
    if (CODES.has(details.code)) record.code = details.code;
    if (typeof details.expected === 'boolean') record.expected = details.expected;
    recent.push(record);
    if (recent.length > 200) recent.shift();
    try {
      fs.mkdirSync(directory, { recursive: true });
      if (fs.existsSync(file) && fs.statSync(file).size >= MAX_LOG_BYTES) fs.renameSync(file, previous);
      fs.appendFileSync(file, JSON.stringify(record) + '\n', { mode: 0o600 });
      available = true;
    } catch { available = false; }
  }
  return { directory, file, write, snapshot: () => recent.map(record => ({ ...record })),
    get available() { return available; } };
}

function agentConnectionInfo({ origin, mode, instanceId }) {
  const url = new URL(origin);
  if (url.origin !== origin || url.protocol !== 'http:' || url.hostname !== '127.0.0.1'
      || !url.port || url.username || url.password) throw new Error('Invalid diagnostic origin.');
  if (!['windows', 'wsl', 'macos'].includes(mode) || typeof instanceId !== 'string'
      || !/^[A-Za-z0-9_-]{1,256}$/.test(instanceId)) throw new Error('Invalid connection identity.');
  return { schema: 'standterm_agent_connection', schema_version: 1,
    base_url: origin, agentinfo_url: `${origin}/agentinfo`, instance_id: instanceId, backend_mode: mode };
}

function diagnosticsMenu({ origin, mode, instanceId, version, coreVersion, logger, openLogs, openTools, openStatus,
  copyText, persistent = false }) {
  const info = agentConnectionInfo({ origin, mode, instanceId });
  return { label: 'Diagnostics', submenu: [
    { id: 'diagnostics-status', label: 'Status and recent events...', click: openStatus },
    { id: 'diagnostics-version', label: `StandTerm Desktop ${version}`, enabled: false },
    { id: 'diagnostics-core-version', label: `Core version: ${coreVersion || 'Unknown (older Core)'}`, enabled: false },
    { id: 'diagnostics-backend', label: `Backend: ${mode === 'wsl' ? 'WSL' : 'Native'}`, enabled: false },
    { id: 'diagnostics-origin', label: `URL: ${origin}`, enabled: false },
    { id: 'diagnostics-copy-origin', label: 'Copy backend URL', click: () => copyText(info.base_url) },
    { id: 'diagnostics-copy-agent', label: 'Copy agent connection info', click: () => copyText(JSON.stringify(info, null, 2)) },
    { label: persistent ? 'Web settings: saved per origin (same as Core)' : 'Web settings: temporary test profile', enabled: false },
    { type: 'separator' },
    { id: 'diagnostics-logs', label: 'Open diagnostics log folder', click: openLogs },
    { label: logger.available ? 'Logs exclude credentials and terminal content' : 'Diagnostic log could not be written', enabled: false },
    { id: 'diagnostics-devtools', label: 'Developer Tools...', click: openTools },
  ] };
}

async function openDeveloperTools(contents, confirm) {
  const answer = await confirm();
  if (answer !== true || contents.isDestroyed()) return false;
  contents.openDevTools({ mode: 'detach' });
  return true;
}

module.exports = { createDiagnostics, diagnosticsMenu, agentConnectionInfo, openDeveloperTools, MAX_LOG_BYTES };
