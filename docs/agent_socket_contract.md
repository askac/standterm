# Agent Socket Contract

This document describes the internal Agent Socket.IO contract and the backend
External Agent Mirror command boundary. The current UI contains a mock Agent
panel for exercising the typed contract; no external provider is connected yet.

## Scope

Agent state is scoped to:

```text
session_id + terminal_id + viewer_id + agent_binding_id
```

The live backend still uses the authenticated session cookie and Socket.IO sid
internally. Agent/provider-facing context uses non-secret identifiers instead:
`session_id`, `viewer_id`, `agent_binding_id`, `run_id`, `proposal_id`, and
`mode_version`.

The target architecture follows a `screen -x` style shared terminal model:

- one PTY / terminal session;
- one authoritative terminal size;
- one terminal output stream with monotonically increasing `output_seq`;
- multiple viewers;
- multiple input producers.

Human input may write directly to the terminal bridge after existing validation.
Agent input must always be a typed action proposal and must pass the
approval/direct/pause gate before it can write to the terminal.

External agent mirror clients are secondary viewers/input producers for a
terminal the human viewer already connected. They are not providers and are not
raw telnet/SSH clients. They can see only the terminal state permitted by the
human-controlled Agent mode and privacy state, and their input is always a
typed `terminal_input` proposal.

Terminal output and browser-visible text are display data only. They must not
create, approve, reject, pause, resume, or upgrade Agent actions.

The frontend may keep hidden Agent state for each terminal and may show a
bottom `[ PAUSE ]` control while structured Agent state says terminal input is
direct-active or has pending actions. It must base that control only on typed
`agent_state`, `agent_action_request`, and `agent_action_result` payloads, not
terminal display text.

The mock Agent panel may send `agent_mode_set`, `agent_suggestion_request`,
`agent_provider_run_request`, `agent_action_approve`, `agent_action_reject`,
`agent_privacy_set`, and `agent_pause`. The approval panel must display only
the public action metadata returned by the backend, including `escaped_preview`;
it must not receive or render the raw terminal input payload.

The mock panel is opened manually from the status bar `Show Agent Panel` /
`Hide Agent Panel` toggle. A terminal connection must not automatically expand
the panel when Agent state is not attached.

The frontend may send xterm screen snapshots as typed Agent context. The
frontend now builds these snapshots from a dedicated hidden xterm mirror that
consumes the same terminal output stream as human viewers, rather than from the
human viewer scroll position. The backend also keeps a provisional headless
terminal grid from the same terminal output stream, so `screen` can return a
server-side display view when no browser snapshot is available. Snapshot and
headless-grid text are untrusted display data and must not be used as a control
signal.

## External Agent Mirror Boundary

The External Agent Mirror is the local CLI boundary for tools such as Codex CLI
or Claude Code. The current testable transport is a loopback-only HTTP command
bridge plus `scripts/agent_cli.py`. OS transports such as Unix domain
sockets or Windows named pipes can be layered on top of the same command
boundary later.

The human StandTerm viewer remains the controller:

- the external agent cannot create terminal connections;
- the external agent cannot read SSH passwords, Flask access tokens, browser
  cookies, Socket.IO sids, browser DOM state, paste dialogs, selection, or human
  scroll position;
- the external agent cannot approve its own proposal;
- the external agent cannot change Agent mode or resume/unpause itself;
- recent human terminal input creates a short backend lease that blocks Agent
  writes until it expires;
- closing the Agent panel by setting mode to `disabled` blocks external screen,
  tail, and send commands;
- `private_input`, `paste_review`, and `paused` block external screen, tail,
  and send commands.

Attach uses a separate high-entropy `agt_...` capability token minted by the
already attached human viewer for one terminal. Agent identifiers such as
`session_id`, `viewer_id`, and `agent_binding_id` are not secrets and are not
sufficient for attach authorization. Tokens use a sliding idle timeout, scoped
to the terminal and authorizing browser binding, and are invalidated by terminal
close, viewer detach/disconnect, session expiry, explicit revoke, or binding
changes. The default idle timeout is five minutes and can be changed with
`STANDTERM_AGENT_EXTERNAL_IDLE_TIMEOUT_SECONDS`.

The browser mints tokens through `POST /agent/external/token` using the current
authenticated StandTerm session cookie and public Agent state fields for the active
terminal. External clients submit commands through `POST /agent/external/command`,
which is accepted only from loopback clients and still requires the `agt_...`
token. When a token is minted, the server also writes the latest local handoff
JSON to `standterm_external_agent_handoff.json` in the StandTerm launch directory.
This ignored local file is only a convenience for CLI agents on the StandTerm host;
it does not bypass the short-lived token, loopback-only command endpoint, or
Agent panel mode gates. It is also the machine-readable discovery document for
non-StandTerm agents. It includes `handoff_schema:
"standterm_external_agent_handoff"`, `schema_version`, `protocol_version`,
`transport`, `capabilities`, operation templates, and ready-to-run CLI commands.
Because `/agent/external/command` only accepts loopback clients, the handoff
`url`, `transport.command_endpoint`, and generated CLI commands use a loopback
host even when the browser-facing StandTerm URL is a WSL or LAN address. The
browser-facing address is retained as `browser_url`.
Agents should call `hello` first when possible and branch only on the typed
`capabilities` field, not on displayed terminal text.
See `docs/examples/standterm-external-agent-skill/SKILL.md` and the adjacent
`skill_prompt.txt` for a local skill example that wraps this workflow for CLI
agents.

The command endpoint is loopback-only. If an external agent runs on another
machine, route it through an SSH tunnel or equivalent loopback tunnel to the
StandTerm host; do not expose the command endpoint or bearer token directly on a
network interface.

The CLI wrapper is intentionally small and speaks this JSON command contract.
It can read the generated handoff file directly. When StandTerm serves HTTPS with
its generated local development certificate, the handoff includes the local CA
path and the wrapper uses it for TLS verification.

```bash
tools/.venv_wsl/bin/python scripts/agent_cli.py \
  --handoff standterm_external_agent_handoff.json \
  hello
```

Or receive the connection fields explicitly:

```bash
tools/.venv_wsl/bin/python scripts/agent_cli.py \
  --url http://127.0.0.1:5010 \
  --token agt_... \
  --terminal main \
  send --text $'pwd\r'
```

CLI `--text` is sent verbatim. Normal quoted strings do not decode backslash
escapes, so `--text "pwd\n"` sends literal backslash and `n` bytes. In bash,
use `$'...'` to send a real control byte such as carriage return. On Windows
shells, prefer `--stdin` or the JSONL wrapper for portable line breaks.
PTY-style interactive programs usually expect carriage return (`\r`) for Enter.
For navigation-only input, the CLI also accepts repeated named keys such as
`send --key Down --key Enter`; these are converted client-side into terminal
control bytes before the same typed `send` command is posted.
The CLI also exposes generic automation aliases over the same protocol:
`key --key Down --key Enter` maps to `send --key ...`, `wait-output` maps to
long-poll `tail`, and `wait-quiet` maps to `screen --wait-ms --quiet-ms`. These
aliases are client-side conveniences; they do not add text-matching or branch on
terminal display payloads.

For terminal-like interaction, use the persistent REPL wrapper instead of
starting one CLI process per line:

```bash
tools/.venv_wsl/bin/python scripts/agent_repl.py \
  --handoff standterm_external_agent_handoff.json \
  --enter cr
```

The REPL keeps one local process alive, coalesces local keyboard input before
calling `send`, and renders remote output from long-poll `tail` using
`output_seq` as its cursor. Its attach banner includes typed token lifetime
metadata when the server provides it, such as remaining idle seconds. It also
runs a hidden `state` heartbeat by default to renew the external-agent token
without writing terminal input or terminal output; use `--keepalive-ms` or
`--no-keepalive` to tune it. `screen` is only a provisional initial
viewport/debug source; it is not the authoritative terminal stream. The local
detach key is `Ctrl-]`. In dev
servers started with `STANDTERM_AGENT_DEV_TOKEN=1`, the REPL may omit `--token` and
use the loopback-only dev command endpoint. `--enter cr` is the default because
PTY-style interactive programs generally expect carriage return for Enter; use
`--enter lf` for line-oriented shell pipe behavior and `--enter crlf` only for
targets that explicitly require both bytes. The REPL stops itself on fatal Agent
errors such as revoked/expired tokens, paused/privacy-blocked state, disabled
external access, or a missing terminal. A short human-input lease is transient:
the REPL reports `agent_human_input_active` but does not detach automatically.
Rejected input is not retried automatically, because replaying stale keystrokes
after the human lease expires can put bytes into the wrong prompt or editor
state.

For workflows that need one paced text entry before interactive follow-up, the
REPL can run `--type-text` or `--type-file` after attaching and then continue
the same live session. It uses the shared `scripts/agent_input.py` pacing
helpers and supports `--type-wait-quiet-ms` for a typed quiet-screen wait after
the paced input. Normal interactive REPL keystrokes are still raw/coalesced and
are not paced.

For paced input into a full-screen editor or TUI, use the dedicated typer helper
instead of REPL pipe mode:

```bash
tools/.venv_wsl/bin/python scripts/agent_type.py \
  --handoff standterm_external_agent_handoff.json \
  --from-file body.txt \
  --cps 3 \
  --newline cr
```

The typer posts one normal `send` operation per text unit and stops on rejected
input such as paused/privacy-blocked state, revoked tokens, missing terminals,
or active human-input leases. Its shared pacing helpers live in
`scripts/agent_input.py`; `agent_type.py` is the CLI wrapper. The default
cadence profile is generic. Use `--cadence-profile ptt` only when a target
application needs that optional whole-second cadence guard. The typer does not
provide an exclusive multi-character write lease. StandTerm terminal input is
one shared stream, so another writer can move the cursor or change editor state
between typed units. While a paced typer is running, avoid concurrent
cursor-moving input from browser viewers, CLI, REPL, JSONL, or other helpers.
Use `tail` for progress checks; `screen` remains a provisional browser snapshot
and `render` requires a live browser viewport.

For machine-to-machine repeated commands, use the persistent JSONL wrapper
instead of the terminal-style REPL. It keeps stdout as JSON only and still
forwards each command to the same loopback HTTP command endpoint:

```bash
tools/.venv_wsl/bin/python scripts/agent_jsonl.py \
  --handoff standterm_external_agent_handoff.json
```

Each stdin line is a JSON command object. The wrapper fills in the default
`token` and `terminal_id` from the handoff when omitted, preserves an optional
caller-supplied `id`, and returns one JSON response per stdout line:

```json
{"id":"1","op":"send-wait","data":"pwd\r","wait_ms":2000}
```

```json
{"id":"1","ok":true,"http_status":200,"result":{"status":"completed"}}
```

The JSONL wrapper is sequential in its first version. Long-poll commands such
as `send-wait` block the next stdin command until their HTTP response returns.
It must not print the bearer token or full handoff JSON.
JSONL `data` is JSON-decoded, so escapes such as `\r` and `\n` become real
control bytes before sending; this is intentionally different from raw CLI
`--text`.

### External Command Shape

The command boundary is JSON object based. Future IPC transports should carry
these objects as newline-delimited JSON or length-prefixed JSON; terminal output
text must not be parsed as control state.

Discover protocol/capabilities:

```json
{
  "op": "hello",
  "token": "agt_...",
  "terminal_id": "main"
}
```

`hello` returns `version`, `external_agent_id`, `terminal_id`, current public
Agent state, and a typed `capabilities` array such as `state`, `screen`,
`headless_screen`, `screen_wait`, `render`, `tail`, `send`, `send_capture`,
`submit_after`, `strip_ansi`, and `revoke`.

Attach:

```json
{
  "op": "attach",
  "token": "agt_...",
  "terminal_id": "main"
}
```

Read state:

```json
{
  "op": "state",
  "token": "agt_...",
  "terminal_id": "main"
}
```

`state`, `attach`, and the nested `hello.state` include a `terminal_session`
object when the terminal bridge is still present. It includes the current
`output_seq`, `last_output_at`, and `terminal_quiet_ms` so CLI clients can
distinguish an idle terminal from a slow task without scraping display text.
They also include `external_agent_token` with typed idle-timeout metadata:
`token_lifetime`, `idle_timeout_seconds`, `expires_at`, `last_used_at`, and
`remaining_idle_ms`. These fields are not secrets and do not include the bearer
token or token hash.
Because every valid command renews the token idle timeout, `state` is the
lightweight typed heartbeat for long local reasoning gaps. Use `tail` with
`wait_ms` instead when the caller also wants to wait for terminal output.

Read screen:

```json
{
  "op": "screen",
  "token": "agt_...",
  "terminal_id": "main"
}
```

`screen` returns the latest browser mirror viewport snapshot when available, or
a server-side headless terminal grid (`source: "server_headless_terminal_grid"`)
when no browser snapshot exists. Both sources are marked `provisional: true`:
they are useful display data for observation, but are not authoritative control
signals. The headless grid implements a small
VT/ANSI display subset for printable text, cursor movement, line clears, screen
clears, and scrolling; `render` remains the xterm/browser path when pixel-level
fidelity matters. To reduce repeated full viewport payloads, clients may
request a slice of the latest screen:

```json
{
  "op": "screen",
  "token": "agt_...",
  "terminal_id": "main",
  "tail_lines": 12
}
```

```json
{
  "op": "screen",
  "token": "agt_...",
  "terminal_id": "main",
  "region": {
    "top": 0,
    "bottom": 12
  }
}
```

`region.top` is inclusive and `region.bottom` is exclusive. Sliced responses
preserve screen metadata such as `source`, `provisional`, `snapshot_seq` when
present, `screen_seq`, `output_seq`, `rows`, and `cols`, and add `region`,
`original_line_count`, and `truncated` fields. `tail_lines` and `region` are
mutually exclusive. Diff reads are intentionally not defined yet; use `tail`
with `output_seq` for event-cursor reads.

Callers may ask `screen` to wait for a stable TUI display by passing `wait_ms`
and `quiet_ms`:

```json
{
  "op": "screen",
  "token": "agt_...",
  "terminal_id": "main",
  "wait_ms": 3000,
  "quiet_ms": 500
}
```

The server returns after the terminal has produced no output for `quiet_ms`, or
after `wait_ms` expires. Responses that requested waiting include a typed
`screen_wait` object with `wait_ms`, `quiet_ms`, `settled`, `timed_out`,
`terminal_quiet_ms`, and `output_seq`. This is only a timing contract for the
display stream; it is not a semantic prompt detector.

Read rendered xterm viewport:

```json
{
  "op": "render",
  "token": "agt_...",
  "terminal_id": "main",
  "wait_ms": 3000
}
```

The CLI wrapper can save the returned PNG directly:

```bash
tools/.venv_wsl/bin/python scripts/agent_cli.py \
  --handoff standterm_external_agent_handoff.json \
  render --save viewport.png
```

When `--save` is used, the CLI writes `render.image_base64` to the given path,
omits `image_base64` from stdout, and adds `render.saved_path` to the printed
JSON metadata.

`render` asks the authorizing browser viewer for a typed in-memory PNG capture
of the currently rendered xterm viewport. The server emits
`agent_viewport_render_request` to that browser sid, waits up to `wait_ms`, then
returns the browser's `agent_viewport_render_result`. The image bytes are
returned only in the command response as `render.image_base64`; audit records
store only metadata such as `request_id`, dimensions, byte length, `output_seq`,
and status.

```json
{
  "status": "ok",
  "terminal_id": "main",
  "external_agent_id": "exa_...",
  "render": {
    "request_id": "agrv_...",
    "terminal_id": "main",
    "render_type": "xterm_viewport",
    "mime_type": "image/png",
    "image_base64": "...",
    "image_byte_length": 12345,
    "cols": 80,
    "rows": 24,
    "pixel_width": 1024,
    "pixel_height": 640,
    "output_seq": 130,
    "captured_at": "2026-05-22T00:00:00.000Z"
  }
}
```

`render` follows the same visibility and privacy gates as `screen` and `tail`:
disabled, paused, `private_input`, and `paste_review` states block the request.
The rendered image is display data only; clients must not parse image contents
as control state.

Tail terminal display events:

```json
{
  "op": "tail",
  "token": "agt_...",
  "terminal_id": "main",
  "since_output_seq": 123,
  "limit": 50,
  "wait_ms": 25000
}
```

Add `"strip_ansi": true` only when the caller wants a plain display-data view
of terminal events. Raw terminal bytes remain the default. Plain output has
ANSI/control sequences removed and `\r` normalized to `\n`; it is still display
data and must not be used as a StandTerm control signal.

Tail returns a structured cursor and retention contract:

```json
{
  "status": "ok",
  "terminal_id": "main",
  "external_agent_id": "exa_...",
  "output_seq": 130,
  "since_output_seq": 123,
  "limit": 50,
  "wait_ms": 25000,
  "first_available_output_seq": 81,
  "dropped_before_output_seq": 80,
  "gap": {
    "detected": false,
    "from_output_seq": null,
    "to_output_seq": null,
    "missing_count": 0
  },
  "events": []
}
```

If `since_output_seq` is older than the retained replay buffer, `gap.detected`
is `true` and the `from_output_seq` / `to_output_seq` range describes terminal
events that are no longer available. When more than `limit` events are available,
tail returns the earliest page after `since_output_seq`, so clients can advance
from the last returned event and call `tail` again without skipping retained
events. `wait_ms` is optional. When it is positive and no retained events are
available yet, the server may hold the request until new terminal output arrives
or the wait expires. The response payload shape is identical for immediate and
long-poll tail calls. When `strip_ansi` is requested, the response includes
`"strip_ansi": true` and `"data_format": "plain"`, and each returned event's
`data` field is the stripped text. Clients must not infer control state from
terminal text.

Propose terminal input:

```json
{
  "op": "send",
  "token": "agt_...",
  "terminal_id": "main",
  "data": "pwd\r"
}
```

`send` in `approval_pending` mode returns public pending action metadata and
emits `agent_action_request` to the authorizing human browser. `send` in
`direct_active` mode still writes only through `AgentInputGate`. `send` in
`observe`, `disabled`, paused, or privacy-blocked states returns a typed error.
For full-screen TUIs that treat glued text plus `\r` as paste content, callers
may send text with `"submit_after": true`; the backend then writes the text and
a separate carriage return keypress as one structured action:

```json
{
  "op": "send",
  "token": "agt_...",
  "terminal_id": "main",
  "data": "codex prompt",
  "submit_after": true
}
```

Clients may request an atomic send-and-observe operation by adding
`"capture": true` to `send`, or by using `op: "send-wait"` / the `send-wait`
CLI alias. The first supported capture mode is tail-based and uses the
terminal `output_seq` just before the direct write as the cursor:

```json
{
  "op": "send",
  "token": "agt_...",
  "terminal_id": "main",
  "data": "pwd\r",
  "capture": true,
  "wait_ms": 3000,
  "settle_ms": 150,
  "limit": 50,
  "strip_ansi": true
}
```

In `direct_active` mode, a captured response keeps the normal send status and
adds typed observation metadata:

```json
{
  "status": "completed",
  "terminal_id": "main",
  "bytes_written": 4,
  "before_output_seq": 10,
  "after_output_seq": 12,
  "capture": {
    "requested": true,
    "status": "ok",
    "mode": "tail",
    "before_output_seq": 10,
    "output_seq": 12,
    "since_output_seq": 10,
    "wait_ms": 3000,
    "settle_ms": 150,
    "settled": true,
    "timed_out": false,
    "gap": { "detected": false },
    "events": []
  }
}
```

If no terminal output arrives before `wait_ms`, the send may still be
`completed`; the timeout is reported only as `capture.status: "timeout"` and
`capture.timed_out: true`. In approval mode, capture is not executed because no
bytes have been written yet; the response remains `pending_approval` and
includes `capture.status: "skipped"` with reason `pending_approval`. Captured
tail events are display data only and must not be parsed as StandTerm control
state. `strip_ansi` affects only the captured `events[*].data` formatting and
adds `capture.strip_ansi: true` plus `capture.data_format: "plain"`; raw capture
events remain the default. Capture returns terminal output after
`before_output_seq`; it does not prove that every returned byte was causally
produced by the sent input.

Revoke:

```json
{
  "op": "revoke",
  "token": "agt_...",
  "terminal_id": "main"
}
```

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

### `agent_privacy_set`

Payload:

```json
{ "terminal_id": "main", "privacy_state": "private_input" }
```

Allowed privacy states:

- `normal`
- `private_input`
- `paste_review`
- `paused`

`private_input`, `paste_review`, and `paused` block Agent context/run creation
and Agent terminal writes. Human terminal input still goes to the terminal, but
metadata captured while privacy is not `normal` is redacted and does not include
`escaped_preview`. Privacy changes increment `privacy_version` and cancel open
proposals. `paused` also closes the Agent write gate.

The frontend uses `paste_review` for large or multiline paste input. It sets
privacy to `paste_review`, shows a review dialog, sends the paste through
`ssh_input` only after explicit approval, and returns privacy to `normal` after
approval or cancellation when the terminal is still connected and not paused.

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

Runs the configured backend Agent provider against the internal Agent context
builder. The provider receives only the bounded context returned by
`build_agent_context()` and a run metadata object; it must not access Socket.IO,
terminal bridges, or terminal transport directly. The context builder can read
the latest same-browser-sid viewport snapshot, sanitized transcript events, and
minimized human input metadata.

The default provider is `mock`, which emits a fixed `terminal_input` proposal
for local contract testing. A second `static_env` adapter is available only when
explicitly selected with `STANDTERM_AGENT_PROVIDER=static_env` and
`STANDTERM_AGENT_STATIC_INPUT`; it is intended for adapter wiring tests, not as an
LLM integration.

In `approval_pending` mode, the resulting action is emitted as
`agent_action_request`. In `direct_active` mode, it is written only through
`AgentInputGate`.

### `agent_action_approve`

Payload:

```json
{
  "terminal_id": "main",
  "action_id": "...",
  "proposal_id": "agp_...",
  "session_id": "ags_...",
  "viewer_id": "agv_...",
  "agent_binding_id": "agb_...",
  "mode_version": 1,
  "privacy_version": 0
}
```

Approves a pending action for this exact sid, terminal, and current
`control_epoch`/`mode_version`/`privacy_version`. Approved input is written only
through `AgentInputGate`.

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
  "output_seq": 42,
  "captured_at": "2026-05-22T00:00:00.000Z",
  "lines": ["visible terminal line", "..."]
}
```

Stores the current frontend Agent mirror screen for future Agent context. The
event name is retained for compatibility with the earlier viewport adapter. The
snapshot is scoped to the current browser sid, session token, and terminal id.
The backend accepts it only when the sid is currently attached to that terminal,
validates cols, rows, line count, monotonic `snapshot_seq`, non-negative
`output_seq`, and total byte limits, and clears stored snapshots on terminal
close, session close, or sid disconnect.

### `agent_viewport_render_result`

Payload:

```json
{
  "request_id": "agrv_...",
  "terminal_id": "main",
  "render_type": "xterm_viewport",
  "mime_type": "image/png",
  "image_base64": "...",
  "cols": 80,
  "rows": 24,
  "pixel_width": 1024,
  "pixel_height": 640,
  "output_seq": 42,
  "captured_at": "2026-05-22T00:00:00.000Z"
}
```

Sent by the browser only in response to a matching
`agent_viewport_render_request`. The backend validates request id, terminal id,
render type, MIME type, terminal dimensions, pixel limits, PNG base64 size, and
current Agent privacy/mode gates before releasing the result to the waiting
external command.

## Server-to-Client Events

### `agent_viewport_render_request`

Payload:

```json
{
  "request_id": "agrv_...",
  "terminal_id": "main",
  "render_type": "xterm_viewport",
  "mime_type": "image/png",
  "session_id": "ags_...",
  "viewer_id": "agv_...",
  "agent_binding_id": "agb_...",
  "mode_version": 1,
  "privacy_version": 0,
  "cols": 80,
  "rows": 24,
  "output_seq": 42,
  "created_at": 1779465600.0
}
```

Requests one browser-rendered xterm viewport PNG for a waiting external
`render` command. The browser must answer with `agent_viewport_render_result`
using the same `request_id`.

### `agent_state`

Payload:

```json
{
  "session_id": "ags_...",
  "viewer_id": "agv_...",
  "agent_binding_id": "agb_...",
  "terminal_id": "main",
  "mode": "observe",
  "paused": false,
  "control_epoch": 1,
  "mode_version": 1,
  "privacy_state": "normal",
  "privacy_version": 0,
  "run_id": "...",
  "human_activity_seq": 0,
  "human_activity_at": null,
  "human_input_lease_expires_at": null,
  "human_input_lease_active": false,
  "pending_actions": 0
}
```

### `agent_action_request`

Payload:

```json
{
  "action_id": "...",
  "proposal_id": "agp_...",
  "action_type": "terminal_input",
  "session_id": "ags_...",
  "viewer_id": "agv_...",
  "agent_binding_id": "agb_...",
  "terminal_id": "main",
  "requires_approval": true,
  "status": "pending_approval",
  "control_epoch": 1,
  "mode_version": 1,
  "privacy_state": "normal",
  "privacy_version": 0,
  "run_id": "...",
  "provider_name": "mock",
  "provider_version": "1",
  "provider_status": "completed",
  "byte_length": 7,
  "line_count": 1,
  "contains_control_chars": false,
  "ends_with_newline": true,
  "escaped_preview": "whoami\\n"
}
```

The exact `data` payload is intentionally not included in the public action
payload. The approval UI should display the escaped preview and safety metadata.
Approval may use `action_id` for compatibility, but should also echo
`proposal_id`, `session_id`, `viewer_id`, `agent_binding_id`, `mode_version`,
and `privacy_version` when available so the backend can reject stale or
cross-viewer approvals.
Provider metadata is present for provider-created actions and is safe to expose;
the raw provider context and terminal input `data` are not included.

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
- `agent_privacy_blocked`
- `agent_stale_mode_version`
- `agent_stale_proposal`
- `agent_provider_unavailable`
- `agent_provider_failed`
- `agent_provider_timeout`
- `agent_provider_invalid_proposal`
- `agent_external_unauthorized`
- `agent_external_expired`
- `agent_external_revoked`
- `agent_external_disconnected`
- `agent_external_origin_blocked`
- `agent_external_disabled`
- `agent_human_input_active`

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

Validated human input also updates typed Agent state for all Agent viewers on
the same `session_id + terminal_id` with `human_activity_seq`,
`human_activity_at`, `human_input_lease_expires_at`, and
`human_input_lease_active`. While that short terminal-scoped lease is active,
Agent terminal input proposals and external `send` commands fail with
`agent_human_input_active`; terminal display text is not involved in this
decision. Human and Agent writes are serialized through the terminal bridge
input lock so the lease check and write order stay consistent.

The buffer is keyed by `session_token + terminal_id`, has a TTL, and is cleared
when the terminal or session is closed. It stores only minimized metadata:
timestamp, terminal id, byte length, line count, whether control characters were
present, and a short escaped preview only when the payload has no unsafe control
characters. It does not store SSH password form values, access tokens, browser
authorization data, DOM/app state, or the full input payload.

When privacy is `private_input`, `paste_review`, or `paused`, input metadata
keeps only minimized counts plus privacy/redaction metadata and does not store
an escaped preview.

## Agent Audit Boundary

The backend keeps an internal bounded structured audit buffer keyed by
`session_token + terminal_id`. Audit entries use non-secret identifiers such as
`session_id`, `viewer_id`, `agent_binding_id`, `run_id`, `proposal_id`,
`provider_name`, `provider_version`, external agent ids, and version fields.
They record typed events for viewer attach/detach, mode and privacy changes,
provider run requests/start/complete/error, external attach/screen/tail/send,
context metadata summaries, proposal creation, approvals/rejections, direct
writes, action results, and terminal cleanup.

Audit entries must not store raw terminal input, raw terminal output, SSH
passwords, access tokens, browser authorization material, or DOM/app state.
Action audit metadata is based on public action fields and excludes the raw
`data` payload.

## Viewport Snapshot Boundary

The viewport snapshot store is separate from terminal transcript and input
metadata stores. It is keyed by `session_token + terminal_id + browser sid`,
bounded by row, column, line-byte, total-byte, and TTL limits, and stores the
latest accepted snapshot only for that sid. Snapshot lines are terminal display
payload and remain data only.

## Agent Terminal Mirror Direction

The frontend dedicated xterm mirror feeds the backend's provisional snapshot
adapter, and the server-side headless grid provides a fallback when no browser
snapshot is active. The backend mirror boundary is:

```text
AgentTerminalMirror.get_active_screen(session_token, terminal_id, viewer_sid)
```

The adapter returns the latest same-browser-sid snapshot when present; otherwise
it returns the headless grid plus metadata such as `cols`, `rows`, `output_seq`,
and cursor position. It is intentionally marked provisional because the headless
grid is a smaller display parser than xterm.js. It consumes the same terminal
output stream as human viewers, uses the authoritative PTY size, and is not
affected by human scroll position or DOM selection.
