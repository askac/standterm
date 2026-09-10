'use strict';

const fs = require('node:fs');
const path = require('node:path');

const SKILL_DIRS = ['standterm-external-agent-skill', 'standterm-file-transfer', 'standterm-privileged-hitl'];
const REQUIRED = ['app.py', 'core_version.py', 'requirements.txt', 'README.md', 'run.sh', 'run.bat',
  'run.command', 'run_at_wsl.bat', 'run_at_wsl+screen.bat', 'install.sh', 'install.ps1', 'install.command',
  'LICENSE', 'THIRD-PARTY-NOTICES.md', 'desktop/backend.py', 'desktop/runtime.py', 'docs/agent_socket_contract.md',
  'docs/backend_plugin_contract.md', 'docs/venv_prompt.txt',
  ...['cli', 'jsonl', 'repl', 'shcmd', 'scp', 'type', 'rsfile', 'mcp'].map(name => `scripts/agent_${name}.py`),
  ...SKILL_DIRS.flatMap(name => ['SKILL.md', 'boot_prompt.txt', 'skill_prompt.txt']
    .map(file => `docs/examples/${name}/${file}`)),
  ...['connection', 'clients', 'terminal-workflows']
    .map(name => `docs/examples/standterm-external-agent-skill/references/${name}.md`),
];

function coreFiles(tracked) {
  return tracked.filter(file => /^[^/]+\.py$/.test(file)
    || /^(static|templates|terminal_backends|scripts)\//.test(file)
    || REQUIRED.includes(file)).sort();
}

function validateCoreFiles(root, files) {
  const selected = new Set(files);
  for (const file of REQUIRED) {
    if (!selected.has(file)) throw new Error(`Missing required Core input: ${file}`);
  }
  for (const file of files) {
    if (!fs.lstatSync(path.join(root, file)).isFile()) throw new Error(`Not a regular Core input: ${file}`);
    if (!file.startsWith('docs/examples/') || !file.endsWith('.md')) continue;
    const text = fs.readFileSync(path.join(root, file), 'utf8');
    for (const match of text.matchAll(/\[[^\]]*\]\(([^\s)]+)\)/g)) {
      const link = match[1].split('#')[0];
      if (!link || /^[a-z][a-z0-9+.-]*:/i.test(link)) continue;
      const target = path.posix.normalize(path.posix.join(path.posix.dirname(file), link));
      if (!selected.has(target)) throw new Error(`Missing bundled skill reference: ${file} -> ${link}`);
    }
  }
}

module.exports = { coreFiles, validateCoreFiles, REQUIRED };
