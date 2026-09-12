# StandTerm Desktop evaluation

This Electron evaluation owns a Python backend and opens the existing StandTerm
UI without asking the local operator to paste an access token. It supports a
source-run workflow, an unsigned Windows x64 evaluation installer and a native
Apple Silicon macOS evaluation app/DMG. It is not
a production release or a replacement for `run.sh` / `run.bat`.

## macOS Apple Silicon evaluation

The arm64 DMG contains `StandTermDesktop.app`, Electron and the verified Core
snapshot. Copy the app to a user-owned Applications folder before launching it.
The evaluation uses ad-hoc signing, without Developer ID or notarization; it is
not a Gatekeeper-qualified public release. No signing account or private key is
needed for a local build. Intel/Rosetta acceptance is not implied.

New build filenames pair the independent Desktop and bundled Core versions:
`StandTerm-Desktop-0.4.5-2.11.0-mac-arm64.dmg` and
`StandTerm-Desktop-0.4.5-2.11.0-win32-x64-Setup.exe` for future matching builds.
These are naming examples, not published download links. Core 2.11.0 is a
source-only release; existing 0.4.3 downloads are unchanged. Delivery archives
and checksum sidecars retain the paired label; a future matching Desktop tag
would be `desktop-v0.4.5-2.11.0`.

Staging writes `release-identity.json` from the staged package/lock versions and
the manifest-hashed `core_version.py`. The builder revalidates this identity and
fails on missing or inconsistent inputs. Build from the printed stage, not the
source Desktop directory. Package and lock versions remain Desktop-only SemVer
(`0.4.5`), so the combined label cannot change installer upgrade ordering. The
Core qualifier is preserved, and About/Diagnostics continue to show separate
versions. A label alone is not a source or Core bundle identity.

The 0.4.5 evaluation source includes updated Core terminal reads, transcript
splitting, token-tab countdowns, asynchronous launcher status polling and the
experimental IME anchor, plus guarded Desktop clipboard controls. Earlier local
0.4.5 candidates contain Core `2.11.0-dev`; they must not be renamed or presented
as containing Core 2.11.0. Rebuild both platforms and verify their manifests
before publishing paired installers. Real macOS IME candidate placement and
native clipboard/upgrade acceptance remain separate manual checks.

The trusted Desktop status strip disables background timer throttling so notice
expiry and recording indicators stay current when another window has focus.
This also keeps frames updating for its owning window; it is not a capture
permission grant. Core's own preferences, action focus checks and the existing
stop-on-hide/minimize/fullscreen/navigation behavior remain unchanged.

Native macOS mode is selected automatically, or explicitly with `--backend=macos`.
Local shells start as login shells so their usual profiles (for example
`~/.zprofile` for zsh) supply MacPorts/Homebrew and user command paths even when
the app starts from Finder. The browser/Core launcher's shell behavior is unchanged.
First launch locates an existing native Python 3.10+ with venv/ensurepip support
in MacPorts, Homebrew or PATH, or offers a file chooser. Python must match the
app architecture. The Apple `/usr/bin/python3` developer-tools stub is skipped;
StandTerm does not install Python, package managers, Rosetta or OS components.

After confirmation, setup copies Core and prepares its venv under
`~/Library/Application Support/StandTermDesktop/runtimes/<bundle-sha256>/`.
The interpreter is `tools/.venv_macos/bin/python`. Launcher settings, saved port,
diagnostics and the browser profile live under
`~/Library/Application Support/StandTermDesktopEvaluation/macos/`.
All mutable state stays outside the `.app`. Later launches verify and reuse the
runtime. Setup cancellation retains partial files for retry; a changed Core
bundle uses a new directory. Removing the app retains user data and runtimes.
The Windows installer's optional cleanup is not a macOS uninstall action.

Build on an Apple Silicon Mac with Node 22.12+ and the checkout's macOS venv:

```sh
cd desktop
npm ci
npm run stage:mac
# Change to the absolute stage directory printed above, then:
npm ci
npm run make:mac
```

Staging shares the tracked-file allowlist used by Windows, excludes internal
documents and Git state, and generates the native icon with macOS `sips` and
`iconutil`. macOS outputs are in the stage's `out.noindex/` directory so local
development app copies stay out of Spotlight results. The current build uses
electron-builder 26's [macOS signing options](https://www.electron.build/v26/docs/mac/).

For isolated verification, run `npm test`, `npm run smoke:capture`, the Python
bootstrap/runtime/backend tests, and `test/browser-storage-smoke.cjs` with the
checkout Electron executable. Set separate `STANDTERM_AGENT_RUNTIME_DIR` and
`STANDTERM_SESSION_RECOVERY_STORE` paths for smoke runs. Use a canonical macOS
temporary root, for example `TMPDIR=/private/tmp`, for the complete Core suite.
`test/macos-setup-integration.cjs`, launched by checkout Electron with the prepared
venv Python and stage path as arguments, exercises the real first-run progress
window and bootstrap in a disposable runtime. Its setup choice is scripted.
Smoke camera/microphone denial probes use synthetic media devices, keeping them
independent of the operator's physical audio/video hardware; permission checks
remain enabled. Real device/OS permission behavior still needs manual acceptance.

## Advanced Core source and recovery

Packaged Desktop builds expose **StandTerm > Core source (Advanced)...** in the
native app menu. Bundled Core remains the default. Git mode is optional and
requires an already installed, usable Git in the selected Windows, macOS or WSL
environment. Installing Git on Windows does not satisfy WSL mode. Management and
recovery use the selected base Python; they do not require a working Core or its
venv. Source-run development keeps its existing checkout workflow.

**Enable Git Core** creates one private checkout of
`https://github.com/askac/standterm.git`, branch `main`, and its own Python venv.
**Update Git Core** fetches and fast-forwards that checkout after Desktop has
stopped its owned backend. Updates never stash, reset, clean or overwrite local
changes. Local modifications may run in this advanced mode, but updating a dirty,
diverged, renamed-branch or changed-origin checkout is refused. Interrupted work
is retained for retry or manual repair. There is no background update service or
automatic rollback, and Git never replaces the installed Desktop shell.

Git source files do not match the installation's bundle hashes; enabling Git
waives only that source equality check. Owned-path and interpreter validation,
the setup/backend lifetime lock, private control pipe, authenticated backend
identity, loopback restrictions and renderer sandbox remain active. About and
Diagnostics show the Git commit and local-change status observed at launch.
Changed `requirements.txt` requires dependency preparation before launch; use
**Prepare Git environment** if requirements were edited locally. Dependency
installation can execute code and requires the configured package index.

Before enabling Git, Desktop retains a verified `core.tar.gz` plus manifest from
the currently installed Core. **Restore bundled Core** also creates this snapshot
when needed. The archive is outside the Git checkout and is checked against the
installed manifest, not a mutable Git HEAD. Normal bundled startup continues its
existing hash checks without requiring this cache. An invalid cache is retained
under a separate name and recreated from the verified installed bundle.

Restore reuses a healthy bundled runtime. If it is damaged, Desktop creates a
fresh runtime and venv, then remembers that recovery identity for later launches.
The damaged source, environment and data are retained. Recovery restores Core
files; it does not roll back user data or promise identical dependency versions.
Missing Git never prevents bundled recovery. Missing base Python or damaged
installed application files still require repairing Python or reinstalling
Desktop. Startup or later Core failure opens a native retry/recovery dialog,
independent of the Core web UI.

The runtime base is `%LOCALAPPDATA%\StandTermDesktop` on Windows,
`~/Library/Application Support/StandTermDesktop` on macOS, and
`~/.local/share/standterm-desktop` inside WSL. Git uses `git-core/repo`,
`git-core/tools/<platform-venv>` and `git-core/setup.log`. Recovery uses
`core-recovery/<installed-bundle-id>/`, with `selected.json` identifying a runtime
under `restored/<recovery-id>/`. The shorter runtime path avoids Windows DLL path
limits. These environments are retained by the installer's optional bundled-venv
cleanup. Core source preferences live in the existing Desktop mode profile's
`core-source.json`; Python/distro selection stays in `launcher.json`.

Changing source restarts Desktop and closes terminal sessions after confirmation.
Core authorization and session-recovery files remain local to each source and are
not migrated; switching may require reauthorization. Browser localStorage and
IndexedDB keep their existing per-origin behavior. A changed Desktop version or
bundle ID resets the source to bundled and clears pending Git operations.
Successful Windows installer preparation also resets both, including a same-version
reinstall. Replacing the same version of a macOS app by drag-copy does not reset
its persistent preferences; use **Restore bundled Core** in that case.

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

The `StandTerm-Desktop-0.4.5-2.11.0-win32-x64-Setup.exe` is under `out/`; the unpacked
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

Use the direct capture buttons in the Desktop toolbar. Capture shortcuts are
also available under **View**; there is no separate Capture dropdown:

- **Copy screenshot** (Ctrl/Cmd+Alt+S): PNG to the OS clipboard.
- **Save screenshot (PNG)**: save directly to the configured screenshot folder.
- **Start recording (WebM)**: record directly to the configured recording folder.
- **Pause / resume recording**: suspend capture without finishing the file.
- **Stop and save recording** (Ctrl/Cmd+Alt+R): finalize the silent WebM.

Capture includes the visible StandTerm page, including any open in-page panels
and terminal text. It does not capture other applications, OS window decorations,
native dialogs, detached PiP windows, off-screen scrollback, microphone or system
audio. Review visible secrets before sharing a capture. Switching StandTerm tabs
while recording records the newly visible tab as well.

The native title and Desktop toolbar show recording status and elapsed active
recording time. The stop button remains available while paused. Closing
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

The first save uses a native folder chooser, initially suggesting Downloads.
Canceling does not save a file or start a recording. The chosen folder is shared
by PNG and WebM until customized in **StandTerm > Capture Settings...**. Folder
preferences persist in `capture-settings.json` in Desktop's user-data directory,
independent of backend origins/ports; settings contain paths only, no credentials.
Captures use timestamped, randomized filenames. An unavailable folder prompts for
a replacement rather than silently changing destinations. With Windows Electron + WSL Core,
captures and clipboard belong to **Windows**, not the WSL filesystem/clipboard.
Format, folder dialogs and clipboard behavior stay entirely in `desktop/`; Core,
browser UI and external agents gain no capture API or filesystem authority.

Video runs in a separate app-owned, sandboxed local recorder page with no backend
cookies, Node access, preload or IPC bridge. A one-use main-process grant selects
only the existing StandTerm main frame; it never enumerates desktop windows or
grants camera/microphone access. The terminal page cannot initiate capture.

## Desktop menus and shared terminal toolbar

Core and Web use the same persistent tab row: adjacent New Tab, an independently
reachable Pause Agent button, and right-aligned Agent Panel, Files, Settings and
More actions. Close all terminal tabs retains confirmation; the last terminal's
individual close button is hidden. Main-toolbar Pause always targets the main
active terminal; Panel and PiP Pause target their own terminal.

Desktop adds Settings, tab actions, Files and Agent controls to native menus.
Terminal-scoped commands require the main window to be focused and recheck Core's
typed action availability and terminal ID at invocation. Core reloads and older
Core versions without the action interface disable those commands. Query-string
flags never grant native capabilities.

**StandTerm > Browser Access** offers Open in browser, Copy browser authorization
URL, Copy access URL and Copy access token. Token/URL copies are sensitive and
requested on demand from the authenticated Core session, never copied from its
session cookie. Authorizing another browser requires confirmation and the existing
launcher-authorized endpoint; the launcher credential stays only in the main
process. The link contains an access token as well as a one-time authorization
grant, so the entire URL must not be described as short-lived. These actions do
not relax the separate external-link opener's loopback restrictions. Diagnostics
and Agent connection info remain credential-free.

The Desktop toolbar is a bundled local page, separate from the authenticated Core
WebContentsView. Only the toolbar has a narrowly scoped preload; its private
session has no backend cookies, no network access, and no camera/microphone grants.
IPC validates the exact sender and main frame, and accepts only fixed menu/edit/capture
actions. Core and floating windows remain sandboxed with no Node or preload.
Windows/Linux place Copy/Paste immediately after the menu buttons, separate from
right-aligned capture controls. macOS retains its system application menu and
places Copy/Paste after the title in the window's Desktop toolbar. Menu labels are
not selectable; terminal text, text fields and status notices remain selectable.

**Copy selected text** uses native Copy, never the terminal Ctrl+C interrupt.
**Paste clipboard text** restores the Core editing target and uses native Paste;
text fields keep normal editing behavior. Windows/Linux Ctrl+V remains the terminal
control code; use Ctrl+Shift+V for keyboard paste (Cmd+V on macOS).
Terminal paste events are reviewed before xterm normalizes line endings, including
two-line text. Cancel sends nothing; approval sends once to the captured terminal,
preserving xterm's native bracketed-paste and line-ending behavior.

The main terminal's custom right-click **Paste** requires a one-time native Paste
confirmation in Desktop. Clipboard-read permission remains denied: only an explicit
confirmation reads text once in the main process and passes it into Core's existing
paste review. A changed tab, focus, modal, disconnected terminal or reloaded document
cancels delivery. Background web clipboard requests do not gain access. Floating
windows do not receive this main-window fallback; use native keyboard paste there.
Use the toolbar Paste button to avoid the extra clipboard-access confirmation;
multi-line/large terminal text still requires review. Native clipboard tests use
fixtures, not the operator's clipboard; real clipboard and Mac acceptance remain manual.

Toolbar SVG artwork is original StandTerm geometric artwork under the project
license. No third-party icon paths, icon package, web font or remote image is used.
Transient toolbar messages use a blue-gray bordered status area, distinct from
menu buttons. Normal notices fade after five seconds; red error notices remain
for ten seconds. The reserved space prevents controls from shifting. Long text
is truncated with the full message available on hover, and reduced-motion
preferences disable the fade animation. Recording updates do not replay old
messages. This shared renderer behavior leaves the macOS system menu unchanged.

The toolbar change is a local evaluation snapshot, not the published 0.4.3 release. Source validation
covers Windows Electron with both native Windows and WSL Core, native menu target guards, browser access,
compact layout, PNG/clipboard and decodable silent WebM with pause/resume. The Core
browser suite includes a regression for main-toolbar Pause with a different Agent
Panel target. WSLg floating/capture checks run, but its window manager can refuse
automated main-window activation; the focus-sensitive smoke remains unqualified
there rather than bypassing the guard. Native macOS toolbar placement, Retina
rendering, menu focus and folder dialogs still require a Mac acceptance run.
The final Windows and Windows-with-WSL source runs passed screenshot and video
checks, as did the packaged Windows-with-WSL run. The native Windows packaged smoke
still intermittently fails its Core preview with Chromium `UnknownVizError`; its
native cause is not established and this remains an evaluation limitation.
Separately, the WSL test's stale download timer and unintended native browser
prompt were corrected; those test dialogs could interrupt subsequent checks.
Screenshot requests now have a bounded timeout and record only window
dimensions and visibility/focus flags on failure, never image or terminal content.
Recording elapsed time is shown in the toolbar; it freezes while paused and
excludes paused intervals after resuming. Both the timer logic and real toolbar
display are covered by tests.

## Agent prompts and bundled Core

The top-level **Agent** menu is the first-use entrypoint; no prior StandTerm
skill installation is required. Choose **Copy skill installation prompt** and
paste it into your agent. For subsequent sessions, use **Copy usage prompt** or
**Copy file-transfer prompt** and describe the intended task and terminal.
**Getting started...** explains token minting and the distinction between setup
and terminal authority. **Copy connection info (JSON)** and **Copy agentinfo URL**
remain available for clients that already know the protocol. Existing Diagnostics
copy actions are preserved.

These actions only copy text or show help. They do not install skills, execute
helpers, mint tokens or approve transfers. Each prompt contains the exact live
endpoint and instance ID, never credentials. Agents verify `/agentinfo` identity
and use its `launch_dir`, `python_path`, `scripts` and `skills` paths. Installation
prompts ask agents to preserve references and customized skills; agents without
persistent skill support can read the documents for the current session instead.
Backend paths belong to the active macOS, Windows or WSL environment. If access fails or
an older Core lacks discovery metadata, request the correct environment or a
Core update; do not guess a different endpoint or download arbitrary helpers.

Core staging includes the published runtime, static assets, launch/install
scripts, README, public documentation, skill prompts/references and support
helpers. Only Git-tracked release inputs are selected; developer venvs, private
handoffs, profiles, credentials and unpublished rescue tools are not included.
`core-files.cjs` checks required inputs and relative skill links before and after
staging. The extracted-package inspector repeats the checks alongside manifest
hash verification, so missing skill documents or helpers fail packaging checks.
The Agent menu and expanded Core payload require a new installer build; existing
0.4.1 installers do not gain them automatically.

## Authentication and security boundary

**StandTerm > About StandTerm Desktop** lists Desktop and the running Core
versions separately, plus Python, Electron, Chromium, Node.js, platform and the
managed Core bundle SHA-256 identity when available. The same Core details are
in Diagnostics. Core reports its version from `core_version.py`, independently
of the Electron package version. Source checkouts have no managed build identity;
older backends that omit version metadata show Unknown, never an inferred Git
tag. The current source pairing is Desktop 0.4.5 / Core 2.11.0. The Core source
release does not publish or qualify Desktop installers. The Agent menu and
expanded Core payload postdate the
published 0.4.1 installer and the earlier macOS 0.4.2 candidate; they require a
new build. The integrated macOS candidate retains native setup, login shells
and arm64 DMG packaging alongside these additions.

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
user-requested **Show/Copy Access URL** action remains available. Explicit native
**Browser Access** actions can copy sensitive access information or open an
authorization URL in the OS browser after confirmation; this is separate from
the credential-free Desktop startup URL. Other clients still need normal
StandTerm authentication. External-agent discovery files retain their existing
permission model and are separate from desktop-login credentials.

The authenticated Core UI has Node integration disabled, context isolation, renderer sandboxing
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
  OS keychain integration or automatic updater yet.

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
`desktop/test/core_manager_smoke.py` uses local temporary Git repositories for
update/refusal, readiness, archive tampering, recovery selection and cancellation
checks. `test/core-source.test.cjs` covers source routing, installation resets,
pending actions, native recovery choices and confirmed backend termination.
`desktop/test/core_manager_integration.py <stage>` runs through a prepared test
venv, copies its dependencies into disposable runtimes, and checks the staged
installed bridge, real Core authentication, live leases, local Git updates and a
fresh-process recovery launch. It does not download packages or qualify dependency
resolution from an index. The test's recovery preparation uses copied fixture
dependencies; the regular bootstrap lifecycle is covered separately.
`test/core-failure-smoke.cjs`, run with checkout Electron, exercises packaged
startup failure with scripted native dialog choices and an intercepted relaunch.
Its `--capture-cancel` variant verifies that canceling capture shutdown saves no
Core action; `--exit-during-load <test-venv-python> <stage>` stops a real backend
after HTTP verification and page load to check the native recovery path before
Desktop declares itself ready. These tests use isolated profiles and do not
replace native macOS or installer lifecycle acceptance.
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
