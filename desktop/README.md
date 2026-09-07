# StandTerm Desktop evaluation

This Electron evaluation owns a Python backend and opens the existing StandTerm
UI without asking the local operator to paste an access token. It supports a
source-run workflow and an unsigned Windows x64 evaluation installer. It is not
a production release or a replacement for `run.sh` / `run.bat`.

## Windows x64 evaluation installer

The installer includes Electron and a SHA-256-manifested snapshot of the public
Core runtime files. It does not include Python, a venv, credentials, profiles,
developer checkouts, unpublished rescue tools or Git history. End users do not
need Node.js, npm or Git. The 0.3.0 NSIS assisted installer lets the user select
**Windows only**, **Windows + WSL**, or **WSL only**. It creates only the selected
shortcuts on the Desktop and Start menu, with independent backend modes:

| Shortcut | Core environment | Prerequisite |
| --- | --- | --- |
| StandTerm Desktop | Native Windows (`--backend=windows`) | Installed 64-bit Windows Python 3.10+ with venv/ensurepip |
| StandTerm Desktop (WSL) | Selected WSL distribution (`--backend=wsl`) | Existing WSL distribution with Python 3.10+ and venv/ensurepip |

Windows mode probes installed `python.exe` paths or offers manual selection.
It does not launch Microsoft Store aliases or `py.exe` to install Python.
Installing Windows Python does not satisfy WSL mode. For Ubuntu/Debian, the
user can run `sudo apt install python3 python3-venv` in WSL. StandTerm explains
these prerequisites but never runs sudo, installs Python or provisions WSL.
Neither mode silently falls back to the other when a prerequisite is missing.

During installation (before the successful Finish page):

1. Locate Windows Python or select an existing WSL distribution, depending on
   the shortcut used.
2. Explicitly approve copying Core, creating a private venv and downloading and
   installing Python dependencies. Package installation can execute code.
3. Wait for copy, venv, dependency installation and verification stages.
4. For Windows + WSL, finish both environments before publishing shortcuts.

The Electron interface always runs on Windows. WSL mode executes the prepared
Core and venv inside the selected distribution, not under `/mnt/c`. The Windows
application directory retains the bundled source snapshot used to prepare either
environment. Both-mode installation creates two separate runtime environments;
it does not make a Windows shell run inside WSL.

Core and its venv live outside the Electron installation directory:

- Windows: `%LOCALAPPDATA%\StandTermDesktop\runtimes\<bundle-sha256>\`,
  with `tools\.venv_win\Scripts\python.exe`.
- WSL: `~/.local/share/standterm-desktop/runtimes/<bundle-sha256>/`,
  with `tools/.venv_wsl/bin/python` inside the selected distribution.

Later launches verify Core hashes and imports and reuse that runtime, without running
pip again. A changed bundled Core gets a separate runtime and a new installation
confirmation; existing runtimes are not deleted or Git-updated. Python
dependencies currently resolve from the bundled `requirements.txt` on first
installation; the complete dependency set is not yet release-locked.

Failed setup leaves its managed files and `setup.log` for diagnosis/retry.
Closing setup or quitting during preparation asks for confirmation, defaulting
to **Keep preparing**. Minimizing continues setup. Confirmed cancellation keeps
the progress window open until its owned installation process tree stops: a
Windows Job Object or a POSIX process group with bounded TERM/KILL cleanup.
The indeterminate progress indicator is not a completion percentage.
Modified Core files or
unrelated directories are not overwritten. Only the selected Python path or
distro is saved to `%APPDATA%\StandTermDesktopEvaluation\<windows|wsl>\launcher.json`;
no secrets are stored there. Removing that mode's preferences file while Desktop
is closed resets its selection. The WSL-only 0.1.0 distro preference is imported
when available; no browser credentials are migrated. Each mode has an independent
profile and single-instance lock, so both can run together.

NSIS installs per user without requesting elevation and provides an uninstall
entry. It does not auto-launch a default backend when installation finishes.
Shortcut creation/update completes before the installer helper exits.
Only shortcuts with matching owned targets and arguments are updated or removed;
unrelated launchers are preserved. Existing per-mode launcher preferences retain
their `StandTermDesktopEvaluation` profile path. The new executable is
`StandTermDesktop.exe`; the NSIS application identity is `org.standterm.desktop`.
This evaluation is **unsigned**; publisher trust/SmartScreen acceptance is not
yet production-qualified. Save work and quit Desktop, including tray windows,
before changing or removing the installation. Setup refuses to terminate running
Desktop processes. There is no automatic update service, and silent installation
is unsupported because environment selection/consent is required.

The setup helper holds a Windows process handle to the owning installer. Loss of
that installer cancels preparation; loss of the helper closes the bootstrap pipe
and stops its owned dependency-installation children. Failure/cancellation retains
application files and partial environments for a retry; it is not a rollback of
already completed work. The installer does not report successful completion.
Normal launch still offers preparation if a selected runtime was later removed.

### Migrating the 0.1.x / 0.2.x Squirrel evaluation

The NSIS candidate refuses installation while the old Squirrel registration,
running process or an `app-*\StandTermDesktopEvaluation.exe` remains under
`%LOCALAPPDATA%\StandTermDesktopEvaluation`. Since 0.3.1, residual `Update.exe`,
`.dead` and version folders without the application executable do not block
installation and are left untouched. Quit the old
Desktop and manually uninstall its interface through Windows Settings first;
retain environments and launcher settings, then run the new installer. No old
uninstall command is executed automatically, and no Git checkout is migrated.
If the old entry is gone but legacy files remain, inspect them before removing
anything. Do not use a live installation as an isolated lifecycle test.

### Optional environment cleanup

Uninstall **retains environments by default**. An unchecked optional component
can move verified, idle venvs to a recoverable folder. Internal NSIS upgrades and
silent uninstall never select that cleanup. Core files, launcher settings,
captures, system Python, WSL distributions and other projects are never cleanup
targets. Only Windows and the currently configured WSL distribution are checked;
unavailable interpreters/distributions and older selections are retained.
Before any move, a read-only inventory lists exact venv paths and the Windows/WSL
distribution context for a second confirmation, defaulting to keeping them.
Only the confirmed runtime IDs can be moved; paths are resolved by the helper
under its fixed managed root. Inventory is capped at 32 runtimes per environment
and a bounded response size; exceeding either limit causes no moves. An interrupted
or unconfirmed result is reported as unknown, never as proof of retention.

New managed environments opt into a versioned lifetime-lease protocol. Setup,
backend lifetime, probes and cleanup use the same exclusive lock. A busy or
legacy runtime, invalid owner marker, directory link/junction, open Windows file
or unavailable cleanup interpreter causes retention, not forced removal. The
helper never removes the venv from which that helper itself is running.

Recovery locations:

- Windows: `%LOCALAPPDATA%\StandTermDesktop\venv-recovery\<unique-id>\`.
- WSL: `~/.local/share/standterm-desktop/venv-recovery/<unique-id>/`.

Each contains the detached `.venv_win` or `.venv_wsl` and `restore.json` recording
its original path. **This detaches the environment; disk space is not freed.**
To restore it, quit that Desktop mode and move the venv back to the recorded path
only if the destination is absent. Do not merge it with a newly created venv.
Permanent disposal of recovery files is a separate, explicit user action.

### Build on Windows

Build tools need Windows Node.js 22.12+ and Git. From the repository:

```powershell
node desktop/stage-windows.cjs
# Change to the exact desktop/dist/windows-build-* path printed above.
cd <printed-build-directory>
npm ci
npm run make:win
```

The staging script creates a new directory each time, using a runtime filter over
`git ls-files` plus explicit desktop-shell/shared-lease file lists. Runtime contents include
the tracked working-tree changes, so this is an evaluation snapshot, not a claim
that uncommitted changes are already a GitHub release. Build staging never copies
the development venv or `node_modules`. The new directory gets Windows build
dependencies; the source checkout's Linux/WSLg `node_modules` is untouched.

The `StandTerm-Desktop-0.4.1-win32-x64-Setup.exe` is under `out/`; the unpacked
application is under `out/win-unpacked/`. Packaging uses
[electron-builder's assisted NSIS target](https://www.electron.build/nsis.html),
with pinned build dependencies and scoped custom installer hooks. Squirrel
packages and `RELEASES` metadata from older evaluations are not NSIS updates.

## Launcher role and remaining integration

Electron is a peer of the batch/shell shortcuts: another launcher/client for
the same Core, not a separate Core implementation. The existing source-run
prototype below still owns its backend. On first launch it requests an automatic
loopback port and remembers it only after authenticated instance verification.
Later launches reuse it. An occupied saved port prompts **Cancel**, **Use once**,
or **Use and remember**; the last choice is saved only after successful startup.
No fixed first-run port is reserved, and no existing service is stopped or reused.
Failed/canceled startup does not change the saved port. A settings-write failure
warns but allows the current launch to continue.
Since 0.3.3, Windows/WSL mode also probes the Windows loopback port. This detects
Windows-reserved ranges and host-side conflicts that Linux binding cannot see.
An unavailable saved port offers a verified replacement with the same three
choices. Automatic candidates rejected by Windows are stopped and retried (at
most 20 host failures); Core's existing candidate exclusions remain in force.
An already-created WSL relay is accepted only after authenticated instance
verification. No Windows reservation, firewall rule or WSL configuration is changed.
Intentional candidate shutdown does not display an unexpected-backend-exit error.
Core's shared selection policy uses `49152–65535`, excludes its built-in fixed-use
list and the backend OS services file's TCP entries before attempting a bind,
and tries at most 20 candidates. The list and source dates are maintained in
`server_startup.py`; no online registry lookup is needed. An unreadable services
file emits a Python warning and falls back to the range plus built-in exclusions.
Windows Core reads Windows services; WSL Core reads its distribution's services.
This does not promise availability on the Windows forwarding side of WSL:
authenticated instance verification must still succeed there.

- Core shortcuts store their port in `tools/launcher-settings.json`. Desktop
  shares Core's bind/conflict helpers but stores only `{version, port}` in
  `port-windows.json` or `port-wsl.json` under its own user-data directory, outside
  the versioned managed Core. Port preferences contain no tokens. Packaged modes
  have separate profiles; neither reads or modifies the Core shortcut setting.
- Both Windows shortcuts use one Electron application with explicit mode flags.
  The WSL shortcut selects a user-approved distro. Detecting WSL does not
  authorize provisioning it.
- Verify both runtime identity and authentication before attaching to an
  existing Core. An occupied port must not silently switch Windows/WSL modes.
- Keep Git updates for source-managed Core checkouts. A packaged distribution
  should update its managed Electron/Core/Python dependencies as a compatible
  set, without running Git updates in a separately managed checkout.
- Restart or replace a running Core only with explicit operator approval;
  active terminals and file transfers must not be interrupted automatically.

## Run

Prepare the normal platform Python venv with the repository launcher first.
After a requirements change, rerun it with `--force`. Stop that launcher when
finished preparing it. The desktop process starts its own backend on an
remembered loopback port (automatically selected on first launch); it does not attach to an
existing browser instance. Automated smoke runs use disposable random ports and
isolated profiles, without reading or writing the operator's port preference.
They exercise the real port startup workflow. In smoke mode only,
`STANDTERM_DESKTOP_TEST_PORT` seeds the disposable profile with an unavailable
port, automatically approves its replacement and asserts that it is verified
and remembered. It never changes the installed launcher's preference.

Install Node.js 22.12+ (prefer a supported LTS runtime), then run from `desktop/`:

```sh
npm ci
npm start
```

The default Python paths are `tools/.venv/Scripts/python.exe` on Windows,
`tools/.venv_macos/bin/python` on macOS, `tools/.venv_linux/bin/python` on Linux,
and `tools/.venv_wsl/bin/python` when running Electron inside WSLg. Set
`STANDTERM_DESKTOP_PYTHON` to an explicit prepared venv executable if needed.
Source-run mode never installs Python packages. Packaged first-run setup uses
the separate, explicitly approved managed runtime described above.

For a Windows-native window with a WSL backend, use Windows PowerShell and a
Windows Node.js installation:

```powershell
$env:STANDTERM_DESKTOP_WSL_DISTRO = 'Ubuntu'
$env:STANDTERM_DESKTOP_WSL_REPO = '/mnt/d/workspace/github/standterm'
cd D:\workspace\github\standterm\desktop
npm ci
npm start -- --backend=wsl
```

Optional `STANDTERM_DESKTOP_WSL_PYTHON` selects the Linux venv executable.
Windows-to-WSL localhost forwarding must work. Failure produces an error; the
prototype does not fall back to binding a LAN address. `node_modules` contains
platform-specific Electron binaries: run `npm ci` again when switching the same
checkout between Windows and WSL/Linux.

For the local evaluation prepared on 2026-09-07,
`desktop/dist/StandTerm WSL Evaluation.cmd` launches the extracted Windows
Electron runtime with the `Ubuntu-24.04.1` repository venv. This machine-specific
launcher and the downloaded binaries are ignored build artifacts. They do not
depend on `/tmp`. The source-run instructions above reproduce the application
on another machine.

## Window and backend lifetime

- A second launch focuses the existing app in the same packaged backend mode.
- With a supported system tray, closing the window hides it and keeps the
  backend and connections alive. Use the tray's **Open StandTerm** to return.
- **Quit StandTerm** or Ctrl/Cmd+Q ends the owned backend and terminal bridges.
- Without a usable tray, closing the last window exits the app.
- A parent crash closes the control pipe; the Python process detects EOF and
  cleans up its bridges. Browser-window reload can reattach while the parent
  remains alive. This prototype does not preserve sessions across app exit.

Desktop uses a persistent browser partition per backend mode (Windows or WSL).
At the same origin, preferences, saved SSH profiles, browser identity and
browser-owned SSH keys survive a full app exit, using Core's existing
localStorage/IndexedDB schemas. SSH private keys remain non-extractable
CryptoKeys; Desktop does not export them to Python, JSON or a new key store.
Chromium profile files still require normal OS-account protection;
non-extractability is an API restriction, not protection against account malware.

As in Core, a changed host or port is a different origin with separate settings
and keys. Settings export/import can move supported preferences and profiles,
but excludes keys and credentials. There is no automatic cross-origin key
migration or import from Chrome/Edge. Windows and WSL profiles are separate.
Data discarded by older memory-only Desktop versions cannot be recovered.
Smoke tests continue to use disposable, memory-only partitions.

The Agent panel keeps action approvals directly below its header. Long transfer
paths and command details scroll independently of the fixed Approve / Reject /
Pause controls; the remaining agent settings have their own scroll area. The
panel is bounded by the viewport, including after dragging or resizing. This
layout is shared with Core Web and does not change approval policy or payloads.

## Capture (desktop-only add-on)

Use the native **Capture** menu:

- **Copy screenshot** (Ctrl/Cmd+Alt+S): PNG to the OS clipboard.
- **Save screenshot as PNG...**: choose a new file with the native Save dialog.
- **Start recording (WebM)...**: choose a new file before recording starts.
- **Stop and save recording** (Ctrl/Cmd+Alt+R): finalize the silent WebM.

Capture includes the visible StandTerm page, including any open in-page panels
and terminal text. It does not capture other applications, OS window decorations,
native dialogs, detached PiP windows, off-screen scrollback, microphone or system
audio. Review visible secrets before sharing a capture. Switching StandTerm tabs
while recording records the newly visible tab as well.

The native title shows `[REC mm:ss]` and the menu shows recording status. Closing
or quitting asks whether to keep recording or stop and save. Hiding, minimizing,
reloading or entering fullscreen stops and saves automatically, because hidden
pages can stop producing frames or hide the indicator. Leave fullscreen before
starting a recording; maximizing is supported.

Recording uses Chromium's available WebM codec (VP8 preferred, VP9/WebM fallback),
with a requested 30 fps and 4 Mbps video rate. Actual frame rate/bitrate depend on
the platform and content. Chunks are written to a `.partial` file beside the
chosen destination, not buffered as a whole video. The renderer queue is limited
to 32 MiB and each chunk to 8 MiB; a slow writer or encoder error stops recording
and reports the retained partial file. A partial/crash-interrupted WebM may not
be playable. There is no automatic retry or deletion of partial output.
Streaming WebM can begin with a brief black frame, and duration/seeking support
varies between players because this prototype does not rewrite container indexes.

Successful output is published without overwriting existing files or copying the
video a second time. This currently requires a filesystem with hard-link support
(for example NTFS or ext4); choose a local supported drive rather than FAT/exFAT.
If publication fails, the error reports the retained partial file. Unix files
are created with mode `0600`; Windows access follows the destination directory's
ACL. Screenshot files use the same no-overwrite publication policy.

The save directory starts at the desktop OS Downloads directory and remembers
the last selection for this app run only. With Windows Electron + WSL Core,
captures and clipboard belong to **Windows**, not the WSL filesystem/clipboard.
Format, save dialogs and clipboard behavior stay entirely in `desktop/`; Core,
browser UI and external agents gain no capture API or filesystem authority.

Video runs in a separate app-owned, sandboxed local recorder page with no backend
cookies, Node access, preload or IPC bridge. A one-use main-process grant selects
only the existing StandTerm main frame; it never enumerates desktop windows or
grants camera/microphone access. The terminal page cannot initiate capture.

## Authentication and security boundary

**StandTerm > About StandTerm Desktop** lists Desktop and the running Core
versions separately, plus Python, Electron, Chromium, Node.js, platform and the
managed Core bundle SHA-256 identity when available. The same Core details are
in Diagnostics. Core reports its version from `core_version.py`, independently
of the Electron package version. Source checkouts have no managed build identity;
older backends that omit version metadata show Unknown, never an inferred Git
tag. The current source candidate is Desktop 0.4.1 / Core 2.11.0-dev, not a
published stable release. Version and copy-info additions postdate the delivered
0.4.0 installer; they require a new build.

The native **Diagnostics** menu shows the actual backend URL (IP and port),
backend mode, Desktop version and web-settings storage mode. It opens
the current mode's diagnostic log folder without exposing an access-token URL.
Startup records use a fixed field/event allowlist: port checks, verification
attempts, elapsed times and typed errors. Raw backend output, page console data,
terminal content and authentication/SSH credentials are never collected. Logs
rotate at 256 KiB and retain one previous file; write failure does not stop Core.
Startup error dialogs include the diagnostic file location even if no terminal
window could be opened.

Since 0.4.0, **Diagnostics > Status and recent events...** opens an isolated,
read-only page with the actual URL, backend mode/process state, Electron /
Chromium / Node versions, profile and port-settings paths, and up to 200 typed
events from this run. Use its **View > Refresh** (Ctrl/Cmd+R) to take another
snapshot. It has a separate temporary session, no backend cookies, no scripts,
no Node/preload bridge and no network access. Paths may identify your OS account;
review the page before sharing. Previous-run logs remain in the log folder.

**Copy backend URL** copies only the current origin. **Copy agent connection
info** copies a tokenless JSON object with `base_url`, `agentinfo_url`,
`instance_id` and `backend_mode`, identified by the `standterm_agent_connection`
schema. Both actions are in the main Diagnostics menu and the diagnostic
window's View menu. Paste the latter into the agent conversation to identify
this exact instance when Core and Desktop run together; do not guess port 5000
or use a shared current-instance pointer. The object is bootstrap information,
not a credential or a handoff file. The agent must fetch its exact Agent Info
URL; existing minting, terminal selection and approval requirements still apply.
Copying does not connect an agent, mint a token or add clipboard access to the
renderer. These source changes postdate the original 0.4.0 evaluation installer.

**Diagnostics > Developer Tools...** requires explicit confirmation each time.
Use the Console or Sources snippets for trusted frontend JavaScript diagnostics
without rebuilding or reinstalling. It does not load external main-process
plugins, patch the packaged backend, open a remote debugging port or grant Node
access to the terminal page. Console code can still operate the authenticated
UI, so do not paste untrusted snippets. Tools are closed by default.

The saved port normally keeps the browser origin stable across launches.
Changing the port, including accepting a replacement after a conflict, changes
the origin just as it does in Core. Check the displayed URL if settings appear
missing; the previous origin's data is not deleted by a port change.

The Python runner binds `127.0.0.1` before returning a bounded JSON handshake on
a private stdout pipe. Normal logs use stderr. Electron checks the returned
instance ID through the authenticated launcher status endpoint before placing
the session credential in an HttpOnly, SameSite=Strict cookie. Before installing
the fresh credential, Desktop clears cookies, HTTP authentication, service
workers and Cache Storage while preserving Core's localStorage and IndexedDB.
It repeats authentication cleanup on shutdown, including the shutdown error
path; next startup also handles stale cookies left by a crash. A persistent
profile may write a cookie to disk during a run, so this is not a promise that
credentials never touch disk. HTTP cache is disabled. The desktop bootstrap
does not pass authentication secrets through
URLs, arguments, environment variables or renderer JavaScript. The existing
user-requested **Show/Copy Access URL** action remains available. Other clients still need normal
StandTerm authentication. External-agent discovery files retain their existing
permission model and are separate from desktop-login credentials.

The UI has Node integration disabled, context isolation, renderer sandboxing
and web security enabled, and no preload or IPC bridge. Network requests are
limited to the owned loopback HTTP/WebSocket origin and local data/blob images.
Other navigation, arbitrary page-created child windows, webviews and device permissions are denied.
There is no certificate-error bypass.

Desktop link previews show **Open in browser** immediately instead of attempting
blocked external embedding. The preview header button, fallback action and
HTTP(S) links opened from Terminal PiP use a native destination confirmation,
defaulting to Cancel, before handing the canonical URL to the OS default browser.
Windows Electron + WSL therefore opens the Windows browser. Its login/cookies
are separate; Desktop does not append its access token or transfer credentials.
The URL itself can contain private information, so review it before approval.
Non-HTTP(S), credential-bearing URLs, loopback links and POST popups are rejected;
pending confirmations are coalesced and owner lifetime is checked again after
approval. Owned Files download tickets retain their existing download path.
An OS browser-launch failure displays an error. Normal Web preview/popup behavior
is unchanged. This follows the restrictive handling required by
[Electron's external-link security guidance](https://www.electronjs.org/docs/latest/tutorial/security#15-do-not-use-shellopenexternal-with-untrusted-content).

Since 0.3.2, Terminal PiP and Files use one controlled, always-on-top native
floating window in Desktop. The Core launcher advertises this typed capability;
ordinary Web launchers retain browser Document PiP. The blank child inherits the
opener's private session and sandbox, has no Node/preload bridge, rejects navigation
and nested windows, and closes when its opener reloads or exits. Download accepts
only owned-origin Files ticket URLs and uses the private authenticated session;
the normal desktop save dialog handles its destination. Rapid repeated/mixed
open requests are coalesced, and creation failure displays a retry message.
Native Windows Local Shell Files remains unavailable because that backend lacks
the anchored POSIX file-operation primitives; WSL Local Shell and direct SSH use
their existing capability checks. This change does not relax backend file safety.

Evaluation tradeoffs:

- Loopback HTTP assumes the local OS account is trusted. It is not an isolation
  boundary against malware running as that user, administrator or root.
- An XSS in the terminal UI can still use the authenticated backend to perform
  terminal actions. Electron sandboxing does not remove that application-level
  authority. A stricter CSP requires addressing the current inline UI scripts.
- Remote URL previews, arbitrary popups and browser permission-based
  clipboard APIs are restricted in this prototype. Native menu/keyboard
  copy/paste and backend file transfers require platform acceptance testing.
- Platform passkeys are not part of desktop bootstrap. This prototype's IP
  origin intentionally cannot register WebAuthn credentials.
- There is no automatic backend restart, OS login autostart,
  OS keychain integration or updater yet.

Keep Electron patched: it ships Chromium and Node.js, independently of the
user's installed Edge/Chrome. Official support covers the latest three major
release lines. Production packaging also needs separate OS/architecture builds,
macOS signing/notarization and Windows signing/distribution decisions.

## Cross-platform evaluation

| Path | What must be evaluated |
| --- | --- |
| Windows + Windows Python | pywinpty, COM ports, IME, file dialogs, tray |
| Windows + WSL Python | interop, localhost forwarding, pipe EOF and WSL process cleanup |
| macOS + local Python | Intel/Apple Silicon builds, IME, Keychain expectations, Dock/tray |
| Linux / WSLg | Chromium sandbox availability, graphics, clipboard and desktop tray support |

The UI and shell code are shared, but each platform needs its own Python runtime
and Electron binary. WSLg testing is not evidence that Windows-native packaging
or macOS works.

## Checks

```sh
npm test
npm run smoke
npm run smoke:capture
```

Smoke starts a real backend and sandboxed Electron renderer, confirms tokenless
entry, verifies that JavaScript cannot read the cookie or access Node, starts a
local terminal and verifies actual command output, reloads to reattach, and checks unauthenticated access and popup
restrictions. Run it in a desktop session with a functioning Chromium sandbox.
Floating-window regression also checks real child creation, private session and
sandbox inheritance, rapid mixed clicks, denied navigation/nesting, close/restore,
PiP-to-Files transition, reload cleanup and visible failure alerts. Where Local
Files is supported, it browses a synthetic fixture and verifies downloaded binary
bytes without opening a save dialog or another child window.

Capture smoke additionally saves a PNG, checks clipboard image packaging without
touching the user's clipboard, records and decodes WebM with a source-page pixel
marker, and checks cancellation, concurrent stop, denied extra media requests,
file collisions, partial-file retention and hide-to-save. Smoke runs use an
isolated Electron user-data directory so they do not attach to a running app.
Generated capture samples are retained under ignored `desktop/dist/`; unit-test
scratch files in the OS temporary directory are disposable and reproducible.

`desktop/test/backend_smoke.py`, run through the selected project venv, separately
checks the private handshake, unauthenticated HTTP rejection, HttpOnly cookie
behavior, pipe-EOF exit, port closure and runtime artifact cleanup.

`desktop/test/bootstrap_smoke.py` checks manifest tampering, traversal/link
rejection, existing-data preservation and retry after dependency failure.
`desktop/test/bootstrap-integration.cjs <prepared-platform-venv-python> <staged-bundle>`
creates a fresh test runtime under ignored `desktop/dist/`, installs dependencies
and verifies reuse. It requires internet access. Keep its stdin open while setup
runs; parent-pipe EOF is the cancellation signal.

Evaluation on 2026-09-07: Electron 44.2.0 passed the desktop smoke with both a
WSLg Linux window and a Windows-native x64 window using WSL Python, plus a
Windows-native x64 window using a newly created Windows Core venv. The
separate backend smoke also passed. Windows requires a bounded readiness retry
while WSL establishes localhost forwarding for the newly assigned port. WSLg
reported WebGL2 as blocklisted; the existing terminal renderer fallback remained
functional. macOS, tray visibility, IME and broader OS
integration still need separate acceptance tests.

The capture smoke also passed on WSLg and Windows-native Electron with both WSL
and Windows Python. Actual OS clipboard paste, interactive native dialogs, macOS and long
recording endurance still require manual/platform acceptance testing.

`desktop/test/shortcut-smoke.cjs` exercises real Windows `.lnk` creation,
readback, update and removal in redirected temporary folders, without modifying
the user's actual Desktop or Start menu. `desktop/test/windows_job_smoke.py`
checks descendant cleanup after cancellation and forced setup-owner exit.
`desktop/test/runtime_smoke.py` verifies real platform leases and recoverable
cleanup against synthetic temporary environments. `installer.test.cjs` covers
typed requests, both-mode partial failures, parent loss and owned shortcut plans.
These tests do not replace actual installer lifecycle testing. Version 0.3.0
remains an evaluation candidate until all three clean-install modes, Squirrel
migration, NSIS upgrade, actual shortcut launches and optional cleanup/data retention pass in an isolated Windows
account or VM. Do not publish a formal release solely from package smoke results.

References: [Electron security](https://www.electronjs.org/docs/latest/tutorial/security),
[release support](https://www.electronjs.org/docs/latest/tutorial/electron-timelines),
[system tray](https://www.electronjs.org/docs/latest/api/tray).
