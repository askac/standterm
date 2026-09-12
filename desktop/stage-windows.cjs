'use strict';

// Build-time only. Never copy a developer profile, venv or untracked file.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { writeIcon } = require('./build-icon.cjs');
const { coreFiles: selectCoreFiles, validateCoreFiles } = require('./core-files.cjs');
const { releaseIdentity } = require('./release-identity.cjs');

const platform = process.argv.includes('--macos') ? 'macos' : 'windows';
if (platform === 'macos' && process.platform !== 'darwin') throw new Error('Stage macOS on a native Mac.');
const root = path.resolve(__dirname, '..');
const tracked = execFileSync('git', ['ls-files', '-z'], { cwd: root, encoding: 'utf8' }).split('\0').filter(Boolean);
const coreFiles = selectCoreFiles(tracked);
validateCoreFiles(root, coreFiles);
const shellFiles = [
  'package.json', 'package-lock.json', 'electron-builder.cjs', 'installer.nsh', 'main.cjs', 'policy.cjs',
  'capture.cjs', 'capture-file.cjs', 'recorder.html', 'recorder.js', 'setup.cjs', 'macos-python.cjs',
  'setup.html', 'README.md', 'smoke.cjs', 'test/capture-smoke.cjs',
  'desktop-mode.cjs', 'squirrel-events.cjs', 'port.cjs',
  'installer.cjs', 'installer-shortcuts.cjs',
  'legacy-install.nsh',
  'floating-windows.cjs', 'test/floating-smoke.cjs',
  'diagnostics.cjs', 'agent-menu.cjs',
  'browser-session.cjs',
  'diagnostics-window.cjs', 'external-links.cjs',
  'test/external-links-smoke.cjs',
  'capture-settings.cjs', 'ui-commands.cjs', 'toolbar.cjs', 'toolbar-preload.cjs',
  'toolbar.html', 'toolbar.js', 'toolbar.css',
  'test/toolbar-smoke.cjs',
  'browser-access.cjs',
  'context-paste.cjs',
  'release-identity.cjs',
  'core-source.cjs', 'backend-stop.cjs',
];
fs.mkdirSync(path.join(__dirname, 'dist'), { recursive: true });
const stage = fs.mkdtempSync(path.join(__dirname, 'dist', `${platform}-build-`));
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
copy(path.join(__dirname, 'core_manager.py'), path.join(stage, 'bundle', 'core_manager.py'));
copy(path.join(__dirname, 'backend.py'), path.join(stage, 'bundle', 'backend.py'));
writeIcon(path.join(stage, 'standterm.ico'));
if (platform === 'macos') {
  // Reuse the existing terminal glyph at native icon sizes using macOS build tools.
  const iconset = path.join(stage, 'standterm.iconset');
  fs.mkdirSync(iconset);
  for (const size of [16, 32, 128, 256, 512]) {
    for (const scale of [1, 2]) {
      execFileSync('/usr/bin/sips', ['-s', 'format', 'png', '-z', String(size * scale), String(size * scale),
        path.join(stage, 'standterm.ico'), '--out', path.join(iconset, `icon_${size}x${size}${scale === 2 ? '@2x' : ''}.png`)],
      { stdio: 'pipe' });
    }
  }
  execFileSync('/usr/bin/iconutil', ['-c', 'icns', iconset, '-o', path.join(stage, 'standterm.icns')]);
}
const files = {};
for (const file of coreFiles.sort()) {
  copy(path.join(root, file), path.join(stage, 'bundle', 'core', file));
  files[file] = createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex');
}
const id = createHash('sha256').update(JSON.stringify(files)).digest('hex');
validateCoreFiles(path.join(stage, 'bundle', 'core'), Object.keys(files));
fs.writeFileSync(path.join(stage, 'bundle', 'manifest.json'), JSON.stringify({ version: 1, id, files }, null, 2), { flag: 'wx' });
fs.writeFileSync(path.join(stage, 'release-identity.json'), JSON.stringify(releaseIdentity(stage), null, 2) + '\n', { flag: 'wx' });
console.log(stage);
