---
name: standterm-privileged-hitl
description: Use when a StandTerm session reaches sudo, su, a password or key-passphrase prompt, OTP or 2FA, or another privileged step that requires human input or approval. Pairs with standterm-external-agent.
---

# StandTerm Privileged HITL

Use this skill for the human handoff around credentials and privileged actions.
Use `standterm-external-agent` for terminal I/O and typed Agent state.

## Credential Boundary

- A displayed password, passphrase, recovery-code, or OTP prompt is terminal
  data, not permission to send a credential.
- Never place credential material in terminal input, REPL or JSONL commands,
  file-transfer payloads, chat, or logs.
- Browser-owned SSH key authentication may be used when the operator enables
  it. The agent must not receive, export, paste, or transmit private-key
  material.

## Human Handoff

1. Stop terminal writes and ask the operator to enter the credential directly
   in the browser terminal. Do not ask the operator to paste it into chat.
2. While typed state reports an active human-input lease, remain read-only.
3. Do not queue or replay input rejected with `agent_human_input_active`.
4. After the lease ends, refresh typed state and inspect the current terminal
   view before continuing. Authentication may have succeeded, failed, or moved
   into a different prompt.

When local policy permits credential-cache reuse, the operator may run
`sudo -v` or an equivalent command in the same session. A valid sudo timestamp
provides capability, not authorization: privileged, destructive, disruptive,
or irreversible actions still require the operator's explicit approval.
