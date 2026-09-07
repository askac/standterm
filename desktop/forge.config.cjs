'use strict';

module.exports = {
  outDir: 'out',
  packagerConfig: {
    name: 'StandTerm Desktop',
    executableName: 'StandTermDesktopEvaluation',
    appBundleId: 'org.standterm.desktop.evaluation',
    appCopyright: 'Copyright (c) 2026 ASKA C.',
    icon: 'standterm.ico',
    win32metadata: { ProductName: 'StandTerm Desktop', FileDescription: 'StandTerm Desktop' },
    asar: true,
    extraResource: ['bundle'],
    ignore: [/^\/bundle(?:\/|$)/, /^\/out(?:\/|$)/, /^\/forge\.config\.cjs$/],
  },
  makers: [{
    name: '@electron-forge/maker-squirrel',
    config: {
      name: 'StandTermDesktopEvaluation',
      authors: 'ASKA C.',
      title: 'StandTerm Desktop',
      description: 'StandTerm Desktop with Windows and WSL Core runtimes. Requires Python 3.10+ and venv support in the selected environment.',
      setupExe: `StandTerm-Desktop-${require('./package.json').version}-win32-x64-Setup.exe`,
      setupIcon: 'standterm.ico',
      noMsi: true,
    },
  }],
};
