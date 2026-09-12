'use strict';

// Wait for GUI executables explicitly; shell launch success is not a smoke result.
const { spawnSync } = require('node:child_process');
const [executable, mode] = process.argv.slice(2);
if (!executable || !['windows', 'wsl', 'macos'].includes(mode)) {
  throw new Error('Pass the packaged executable and backend mode.');
}
const result = spawnSync(executable, [`--backend=${mode}`, '--desktop-smoke'], {
  env: process.env, encoding: 'utf8', timeout: 180000, maxBuffer: 4 * 1024 * 1024,
});
process.stdout.write(result.stdout || '');
process.stderr.write(result.stderr || '');
console.log(JSON.stringify({ type: 'desktop_smoke_process_result', exitCode: result.status,
  signal: result.signal, errorCode: result.error?.code || null }));
process.exit(result.error ? 1 : result.status ?? 1);
