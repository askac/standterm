# Work With tmux Over SSH

[Back to StandTerm](../../README.md)

Use tmux to keep a build, shell or TUI running on a remote host across SSH
disconnects. Use StandTerm to connect, view the terminal and optionally let an
authorized AI agent assist in the same visible session.

## Start or Attach

Connect to a host that already has tmux installed. At its shell prompt, run:

```bash
tmux new-session -A -s work
```

This attaches to the existing `work` session or creates it. Run your normal build
or diagnostic workflow inside that session.

With tmux's default bindings, press **Ctrl+B**, then **D**, to detach. After an
SSH disconnect, reconnect to the same host and account, then run the same command
to reattach. The remote host and tmux server must still be running; this does not
preserve jobs across a host reboot.

## Add AI Assistance

Follow the [agent setup guide](../user-guide.md#agent-and-external-agent-mirror)
and authorize the StandTerm tab. For passive build monitoring, choose
**Read only** in the Agent panel and give the agent a task such as:

> Watch the build output in this terminal. Summarize the first failure and the
> evidence around it. Do not type commands or restart the build.

Choose **Approval required** when the agent needs to propose terminal input.
StandTerm grants access to the terminal tab, not individual tmux panes; have the
agent confirm the active shell or TUI before proposing input. For workflows
across hosts, see [Compare two SSH hosts](ssh-cross-tab.md).

## What Each Tool Adds

tmux provides remote session persistence, panes and attach/detach. It also has
`capture-pane`, `send-keys` and a [control-mode API](https://github.com/tmux/tmux/wiki/Control-Mode),
so automation is already possible with tmux alone.

StandTerm adds a browser/Desktop interface, integrated file transfer and
per-tab agent permissions, input approval, pause and revocation. This example
uses ordinary tmux terminal output and input, rather than a dedicated tmux
control-mode integration. Files still refers to the SSH tab's connected host;
it does not follow a nested `ssh` command run inside a tmux pane.

If you only need session persistence and command scripting, tmux may already
cover your needs. The combination is useful when you also want visible,
controlled human/agent interaction around that workflow.
