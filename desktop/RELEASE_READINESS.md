# Windows x64 desktop 0.2.0 candidate review

The subsequent **0.3.0 NSIS candidate** and its separate two-round decision review
are documented in [NSIS_REVIEW.md](NSIS_REVIEW.md). The Squirrel artifacts and
historical validation below are retained; they do not describe the new installer's
environment-selection or optional recoverable-cleanup workflow.

Decision: ship one evaluation installer with explicit Windows and WSL shortcuts.
Do not designate it a formal release until isolated installer lifecycle acceptance
is complete. This is an unsigned, manually updated launcher with a bundled public
Core snapshot, not an update to an operator's Git checkout.

The independent, read-only review used two bounded rounds. The main implementer
verified the cited control flow and owns the following decisions.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Native-mode and installer lifecycle evidence | High, release validation | First candidate was WSL-only; real Setup lifecycle is not covered by package smoke | Test native Core and clean install/update/uninstall in isolation | Accept; never reinstall over an operator's app as a substitute for isolation | Native Core addressed; installer lifecycle remains a release gate | Fresh Windows venv creation/reuse, actual shell output and capture passed; lifecycle pending |
| Cancel can leave installation descendants | High, correctness | `bootstrap.py` previously used TERM only; a daemon cleanup thread could lose a race with interpreter exit | Await bounded owned-group/job cleanup, including an exited leader | Accept; move POSIX cleanup to the main thread and retain Windows kill-on-close Job ownership | Addressed after round two | POSIX stubborn leader, exited leader, actual EOF with TERM-responsive leader/stubborn descendant; Windows cancel and forced owner crash |
| Newly required helpers omitted from staging | Medium, packaging | Explicit shell and bundle allowlists in `stage-windows.cjs` | Include helpers and test packaged entrypoints | Accept | Critic withdrew after source verification | Final package contents match current shell/bootstrap; native and WSL packaged shell/capture smoke passed |
| Ready runtime reuses modified Core | Medium, hardening | Reuse previously verified imports, not installed Core hashes/parents | Verify immutable runtime contract or document narrower checks | Modify: recheck Core hashes and parent links; no same-account isolation claim | Addressed | Manifest/link/tamper tests and managed-runtime reuse |

Review-driven changes: main-thread POSIX cleanup with TERM/KILL, Windows Job
ownership, explicit staging of new helpers, and Core hash/link checks on reuse.
The two-round budget is complete; the last concrete race was resolved through
focused regression tests, not an additional review round.

Final candidate validation on 2026-09-07: Windows x64 Forge build completed;
the executable reports `StandTerm Desktop` and version `0.2.0`. Both packaged
backend modes passed authenticated shell command output, renderer sandbox,
reload reattachment, PNG and decodable WebM checks using isolated test runtimes.
All 16 Node tests and 8 WSL bootstrap tests passed. Windows bootstrap checks
passed with POSIX and unavailable symlink-privilege cases skipped; separate real
Windows Job and temporary-folder shortcut tests passed. All 46 bundled public
Core hashes match; no venv, private handover or unpublished rescue tool is bundled.
No actual user installation or active StandTerm session was changed. Windows
Sandbox was not found by the local command availability check; actual installer
lifecycle acceptance is still outstanding.

Subsequent 0.2.1 change: first-launch automatically selected ports are remembered by
both Core shortcuts and Desktop, with explicit approval on reuse conflicts.
Selection and conflict suggestions use the Dynamic/Private range minus built-in
fixed-use exclusions and the backend OS services file, with bounded real-bind
attempts. Validation covers Linux/WSL and native Windows; macOS acceptance
remains outstanding.
The existing 0.2.0 artifact predates this change and still uses a fresh random
desktop port each run.

The 0.2.1 Windows x64 installer was rebuilt on 2026-09-07. The final executable
reports `StandTerm Desktop` / `0.2.1`; both packaged Windows and WSL modes passed
shell I/O, authentication, renderer isolation, reload reattachment, PNG and WebM
checks against the new packaged Core, using existing isolated test venvs. These
smoke runs bypass interactive first-run setup and use disposable port/profile
state. Port persistence/approval regression is separately covered by Node tests.
All 21 Node tests, 15 Core startup tests and 8 WSL bootstrap tests passed.
The 46-file Core manifest and packaged shell helpers match the source snapshot.
Actual Setup install/upgrade/uninstall remains untested; no installer was run.

Artifact: `desktop/dist/StandTerm-Desktop-0.2.1-win32-x64-Setup.exe` (unsigned).
SHA-256: `98424709837751d891f4c55ce2bf5168a9d96872a27611e347f4dd92da5ed2ee`.
Bundle ID: `4917373eeedb89bbfdce161b085d412cf0cc335469f66d6daf273d75759256a6`.
The 0.2.0 installer is preserved separately. Port reset/edit UI is not included.

Preserved: explicit mode flags, one shared Electron/Core implementation,
user-approved dependency installation, stable internal Squirrel IDs, memory-only
terminal browser state, loopback-only desktop binding and no fallback to a different OS.
The new display names do not rename unrelated batch-file shortcuts. Uninstall
retains managed runtimes, launcher preferences and captures.

Deferred: full dependency locking, signed distribution policy, automatic updates,
persistent browser profiles and broader platform acceptance. Signing/distribution
policy and complete dependency reproducibility must be decided before a public
production rollout; lack of signing alone is not proof of a runtime defect.

Next executable acceptance step (isolated Windows account or VM):

1. Install the 0.1.0 evaluation and record its runtime/preferences.
2. Upgrade to 0.2.1 with Desktop quit; verify spaced installed name, icon and both
   Desktop/Start-menu shortcuts. Separately verify a clean 0.2.1 install and an
   upgrade from 0.2.0, including first-run consent for the new Core bundle.
3. Launch each actual shortcut; verify its native/WSL prerequisite and consent
   dialogs, expected shell, independent single-instance lock and runtime reuse.
4. Cancel setup during dependency installation; verify no child installer remains.
5. Uninstall; verify only owned shortcuts/install files disappear and runtime,
   preferences, captures and unrelated launchers remain.

Do not launch Setup over the current user's installation or terminate active
StandTerm sessions without explicit permission. Real `.lnk` tests use redirected
temporary folders; passing those tests is not equivalent to these lifecycle steps.
