'use strict';

const { stagedReleaseIdentity, artifactNames } = require('./release-identity.cjs');
const identity = stagedReleaseIdentity(__dirname);

module.exports = {
  appId: 'org.standterm.desktop',
  productName: 'StandTerm Desktop',
  executableName: 'StandTermDesktop',
  // Keep development app copies out of macOS Spotlight application results.
  directories: { output: process.platform === 'darwin' ? 'out.noindex' : 'out', buildResources: '.' },
  asar: true,
  files: ['*.cjs', '*.html', 'recorder.js', 'toolbar.js', 'toolbar.css', 'package.json', 'release-identity.json', 'README.md', 'LICENSE',
    'test/capture-smoke.cjs', 'test/floating-smoke.cjs', 'test/external-links-smoke.cjs', 'test/toolbar-smoke.cjs', '!electron-builder.cjs', '!stage-windows.cjs', '!build-icon.cjs', '!release-identity.cjs'],
  extraResources: [{ from: 'bundle', to: 'bundle' }],
  mac: {
    icon: 'standterm.icns',
    target: [{ target: 'dmg', arch: ['arm64'] }],
    category: 'public.app-category.developer-tools',
    artifactName: artifactNames(identity, 'darwin', 'arm64').installer.replace('-mac-arm64.dmg', '-mac-${arch}.${ext}'),
    // Local evaluation only: ad-hoc signing never reads a Developer ID identity.
    identity: '-',
    notarize: false,
  },
  dmg: { sign: false },
  win: { target: [{ target: 'nsis', arch: ['x64'] }], icon: 'standterm.ico',
    artifactName: artifactNames(identity, 'win32', 'x64').installer },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowElevation: false,
    allowToChangeInstallationDirectory: false,
    createDesktopShortcut: false,
    createStartMenuShortcut: false,
    deleteAppDataOnUninstall: false,
    runAfterFinish: false,
    include: 'installer.nsh',
    installerIcon: 'standterm.ico',
    uninstallerIcon: 'standterm.ico',
  },
  publish: null,
};
