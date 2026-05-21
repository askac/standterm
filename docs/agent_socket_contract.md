# Agent Socket Contract

This document describes the internal Agent Socket.IO contract. The UI is not
enabled yet; these events are currently for backend smoke tests and future
Agent UI wiring.

## Scope

Agent state is scoped to:

```text
session_token + terminal_id + browser sid
```

Terminal output and browser-visible text are display data only. They must not
create, approve, reject, pause, resume, or upgrade Agent actions.

## Client-to-Server Events

### `agent_attach`

Payload:

```json
{ "terminal_id": "main" }
```

Attaches the current browser sid to Agent state for a terminal. The initial mode
is `observe`.

### `agent_detach`

Payload:

```json
{ "terminal_id": "main" }
```

Invalidates Agent state for this sid and terminal. Pending actions are cancelled.

### `agent_mode_set`

Payload:

```json
{ "terminal_id": "main", "mode": "observe" }
```

Allowed client mode values:

- `disabled`
- `observe`
- `approval` or `approval_pending`
- `direct` or `direct_active`

Changing mode increments `control_epoch` and cancels pending actions.

### `agent_pause`

Payload:

```json
{ "terminal_id": "main" }
```

Hard-closes the backend Agent write gate for this sid and terminal. The server
sets mode to `paused`, increments `control_epoch`, clears pending actions, and
rejects stale writes.

### `agent_resume`

Payload:

```json
{ "terminal_id": "main", "mode": "observe" }
```

Resumes Agent state into `observe`, `approval_pending`, or `direct_active`.
Resume increments `control_epoch`.

### `agent_suggestion_request`

Payload for current mock bridge:

```json
{ "terminal_id": "main", "mock_input": "whoami\n" }
```

Creates a mock `terminal_input` action. In `approval_pending` mode, the server
emits an `agent_action_request`. In `direct_active` mode, the action is written
through `AgentInputGate`.

### `agent_action_approve`

Payload:

```json
{ "terminal_id": "main", "action_id": "..." }
```

Approves a pending action for this exact sid, terminal, and current
`control_epoch`. Approved input is written only through `AgentInputGate`.

### `agent_action_reject`

Payload:

```json
{ "terminal_id": "main", "action_id": "..." }
```

Rejects a pending action for this exact sid and terminal.

## Server-to-Client Events

### `agent_state`

Payload:

```json
{
  "terminal_id": "main",
  "mode": "observe",
  "paused": false,
  "control_epoch": 1,
  "run_id": "...",
  "pending_actions": 0
}
```

### `agent_action_request`

Payload:

```json
{
  "action_id": "...",
  "action_type": "terminal_input",
  "terminal_id": "main",
  "requires_approval": true,
  "status": "pending_approval",
  "control_epoch": 1,
  "run_id": "...",
  "byte_length": 7,
  "line_count": 1,
  "contains_control_chars": false,
  "ends_with_newline": true,
  "escaped_preview": "whoami\\n"
}
```

The exact `data` payload is intentionally not included in the public action
payload yet. The approval UI should display the escaped preview and safety
metadata until a final redaction policy is chosen.

### `agent_action_result`

Payload:

```json
{
  "action_id": "...",
  "action_type": "terminal_input",
  "terminal_id": "main",
  "status": "completed",
  "error_code": "agent_paused"
}
```

`error_code` is present for failed or cancelled operations.

## Important Error Codes

- `agent_not_attached`
- `agent_paused`
- `agent_stale_epoch`
- `agent_action_not_found`
- `agent_stale_action`
- `agent_action_not_writable`
- `terminal_not_found`
- `agent_invalid_mode`
- `agent_mode_not_writable`
- `agent_action_not_pending`

## Transcript Boundary

The backend keeps an internal sanitized transcript buffer for future Agent
context. It is copied from terminal output, stripped of ANSI/control sequences,
bounded in memory, and not exposed to the current UI or mock provider.
