# Agent Socket Contract

This document describes the internal Agent Socket.IO contract. The current UI
contains a mock Agent panel for exercising the typed contract; no external
provider is connected yet.

## Scope

Agent state is scoped to:

```text
session_token + terminal_id + browser sid
```

Terminal output and browser-visible text are display data only. They must not
create, approve, reject, pause, resume, or upgrade Agent actions.

The frontend may keep hidden Agent state for each terminal and may show a
bottom `[ PAUSE ]` control while structured Agent state says terminal input is
direct-active or has pending actions. It must base that control only on typed
`agent_state`, `agent_action_request`, and `agent_action_result` payloads, not
terminal display text.

The mock Agent panel may send `agent_mode_set`, `agent_suggestion_request`,
`agent_provider_run_request`, `agent_action_approve`, `agent_action_reject`,
and `agent_pause`. The approval panel must display only the public action
metadata returned by the backend, including `escaped_preview`; it must not
receive or render the raw terminal input payload.

The mock panel is opened manually from the status bar `Show Agent Panel` /
`Hide Agent Panel` toggle. A terminal connection must not automatically expand
the panel when Agent state is not attached.

The frontend may send xterm viewport snapshots as typed Agent context. Snapshot
text is untrusted display data and must not be used as a control signal.

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

### `agent_provider_run_request`

Payload:

```json
{ "terminal_id": "main" }
```

Runs the current backend mock provider against the internal Agent context
builder. The context builder can read the latest same-browser-sid viewport
snapshot, sanitized transcript events, and minimized human input metadata. The
current mock provider emits a fixed `terminal_input` proposal for local contract
testing; it is not an external LLM provider.

In `approval_pending` mode, the resulting action is emitted as
`agent_action_request`. In `direct_active` mode, it is written only through
`AgentInputGate`.

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

### `agent_viewport_snapshot`

Payload:

```json
{
  "terminal_id": "main",
  "cols": 80,
  "rows": 24,
  "viewport_y": 120,
  "base_y": 120,
  "snapshot_seq": 7,
  "captured_at": "2026-05-22T00:00:00.000Z",
  "lines": ["visible terminal line", "..."]
}
```

Stores the current xterm viewport for future Agent context. The snapshot is
scoped to the current browser sid, session token, and terminal id. The backend
accepts it only when the sid is currently attached to that terminal, validates
cols, rows, line count, monotonic `snapshot_seq`, and total byte limits, and
clears stored snapshots on terminal close, session close, or sid disconnect.

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

### `agent_viewport_snapshot_result`

Payload:

```json
{
  "terminal_id": "main",
  "status": "accepted",
  "snapshot_seq": 7,
  "cols": 80,
  "rows": 24,
  "line_count": 24,
  "byte_length": 1024
}
```

Rejected snapshots use `status: "failed"` or `status: "stale"` and include an
explicit `error_code`.

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
- `agent_snapshot_invalid`
- `agent_snapshot_too_large`
- `agent_snapshot_stale`

## Transcript Boundary

The backend keeps an internal sanitized transcript buffer for future Agent
context. It is copied from terminal output, stripped of ANSI/control sequences,
bounded in memory, and not exposed to the current UI or mock provider.

## Human Input Metadata Boundary

The backend also keeps an internal bounded metadata buffer for human terminal
input observed through the existing `ssh_input` event. This applies to SSH,
Local Shell, and UART terminals because they share that input event. Metadata is
recorded only after the current `ssh_input` session, terminal, bridge, type, and
size validation passes, immediately before the input is written to the terminal
bridge.

The buffer is keyed by `session_token + terminal_id`, has a TTL, and is cleared
when the terminal or session is closed. It stores only minimized metadata:
timestamp, terminal id, byte length, line count, whether control characters were
present, and a short escaped preview only when the payload has no unsafe control
characters. It does not store SSH password form values, access tokens, browser
authorization data, DOM/app state, or the full input payload.

## Viewport Snapshot Boundary

The viewport snapshot store is separate from terminal transcript and input
metadata stores. It is keyed by `session_token + terminal_id + browser sid`,
bounded by row, column, line-byte, total-byte, and TTL limits, and stores the
latest accepted snapshot only for that sid. Snapshot lines are terminal display
payload and remain data only.
