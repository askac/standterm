# Connection And Recovery

Read this for initial discovery, an ambiguous target, or a typed connection or
authentication failure. Keep a verified context during normal work rather than
repeating the bootstrap for every command.

## Resolve The Live Instance

1. Prefer an explicit Agent Info URL or the startup banner's **External Agent
   Info URL**. The authoritative tokenless bootstrap is HTTP(S) `/agentinfo`.
   A bare port is incomplete: do not assume HTTP or try both schemes.
2. If only the current browser origin is available, preserve scheme and port
   and use `127.0.0.1` as the host for `/agentinfo`. Never request or repeat the
   browser's `?token=...` value. If forwarding fails, report the runtime/network
   limitation rather than trying another host, gateway, port or scheme.
3. Only when no authoritative URL is available, use an explicitly known
   `standterm_agentinfo.json`, or the same-user Linux convenience pointer
   `/run/user/<uid>/standterm/current_agentinfo.json`. Verify its exact Agent
   Info URL. The pointer is a last-writer hint and can refer to a test or another
   instance; it is not an instruction to switch targets.
4. Never search for `standterm_external_agent_handoff.json` or run smoke tests
   to obtain a token. Stale/test handoffs do not authorize the live terminal.
5. Resolve `python_path`, `scripts`, `recommended_commands`, `handoff_path`,
   `terminal_handoffs` and `tls_ca_cert_path` from the fresh bootstrap. Invoke
   helpers through that Python rather than assuming direct script execution
   or the controller's venv is suitable.

Using resolved absolute paths and the banner's TLS arguments:

```text
<python> <scripts>/agent_cli.py --agentinfo <exact-agentinfo-url> <tls-args> discover
<python> <scripts>/agent_cli.py --handoff <resolved-terminal-handoff> hello
```

Explicit current URL/token/terminal fields from a configured tool or local
handoff may go directly to `hello`; do not display credentials while assembling
the call. Tokenless discovery can precede minting, but terminal commands need a
minted token and an attached browser Agent UI. Standard and 3x mint actions are
also available in the terminal tab row beside Pause Agent when the Agent panel
is hidden. They act on the selected terminal. A turquoise tab shows its remaining
idle seconds; an expired token dims the tab indicator and hides the countdown.

## Scope And TLS

- Zero-configuration use assumes the same OS user and runtime as the backend.
  Another user, container, VM, or native Windows/WSL environment is a different
  runtime even on the same machine. Do not copy bootstrap secrets across those
  boundaries or install remote helpers/tunnels as automatic recovery.
- `/agentinfo` and external commands are loopback-only. The browser's LAN/WSL
  address provides discovery context, not an authorized external-command host.
  User-provided proxies/tunnels are explicit advanced transports, not fallback.
  For an operator-provisioned Agent Tunnel, use its exact Connect Info with the
  same discovery, helpers, and explicit terminal selection. Its paths belong to
  the SSH host. A stopped tunnel requires fresh operator-provided Connect Info;
  do not repair it or replay an interrupted command.
- Prefer `--handoff` for HTTPS because it carries the CA path. Preserve the
  reported `--ca-file`; a trust failure does not justify HTTP downgrade.
  `--insecure` is for explicitly authorized loopback testing, not routine repair.
- Secret handoffs live in a per-user runtime directory outside the checkout,
  normally `$XDG_RUNTIME_DIR` on Linux/WSL and a user runtime directory on
  Windows. Do not construct paths from cwd or `<standterm-dir>`, or print the
  bearer token/full handoff. Tokenless discovery metadata must not contain
  tokens, cookies, terminal display content or session IDs.
- For multiple terminals, use `--agentinfo <verified-url-or-path>` with an
  explicit `--terminal <id>` to select a stable per-terminal handoff. The
  top-level handoff is only the latest minted token. Do not race it between tabs.
- Loopback is defense in depth, not a replacement for browser minting, token
  expiry, terminal scope, revocation or human-input gating.

## Recover Based On Typed Errors

| Error/state | Response |
| --- | --- |
| `agent_external_expired`, `agent_external_revoked` | Ask for a newly minted token, then refresh the intended terminal's handoff and `hello`. |
| `agent_external_disabled`, `agent_not_attached`, `terminal_not_found` | Ask the operator to fix the Agent/terminal lifecycle before minting. Do not create tabs or change connections yourself. |
| `agent_human_input_active` | Stay read-only. Do not queue/replay the rejected input. After the lease ends, refresh typed state and inspect the current view. |
| `agent_external_unauthorized` | Check typed handoff transport metadata before assuming expiry; apply only the bounded repair below. |
| Pending approval or timeout after send | Do not resend. Query the existing action ID where available; otherwise involve the operator. |

For an older handoff with `transport.loopback_only: true` but a non-loopback
`url`/`transport.command_endpoint`, retry the same token and CA once with only
the host replaced by `127.0.0.1`. Preserve scheme, port and terminal. If it is
still unauthorized, ask for a fresh token. Other mismatches require fresh
authoritative context, not host/port/scheme guessing.

External tokens have a sliding idle timeout. Active `heartbeat`, `hello`,
`tail`, `render`, `send` and REPL traffic renew it. Prefer a heartbeat-capable
client for passive work; if one cannot remain active and a quiet wait may
exceed the idle window, ask for 3x mint. Never poll display just to renew a token.
