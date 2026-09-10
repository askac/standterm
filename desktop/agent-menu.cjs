'use strict';

const { agentConnectionInfo } = require('./diagnostics.cjs');

const INTRO = 'Read the agentinfo_url in the connection JSON below and verify instance_id before proceeding. '
  + 'Use launch_dir, python_path, scripts and skill/skills from that exact instance; do not guess ports or runtime paths. '
  + 'Read the bundled skill entrypoint and only the references needed for this task. '
  + 'Paths belong to the backend environment (macOS, Windows or WSL): if access or the endpoint is unavailable, report the limitation '
  + 'and ask for the correct environment or fresh connection information instead of scanning for another instance. ';
const BOUNDARY = 'Never expose handoff secrets or credentials. Skill installation is not permission to operate terminals. '
  + 'Preserve token minting, terminal modes, human-input boundaries and fresh per-copy browser approval. '
  + 'If no terminal is enabled, ask the user to enable External Agent and mint its token in the intended tab. '
  + 'Do not execute terminal commands until the user specifies the task and target.';
const PROMPTS = {
  usage: 'Help me use this StandTerm instance for my stated task. ' + INTRO
    + 'Use the skill boot_prompt_path for routine operation; do not install or overwrite local skills in this workflow. ',
  install: 'Help me install or update the bundled StandTerm skills for this agent. ' + INTRO
    + 'Read each available skill install_prompt_path (skill_prompt.txt), including external-agent, file-transfer and privileged-HITL guidance. '
    + 'Use this agent\'s supported skill installation mechanism, preserving references and relative paths. '
    + 'Compare existing installations first and ask before overwriting customized content. '
    + 'If persistent skills are unsupported, read the documents for this session and report that nothing was installed. '
    + 'Use helpers from the active Core with its reported Python, rather than copying them into the skill installation. ',
  transfer: 'Help me prepare a StandTerm file transfer. ' + INTRO
    + 'Read the external-agent and file-transfer skills. Ask for any missing source, destination and file details. '
    + 'Prefer the typed backend copy helper; do not automate the human Files UI or approve the copy yourself. '
    + 'Do not fall back to terminal-stream rescue without a new explicit instruction. ',
};

function agentMenu({ origin, mode, instanceId, copyText, showHelp }) {
  const info = agentConnectionInfo({ origin, mode, instanceId });
  const json = JSON.stringify(info, null, 2);
  const prompt = kind => PROMPTS[kind] + BOUNDARY + '\n\n' + json;
  return { id: 'agent-menu', label: 'Agent', submenu: [
    { id: 'agent-help', label: 'Getting started...', click: showHelp },
    { type: 'separator' },
    { id: 'agent-copy-usage', label: 'Copy usage prompt', click: () => copyText(prompt('usage')) },
    { id: 'agent-copy-install', label: 'Copy skill installation prompt', click: () => copyText(prompt('install')) },
    { id: 'agent-copy-transfer', label: 'Copy file-transfer prompt', click: () => copyText(prompt('transfer')) },
    { type: 'separator' },
    { id: 'agent-copy-connection', label: 'Copy connection info (JSON)', click: () => copyText(json) },
    { id: 'agent-copy-agentinfo', label: 'Copy agentinfo URL', click: () => copyText(info.agentinfo_url) },
  ] };
}

module.exports = { agentMenu };
