# IME input-line anchor: experimental PoC

Status: enabled as an experimental feature in Core 2.11.0 for user testing,
**not a qualified permanent fix**. Applies to Core terminals in both the browser and
Desktop. No CJK-width preference, remote shell, tmux, or screen setting is changed.

## Problem and proposed behavior

An agent/TUI can temporarily move a hidden terminal cursor to redraw a spinner
or status area. xterm 6.0.0 positions its composition overlay and hidden textarea
at that cursor. When output arrives in separate writes, a redraw can move the
overlay while the user is composing text. This reproduces with bundled xterm
alone; SSH and terminal multiplexers are not prerequisites.

The PoC saves the current buffer line marker and column on `compositionstart`.
During that composition it positions both elements at that input line, following
normal output/viewport scrolling and updated cell dimensions. It does not pause
terminal output or intercept text, selection, key handling, commit, or cancel.
Clipboard behavior is a separate Core correction.

The separate `static/js/standterm-ime-anchor-poc.js` addon wraps only one private
geometry method per terminal. Vendored `xterm.js` remains unchanged. Its geometry
is adapted from the MIT-licensed xterm 6.0.0 CompositionHelper; the existing xterm
license is retained under `static/licenses/`.

## Limits and fallback

- The starting cursor is assumed to identify the input position, including when
  hidden. If composition starts during a temporary TUI drawing operation, the
  saved starting position can be wrong. The PoC does not guess the prompt from
  output text or use a last-visible-cursor heuristic.
- Resize/reflow, switching buffers, a disposed marker, an offscreen anchor, or
  unavailable cell dimensions releases the anchor for the rest of that
  composition. Native behavior, including its positioning limitations, resumes.
  The next composition starts a fresh anchor.
- Blur clears the anchor. Composition end and terminal disposal release the
  marker and pending timer. Disposal also restores the wrapped method and removes
  listeners. Text submission remains owned by xterm.
- The adapter checks the APIs needed to activate; a missing optional asset/API
  leaves native positioning enabled. Private API semantic changes still require
  requalification when upgrading xterm.
- Faster Core output batching reduces redraw gaps, but does not by itself solve
  this positioning problem. Neither change solves reconnect replay duplication.

## Enable, compare, or withdraw

`IME_ANCHOR_POC_ENABLED` in `templates/index.html` is `true` for this PoC.
Set it to `false` and reload in an isolated test instance to compare native xterm
positioning. No new persistent user preference is introduced. Packaged Desktop
must be rebuilt to change its bundled Core; do not edit an installed runtime or
replace a live backend during active terminal work.

## Automated evidence

Run with a prepared project WSL venv and the repository's Playwright Chromium:

```sh
tools/.venv_wsl/bin/python tests/ime_anchor_browser_smoke.py
tools/.venv_wsl/bin/python tests/agent_browser_smoke.py
```

The standalone suite uses the actual bundled xterm and synthetic Chromium CDP
composition. Its A/B test checks intermediate overlay/textarea positions during
split redraws, including initially hidden cursors and the alternate buffer.
Other checks cover one-time commit, cancellation, a fresh next composition,
scrolling, changed font dimensions, resize/buffer/marker/offscreen fallback,
non-composing input placement, disposal with a pending update, and API fallback.
The Core browser suite verifies the adapter loads and a missing asset fails open.

These are DOM/input-path regressions, **not OS IME candidate-window tests**.

## Manual acceptance before promotion

Use Windows Bopomofo IME first, then repeat with the relevant macOS IME when a Mac
evaluation is available. Compare the same workload with native positioning.

1. At an idle prompt, compose, select a candidate, commit with Enter, cancel with
   Escape, and enter several consecutive words. Confirm no lost or doubled text.
2. Repeat while an agent redraws its status/spinner: direct local shell, SSH,
   SSH with tmux, and SSH with screen. Include a TUI that keeps its cursor hidden.
3. Start composing just as output begins. Record wrong starting positions
   separately from movement after composition has already started.
4. During composition, scroll, resize, change tabs, and return from a floating
   terminal. Confirm fallback remains usable and no text is submitted by those
   actions. Close a disposable composing tab to check lifecycle cleanup.
5. Record backend, shell/TUI/multiplexer versions, IME, renderer/font, and whether
   the candidate window or only the in-terminal composition text moved. Avoid
   screenshots containing tokens or private terminal output.

Promote only after manual typing and commit/cancel checks pass. If candidate
placement or text handling regresses, disable the PoC and retain the independently
validated latency and paste corrections.
