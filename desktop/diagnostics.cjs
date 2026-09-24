'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { create } = require('./i18n.js');
const EVENTS = new Set(['startup', 'setup_start', 'setup_ready', 'backend_launch', 'backend_ready',
  'backend_exit', 'backend_spawn_failed', 'backend_verify_retry', 'backend_verified',
  'host_port_rejected', 'port_change', 'window_ready', 'window_state_save_failed', 'startup_failed', 'core_failed', 'shutdown', 'devtools_opened', 'capture_failed']);
const CODES = new Set(['EACCES', 'EADDRINUSE', 'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT',
  'ENOENT', 'EPIPE', 'HOST_PORT_UNAVAILABLE', 'PORT_IN_USE', 'SETUP_CANCELED',
  'git_required', 'git_dirty', 'git_diverged', 'git_source_changed', 'invalid_git_workspace',
  'git_needs_setup', 'git_failed', 'invalid_archive', 'modified_runtime', 'invalid_bundle',
  'setup_busy', 'unsafe_runtime_path', 'dependencies_failed', 'venv_failed', 'setup_failed', 'setup_timeout']);
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
    for (const key of ['port', 'candidate', 'attempt', 'elapsedMs', 'exitCode', 'width', 'height']) {
      if (Number.isSafeInteger(details[key]) && details[key] >= 0 && details[key] <= 2147483647) record[key] = details[key];
    }
    if (CODES.has(details.code)) record.code = details.code;
    if (typeof details.expected === 'boolean') record.expected = details.expected;
    for (const key of ['visible', 'minimized', 'focused']) {
      if (typeof details[key] === 'boolean') record[key] = details[key];
    }
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
  copyText, persistent = false, t = create('en').t }) {
  const info = agentConnectionInfo({ origin, mode, instanceId });
  return { id: 'diagnostics', label: t('desktop.toolbar.menu_diagnostics'), submenu: [
    { id: 'diagnostics-status', label: t('desktop.diagnostics.show_status'), click: openStatus },
    { id: 'diagnostics-version', label: t('desktop.about.desktop_version', { version }), enabled: false },
    { id: 'diagnostics-core-version', label: t('desktop.about.core_version', { version: coreVersion || t('desktop.about.unknown_version') }), enabled: false },
    { id: 'diagnostics-backend', label: t('desktop.about.backend', { backend: mode === 'wsl' ? 'WSL' : t('desktop.diagnostics.native_backend') }), enabled: false },
    { id: 'diagnostics-origin', label: t('desktop.diagnostics.origin', { origin }), enabled: false },
    { id: 'diagnostics-copy-origin', label: t('desktop.diagnostics.copy_backend_url'), click: () => copyText(info.base_url) },
    { label: t(persistent ? 'desktop.diagnostics.web_settings_persistent' : 'desktop.diagnostics.web_settings_temporary'), enabled: false },
    { type: 'separator' },
    { id: 'diagnostics-logs', label: t('desktop.diagnostics.open_logs'), click: openLogs },
    { label: t(logger.available ? 'desktop.diagnostics.logs_filtered' : 'desktop.diagnostics.log_unwritable'), enabled: false },
    { id: 'diagnostics-devtools', label: t('desktop.diagnostics.devtools_menu'), click: openTools },
  ] };
}

async function openDeveloperTools(contents, confirm) {
  const answer = await confirm();
  if (answer !== true || contents.isDestroyed()) return false;
  contents.openDevTools({ mode: 'detach' });
  return true;
}

module.exports = { createDiagnostics, diagnosticsMenu, agentConnectionInfo, openDeveloperTools, MAX_LOG_BYTES };
