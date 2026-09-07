'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const net = require('node:net');

const validPort = value => Number.isInteger(value) && value >= 1 && value <= 65535;
const HOST_PORT_ATTEMPTS = 20;

function checkHostPort(port, { backendBound = false } = {}, createServer = () => net.createServer()) {
  if (!validPort(port)) return Promise.reject(new Error('Invalid host port.'));
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once('error', error => {
      // After WSL binds, its localhost relay may already own the Windows port.
      // Only authenticated instance verification can accept that listener.
      if (backendBound && error.code === 'EADDRINUSE') { resolve(); return; }
      if (['EACCES', 'EADDRINUSE'].includes(error.code)) {
        reject(Object.assign(new Error('The selected port is unavailable on the desktop host.'), {
          code: 'HOST_PORT_UNAVAILABLE', port,
          reason: error.code === 'EACCES' ? 'host_permission_denied' : 'host_address_in_use',
        }));
      } else reject(error);
    });
    // Real binding detects Windows exclusions without parsing localized netsh output.
    server.listen({ host: '127.0.0.1', port, exclusive: true }, () => server.close(error => {
      if (error) reject(error); else resolve();
    }));
  });
}

function parsePortConflict(line, requestedPort) {
  const data = JSON.parse(line);
  if (data?.type !== 'standterm_desktop_bind_error') return null;
  if (data.version !== 1 || data.code !== 'address_in_use' || data.port !== requestedPort
      || !validPort(data.port) || (data.suggested_port !== null
        && (!validPort(data.suggested_port) || data.suggested_port < 49152
          || data.suggested_port === data.port))) throw new Error('Invalid desktop bind response.');
  return Object.assign(new Error('The selected desktop port is already in use.'), {
    code: 'PORT_IN_USE', port: data.port, suggestedPort: data.suggested_port,
  });
}

async function startWithPort({ settingsPath, launch, verify, stop, confirm, notify, checkHost }) {
  let savedPort;
  try {
    const settings = JSON.parse(await fs.readFile(settingsPath, 'utf8'));
    if (settings.version !== 1 || !validPort(settings.port)) throw new Error('Invalid port settings.');
    savedPort = settings.port;
  } catch (error) {
    if (error.code !== 'ENOENT') await notify('Could not read the saved desktop port. Selecting an automatic port.');
  }
  let port = savedPort ?? 0;
  let remember = true;
  let hostAttempts = 0;
  let pendingChange;
  while (true) {
    let handoff;
    let actualPort;
    try {
      if (checkHost && port) await checkHost(port, { backendBound: false });
      handoff = await launch(port);
      actualPort = Number(new URL(handoff.origin).port);
      if (!validPort(actualPort) || (port !== 0 && actualPort !== port)) throw new Error('Backend bound an unexpected port.');
      if (checkHost) await checkHost(actualPort, { backendBound: true });
      await verify(handoff);
    } catch (error) {
      await stop();
      if (checkHost && error.code === 'HOST_PORT_UNAVAILABLE') {
        if (++hostAttempts >= HOST_PORT_ATTEMPTS) throw new Error('No usable Windows/WSL loopback port was found. The saved port was not changed.');
        if (port && !pendingChange) pendingChange = { port, reason: error.reason };
        // Core still selects candidates using its service exclusions and real
        // binding. Do not replace that policy with an unchecked host-only port.
        port = 0;
        continue;
      }
      if (error.code !== 'PORT_IN_USE') throw error;
      if (error.suggestedPort === null) throw new Error(`Port ${port} is in use and no automatic port is available.`);
      const choice = await confirm(port, error.suggestedPort);
      if (!['once', 'remember'].includes(choice)) {
        throw Object.assign(new Error('Desktop startup canceled. The saved port was not changed.'), { code: 'SETUP_CANCELED' });
      }
      port = error.suggestedPort;
      remember = choice === 'remember';
      continue;
    }
    if (pendingChange) {
      let choice;
      try { choice = await confirm(pendingChange.port, actualPort, pendingChange.reason); }
      catch (error) { await stop(); throw error; }
      if (!['once', 'remember'].includes(choice)) {
        await stop();
        throw Object.assign(new Error('Desktop startup canceled. The saved port was not changed.'), { code: 'SETUP_CANCELED' });
      }
      remember = choice === 'remember';
    }
    // Persist only after dual-side checks, authenticated verification and consent.
    port = actualPort;
    if (remember && savedPort !== port) {
      const temporary = `${settingsPath}.${randomUUID()}.tmp`;
      try {
        await fs.mkdir(path.dirname(settingsPath), { recursive: true });
        await fs.writeFile(temporary, JSON.stringify({ version: 1, port }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
        await fs.rename(temporary, settingsPath);
      } catch {
        await notify(`Port ${port} is active, but could not be saved. This launch will continue.`);
      } finally {
        await fs.unlink(temporary).catch(() => {});
      }
    }
    return handoff;
  }
}

module.exports = { startWithPort, parsePortConflict, checkHostPort };
