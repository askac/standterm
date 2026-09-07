# Desktop 0.3.2 floating-window correction

Decision: preserve browser Document PiP for Web; use an owned blank, sandboxed,
always-on-top BrowserWindow for Desktop Terminal PiP and Files. Do not add a
renderer preload/IPC bridge or broaden backend filesystem permissions.

The original deny-all popup handler rejects window requests. Real Electron
44.2.0 testing additionally shows that allowing native Document PiP resolves
requestWindow but destroys its contents almost immediately, without a
did-create-window BrowserWindow. A request-only test is insufficient. Core also
intentionally uses Referrer-Policy: no-referrer, so the actual owned opener URL,
not the optional referrer string, is the origin authority.

Two bounded independent review rounds completed; implementation decisions and
validation are owned by the main implementer. No demonstrated High security
finding was reported. The following ledger records the material concerns.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Closing PiP can occupy the only child slot | Medium correctness | Real immediate PiP-to-Files transition fails while old child is closing | Exercise the exact transition and separate closing from live admission | Accept | Await old document pagehide before replacement, track closing separately, retain ownership through closed, prevent unload veto | Mock lifecycle plus actual immediate transition smoke |
| Promise resolution is not child-lifetime evidence | Low hardening / validation | Native Document PiP returns then immediately closes; no did-create-window | Inspect actual created child preferences, guards and lifetime | Accept; use controlled native blank child, keep Web API | Native child recorded and inspected directly | Session identity, sandbox/no-Node/no-preload, navigation/nesting, reload cleanup |
| Desktop opening lock releases synchronously | Medium correctness | finally runs before caller resumes after synchronous window.open | Await promise settlement and count actual calls for repeated/mixed clicks | Accept | Hold lock across settlement | Files+Files+PiP and PiP+PiP initialize only once |
| Ticket download path lacks end-to-end evidence | Medium validation | Download links use target=_blank inside the floating document | Handler positive/negative tests plus synthetic authenticated binary download | Accept | Exact-origin ticket dispatch without child creation | Handler checks reject POST/external/stale opener; actual download compares 1,024 binary bytes |

Preserved: existing session request/permission policy, Core authorization, Web
Document PiP, default-deny arbitrary popups, and native Windows Local Shell Files
unavailability. The request shape is not a cryptographic identity or an XSS
boundary; the authenticated UI already has backend authority. Child windows gain
no additional OS bridge. Controlled UI has no beforeunload prompt; close cannot
be vetoed to retain a stale owned child.

Deferred: actual installed 0.3.1-to-0.3.2 upgrade acceptance in an isolated account
or VM, signing/distribution policy and broader platform acceptance. No user
installation or live connection may be modified by smoke tests. These remain
formal-release gates; this build is an unsigned evaluation installer.

Review-driven changes: real child-lifetime verification, closing-state admission,
mixed-click coalescing, and full authenticated download verification.

## Final candidate validation, 2026-09-07

The final packaged executable and bundled Core passed native Windows and
Windows/WSL authenticated shell I/O, reload reattachment, floating-window checks,
PNG and decodable silent WebM smoke. WSL additionally passed Files browse,
ticket download with exact binary content and immediate PiP-to-Files transition.
Native Windows correctly retains the Local Shell Files capability restriction.
All 35 Node tests, the WSL backend authentication/capability/EOF smoke, and three
ordinary Web PiP/Files browser regressions passed. The 47 public Core manifest
hashes and packaged floating-window helpers match the tested working-tree inputs.
Read-only extraction of the completed NSIS payload verifies all 47 Core hashes
and the exact tested app.asar; it excludes venvs, caches, test fixtures, private
handover files and unpublished rescue tools. The delivered copy passes its
SHA-256 sidecar check.

Build: `desktop/dist/windows-build-aoVQnI`.
Artifact: `desktop/dist/StandTerm-Desktop-0.3.2-win32-x64-Setup.exe`.
SHA-256: `72b8425c305f8d1ee25a723b416c2ce6cba4cd9140eab797c355736f1670746b`.
Core bundle ID: `082b15dca138292abb6491b692b9d7ecc55f8de4b64493b7da0b8268d681d606`.

The installer was built, not executed. Smoke uses disposable profiles, explicit
test interpreters and the unpacked bundle, bypassing installed-runtime setup.
The previous 0.3.1 artifact is preserved. Stable NSIS identity and installer
workflow are unchanged; actual overwrite-upgrade remains an acceptance step,
not a claimed test result. Next executable step: quit both installed Desktop
modes via their trays, then test the 0.3.1-to-0.3.2 upgrade with user consent and
verify the actual shortcuts and connected direct SSH Files UI.
