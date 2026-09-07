# Terminal Input And Observation

Read this when sending keys or commands, handling a TUI, selecting a render
mode, or planning output continuation. Paths below come from verified agentinfo.

## Send Once, Observe Once

When `hello` advertises `send_capture`, use `send-wait` (or `send --capture`) to
combine input and bounded observation. In a known shell, this Bash example
sends a real carriage return:

```bash
<python> <scripts>/agent_cli.py --handoff <handoff> send-wait --text $'pwd\r' --strip-ansi
```

CLI `--text` sends bytes verbatim: `"pwd\r"` in ordinary shell quoting does not
necessarily contain Enter. On Windows shells, prefer `agent_shcmd.py` for a
known-shell command, or JSONL for portable control bytes. JSONL decodes `\r`;
interactive PTY programs usually expect CR for Enter. `--stdin` is available
when the selected helper supports it; check its help rather than invent flags.

Named keys do not depend on shell escape quoting:

```text
<python> <scripts>/agent_cli.py --handoff <handoff> key --key Down --key Enter --capture
```

`send --key` is equivalent. Track the current application view before choosing
keys: the same input can navigate, edit, confirm, or execute in different views.

- Inspect both send status and `capture.status`. Pending approval skips capture
  because input has not been written. Query the original `action_id` with
  `agent_cli.py action-status --action-id <id>` when available; do not resubmit.
- A successful send reports terminal input delivery, not shell command success.
  Capture timeout/quietness and raw shell markers are not command exit status.
- Full capture retains `next_since_output_seq`, `more_available` and `gap`.
  Continue from `capture.next_since_output_seq`, not the terminal's latest
  `output_seq`/`after_output_seq`, when paging matters.
- `agent_shcmd.py --json` is useful for small one-shot checks, but currently
  omits continuation/approval fields. Use `--full-json` before sending if those
  fields are needed. Its `stdout` is captured terminal display, including possible
  echo/prompts, not a true stdout/stderr split. Its process exit code is not the
  remote command's exit code.

## Long-Running Commands And Paging

For continuous output or long builds, do not rely on a send-capture quiet window
to identify completion. Record the current typed output cursor with an
observation, send the authorized input once without capture, then use incremental
tail/REPL monitoring. A capture call may remain busy while output keeps arriving.

```text
<python> <scripts>/agent_cli.py --handoff <handoff> tail --since <cursor> --limit 20 --wait-ms 25000 --strip-ansi
```

Advance only to `next_since_output_seq`; when `more_available` is true, fetch
the next page if needed. `gap.detected` means some history has expired. Report
the gap and inspect the current screen if useful; do not claim lossless output
or rerun a mutating command to recover missing logs. A reconnect/new instance
or replaced terminal invalidates assumptions about an old cursor.

If earlier history is intentionally irrelevant, a current observation's typed
`output_seq` can establish a new baseline. This deliberately skips history;
it must not be described as reading all prior output. `--since 0` is an explicit
retained-history read, not the default for every polling iteration.

For a typed wait without full observation:

```text
<python> <scripts>/agent_cli.py --handoff <handoff> wait-output --since <cursor> --wait-ms 25000
<python> <scripts>/agent_cli.py --handoff <handoff> wait-quiet --wait-ms 3000 --quiet-ms 500
```

These report activity or bounded quietness only. Use the monitoring reference
for hidden heartbeat during long waits; do not keep tokens alive with repeated
full-screen reads.

## Viewports, TUIs And Images

Use text first, sized to the next decision:

```text
<python> <scripts>/agent_cli.py --handoff <handoff> screen --tail-lines 12
<python> <scripts>/agent_cli.py --handoff <handoff> screen --region 0:12
```

Regions are zero-based, bottom-exclusive. Expand when relevant content is
outside the slice. ANSI-stripped tail can be noisy or misleading for TUIs:
it loses cursor movement/highlight clues and is not a reconstructed viewport.
Use `screen`, raw events or a rendered image when those clues matter.

```text
<python> <scripts>/agent_cli.py --handoff <handoff> render --mode mirror-screen
<python> <scripts>/agent_cli.py --handoff <handoff> render --mode visible-xterm-png --save viewport.png
```

`mirror-screen` is structured, headless-safe screen data. `visible-xterm-png`
needs an authorizing browser viewer and returns an image: use `--save` to avoid
printing base64 into model context. Inspect typed `render.source`:

- `visible_xterm_dom`: foreground pixel-fidelity capture.
- `terminal_mirror_canvas`: valid background-safe capture; glyph antialiasing
  may differ. Do not retry or ask the user to foreground a tab just for this.

For `agent_render_timeout`, `agent_render_stale`, or `agent_render_not_visible`,
fall back to text/`mirror-screen` unless an image is actually required. Do not
request images after every command. Screen snapshots are observations, not
exclusive input locks or command-completion signals.
