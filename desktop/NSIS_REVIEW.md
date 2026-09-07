# NSIS installer candidate decision review

Decision: replace the Squirrel evaluation with a per-user assisted NSIS installer
that prepares explicitly selected Windows/WSL environments before success.
No automatic system Python/WSL installation, elevation, backend startup, legacy
uninstall or running-session termination is authorized. Optional venv cleanup
must preserve Core, settings and unrelated files. This is a candidate, not a
formal release.

Two bounded, read-only independent review rounds are complete. The main
implementer verified the cited behavior and owns the following decisions.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| New locks cannot prove old runtimes idle | High correctness | Legacy bootstrap has setup-only locks; old backends hold none | Opt-in protocol marker; shared lifetime/setup/cleanup lease; retain legacy | Accept | New bundles opt in; old markers never qualify | Actual platform locks, legacy markers, busy backend/setup, reverse start race |
| Installer must own helper completion and cancellation | High correctness | Child EOF/Job protects only Electron to Python | Watch installer lifetime; all selected modes must succeed | Accept | Fixed PowerShell script holds parent process handle; helper loss cancels bootstrap; partial work retained | Typed handle handshake, real Windows dummy-parent exit, second-mode failure and no success shortcuts; real NSIS cancellation still pending |
| Maintenance can write the wrong mode profile | Medium correctness | Setup formerly used only current userData | Fixed per-mode profile, separate maintenance lock | Accept | Installer explicitly selects fixed profile helper; independent early main entry | Windows/WSL preference isolation regression |
| Missing installer lifecycle acceptance | High release validation | Package smoke is not install/update/uninstall | Isolated Windows account/VM lifecycle matrix | Accept | Formal release remains gated | Pending; no live operator installation used |
| Cleanup consent lacks exact paths/context | Medium correctness | Generic checkbox followed by immediate cleanup | Read-only inventory, path confirmation, bounded IDs, revalidate | Accept | Second confirmation defaults to keep; helper accepts IDs only under fixed roots | Empty selection, read-only inventory, becoming busy after inventory; dialog cancellation test |
| Partial cleanup can be reported as retention | Medium correctness | Late entry/output cap can lose already-completed move results | Preflight bounds or streaming, report unknown on lost completion | Accept | Count/serialized worst-case budget before mutation; unknown result wording | Oversized inventory/response budget and failed cleanup result regression |

Review-driven changes: a common runtime lifetime lease, explicit cleanup protocol
marker, fixed profile routing, parent process-handle monitoring, all-mode
completion gating, exact-path cleanup confirmation and pre-mutation output
budgets. The second pass accepted the main corrections and left two Medium
cleanup issues; those were handled with focused tests, without a third round.

Preserved: one shared Electron/Core implementation, explicit OS choice, no
fallback between modes, user consent before dependency installation, sandboxed
setup/terminal renderers, loopback authentication and manual Squirrel migration.
Uninstall retains data by default; same-version retry may reuse completed work.

Cleanup remedy modified: use an app-owned, same-filesystem recovery folder rather
than permanent deletion. The critic accepted this conservative candidate policy.
It detaches only an idle verified venv, **does not reclaim disk space**, and keeps
`restore.json` plus the original venv for manual recovery. Older selected WSL
distributions are not scanned. More than 32 runtimes or an excessive response
budget is retained for separate manual review. Full restore UI, permanent purge,
automatic updates and broad platform qualification remain deferred.

Next executable acceptance step: in an isolated Windows account/VM, test all
three installation modes; missing Windows/WSL Python; cancellation in either
environment; forced installer/helper exit; second-mode failure then retry;
legacy Squirrel refusal/manual migration; NSIS upgrade without cleanup; actual
shortcut launches; uninstall default retention; exact-path cleanup cancel and
confirm; busy/legacy/unavailable retention; and manual recovery. Only after that
matrix passes should a formal release be considered. Never launch Setup over
the current operator's installation as a substitute for isolation.

## Candidate build verification

The unsigned Windows x64 0.3.0 NSIS build completed using electron-builder
26.15.3 and Electron 44.2.0. `StandTermDesktop.exe` reports **StandTerm Desktop** /
**0.3.0.0**. The final package contains 47 manifested public Core files and the
explicit shell/bootstrap/cleanup helpers; their bytes match the source snapshot.
No venv, credentials, private handover or unpublished rescue utility is bundled.

Both unpacked packaged backend modes passed terminal command output,
authentication, renderer sandboxing, reload reattachment, PNG and decodable
silent WebM smoke checks with isolated profiles and pre-existing test venvs.
These smoke checks deliberately bypass interactive installation and do not
constitute lifecycle acceptance. No Setup installer or live uninstaller was run.

Regression evidence: 32 Node tests; 10 synthetic runtime tests on WSL and Windows
(Windows skips the unavailable symlink-creation privilege case); actual Windows
parent-handle loss against an owned dummy process; WSL backend authentication,
port-conflict/reuse and EOF cleanup; bootstrap cancellation and opt-in-marker
tests. Recovery tests operate only on newly created synthetic test directories.

Artifact: `desktop/dist/StandTerm-Desktop-0.3.0-win32-x64-Setup.exe`.
SHA-256: `fe2a5c0f96f28ac9341b6f89adab19dd04dff2e99a0fad58dd129457c9532c9b`.
Bundle ID: `9cb6f3231ff46195e629a84acf081dce668f96a52ec0d21da1470eba1aff8286`.
Build stage: `desktop/dist/windows-build-Kg6dAb`.

## 0.3.1 residual-file detection correction

Operator evidence after uninstall: both HKCU registry views lack the Squirrel
uninstall key; `Update.exe`, `.dead` and `app-0.2.1` remain, but the application
executable is absent. The 0.3.0 `Update.exe`-presence fallback incorrectly blocks
that state. No residual files, preferences or managed environments were removed.

The corrected installer checks the old registration and running executable, then
searches immediate `app-*` folders for `StandTermDesktopEvaluation.exe`. A lone
updater, dead marker or empty version folder is not an installed application.
An existing application executable still blocks even beside `.dead`; process
query errors remain fail-closed. The shared `legacy-install.nsh` predicate is
compiled into both the installer and a read-only NSIS test harness.

Validation: six real Windows NSIS fixture cases cover empty roots, updater-only,
uninstalled residuals with spaces, existing executables, misleading dead markers
and an executable in a later version folder. The same read-only harness returns
no application present against the operator's actual residual folder. All 32
Node regressions pass. No actual installer was run; the earlier full lifecycle
release gate remains in effect.

The unsigned 0.3.1 NSIS build completed in `desktop/dist/windows-build-PjK83d`;
its compiler inputs match the tested source and packaged version is 0.3.1.
Artifact: `desktop/dist/StandTerm-Desktop-0.3.1-win32-x64-Setup.exe`.
SHA-256: `09e59d7898184e0904abd3736c9c10405fc85312450f26b0f665b4b3704c13bf`.
The older 0.3.0 artifact remains unchanged and should not be used to test this fix.
