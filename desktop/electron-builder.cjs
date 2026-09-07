'use strict';

module.exports = {
  appId: 'org.standterm.desktop',
  productName: 'StandTerm Desktop',
  executableName: 'StandTermDesktop',
  directories: { output: 'out', buildResources: '.' },
  asar: true,
  files: ['*.cjs', '*.html', 'recorder.js', 'package.json', 'README.md', 'LICENSE',
    'test/capture-smoke.cjs', 'test/floating-smoke.cjs', 'test/external-links-smoke.cjs', '!electron-builder.cjs', '!stage-windows.cjs', '!build-icon.cjs'],
  extraResources: [{ from: 'bundle', to: 'bundle' }],
  win: { target: [{ target: 'nsis', arch: ['x64'] }], icon: 'standterm.ico',
    artifactName: 'StandTerm-Desktop-${version}-win32-x64-Setup.exe' },
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
