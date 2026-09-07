'use strict';

// Build-time only. Never copy a developer profile, venv or untracked file.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { writeIcon } = require('./build-icon.cjs');

const root = path.resolve(__dirname, '..');
const tracked = execFileSync('git', ['ls-files', '-z'], { cwd: root, encoding: 'utf8' }).split('\0').filter(Boolean);
const coreFiles = tracked.filter(file => /^[^/]+\.py$/.test(file)
  || /^(static|templates|terminal_backends|scripts)\//.test(file)
  || ['requirements.txt', 'LICENSE', 'THIRD-PARTY-NOTICES.md', 'desktop/backend.py'].includes(file));
// This new shared lifetime lease is required by the packaged backend even while
// awaiting its first Git commit. Never include arbitrary untracked Core files.
if (!coreFiles.includes('desktop/runtime.py')) coreFiles.push('desktop/runtime.py');
const shellFiles = [
  'package.json', 'package-lock.json', 'electron-builder.cjs', 'installer.nsh', 'main.cjs', 'policy.cjs',
  'capture.cjs', 'capture-file.cjs', 'recorder.html', 'recorder.js', 'setup.cjs',
  'setup.html', 'README.md', 'smoke.cjs', 'test/capture-smoke.cjs',
  'desktop-mode.cjs', 'squirrel-events.cjs', 'port.cjs',
  'installer.cjs', 'installer-shortcuts.cjs',
  'legacy-install.nsh',
  'floating-windows.cjs', 'test/floating-smoke.cjs',
  'diagnostics.cjs',
  'browser-session.cjs',
  'diagnostics-window.cjs', 'external-links.cjs',
  'test/external-links-smoke.cjs',
];
fs.mkdirSync(path.join(__dirname, 'dist'), { recursive: true });
const stage = fs.mkdtempSync(path.join(__dirname, 'dist', 'windows-build-'));
function copy(source, destination) {
  if (!fs.lstatSync(source).isFile()) throw new Error(`Not a regular input file: ${source}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
}
for (const file of shellFiles) copy(path.join(__dirname, file), path.join(stage, file));
copy(path.join(root, 'LICENSE'), path.join(stage, 'LICENSE'));
copy(path.join(__dirname, 'bootstrap.py'), path.join(stage, 'bundle', 'bootstrap.py'));
copy(path.join(__dirname, 'windows_job.py'), path.join(stage, 'bundle', 'windows_job.py'));
copy(path.join(__dirname, 'runtime.py'), path.join(stage, 'bundle', 'runtime.py'));
copy(path.join(__dirname, 'runtime_cleanup.py'), path.join(stage, 'bundle', 'runtime_cleanup.py'));
writeIcon(path.join(stage, 'standterm.ico'));
const files = {};
for (const file of coreFiles.sort()) {
  copy(path.join(root, file), path.join(stage, 'bundle', 'core', file));
  files[file] = createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex');
}
const id = createHash('sha256').update(JSON.stringify(files)).digest('hex');
fs.writeFileSync(path.join(stage, 'bundle', 'manifest.json'), JSON.stringify({ version: 1, id, files }, null, 2), { flag: 'wx' });
console.log(stage);
