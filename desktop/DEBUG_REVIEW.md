# Desktop 0.4.0 diagnostics and browser fallback

Decision: keep Core's Web preview behavior and Desktop's default-deny external
networking. In Desktop, show the browser fallback immediately and route main /
PiP HTTP(S) popup requests through one native, cancel-default confirmation before
the OS browser handoff. Add an isolated read-only diagnostics page rather than a
renderer-to-Node debug bridge. Retain Core-equivalent persistent storage from
the separately documented storage review.

The user requested an installer moving toward release readiness, more visible
debug information, and a working browser fallback when a preview cannot display.
This is an unsigned 0.4.0 evaluation candidate, not a completed formal release.
Unrestricted shell opening, arbitrary embedded external browsing, and a preload
debug-command API were considered and rejected as unnecessary authority.

Two bounded independent read-only review rounds completed. The main implementer
verified the source findings and owns the final scope and validation claims.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Both popup paths must support fallback | Medium correctness | `floating-windows.cjs` previously denies main external URLs and child non-downloads | One shared opener, Files first, always deny Electron external-window creation | Accept | Add `external-links.cjs`; preserve owned Files tickets and blank PiP admission | Actual Core overlay button and actual PiP child, cancel and approval; OS call stubbed |
| Status page must not inherit credentials or network authority | Medium security | Main partition has cookie and IndexedDB keys | Separate temporary partition, escaped HTML, restrictive CSP, no scripts/Node/IPC/network | Accept | Add `diagnostics-window.cjs`, bounded in-memory typed events | HTML injection unit test; real separate session, empty cookies, sandbox, denied fetch, repeated reopen |
| OS handoff needs validation and post-approval lifetime checks | Medium security | Native opener is outside renderer network policy | Validate URL/POST/loopback/credentials; coalesce prompt; recheck owner | Accept | Canonical HTTP(S), explicit destination, cancel default, no appended credentials or URL logs | Numeric/IPv6 loopback, schemes, navigation/destruction during prompt, failure and failed-notification regressions |
| New helpers can be omitted from staging | Medium packaging | `stage-windows.cjs` uses explicit lists | Include production and runtime smoke helpers | Accept | Update staging and builder lists | Final payload comparison required before delivery |
| Runtime smoke does not prove OS browser or installed upgrade | Release validation | `external-links-smoke.cjs` stubs `shell.openExternal`; smoke bypasses installer setup | Preserve truthful scope and manual acceptance checklist | Accept | Keep evaluation label and explicit remaining checks | No user installer or existing profile is modified by automated tests |

Review-driven work includes actual Core fallback coverage, child dispatch,
isolated status-session checks, owner/source destruction regressions, failed
notification handling and repeated diagnostic reopening. The review found no
remaining blocking implementation issue after round two. No third round ran.

Preserved: Core schemas and Web behavior, Desktop sandbox/permission policy,
fresh launcher authentication, browser-owned SSH CryptoKeys, and existing Files
download authorization. This is not a defense against malware with the same OS
account or against authenticated UI XSS. A user-approved external URL may itself
contain private information; Desktop does not append its own credentials.

Deferred: actual OS default-browser acceptance, isolated clean install / upgrade /
uninstall acceptance, code signing, automatic updates and other platform builds.
Activate these before public production rollout; do not substitute smoke results
for installer lifecycle or signing policy. No unresolved policy blocks this
evaluation installer.

Next executable acceptance step: quit installed Desktop modes using their trays,
upgrade with the user's installer, inspect Diagnostics > Status and recent events,
and try Open in browser from a previously failing link preview. Verify both cancel
and approval with the user's configured default browser.

## Additional approval-card correction and final candidate

The user subsequently reported long cross-tab file-copy approvals hiding their
controls. This layout-only addition was validated locally, not submitted as a
third review round. The action card is now directly below the panel header;
its content scrolls independently of fixed approval/rejection/pause controls,
while agent settings have their own scroll area. Paths wrap without horizontal
overflow. Clamp a manually positioned panel after updating its content, and bound
the overall panel by the viewport. No backend policy or decision payload changed.

Validation on 2026-09-07:

- All 50 Desktop Node tests pass.
- Full separate-process storage test preserves preferences, saved SSH profiles,
  browser identity and SSH signing keys across backend restart, resets stale
  cookies, and verifies mode/origin isolation.
- Six focused Web regressions pass: 640x480 / 360x300 long approval layout with
  real hit-target/trial-click checks, canonical copy plan and replace warning,
  global single-shot decisions, stale approval rejection, panel dragging and
  Terminal PiP. Test-only debug/log overlays are hidden for layout hit testing.
- Final packaged native Windows and Windows/WSL pass authentication, shell I/O,
  reload, diagnostics isolation/reopen, main and child link approval dispatch,
  PiP and PNG / decodable WebM capture. WSL additionally passes Files browse,
  exact binary download and PiP-to-Files transition; native Local Shell Files
  correctly remains unsupported by its existing anchored-file capability.
- Read-only extraction verifies the installer's app.asar and executable match
  the tested unpacked application, with all 47 Core hashes and helper inclusion
  checked by `test/package-inspect.cjs`. No venv/private handover/rescue tool is
  bundled. These checks do not run the installer or change an operator profile.

Final build: `desktop/dist/windows-build-sGIWZh`.
Core bundle: `8b4834a53087e69b2890251d461921257b7633ad45cd4d85912389a3c6ded291`.
Artifact: `desktop/dist/StandTerm-Desktop-0.4.0-win32-x64-Setup.exe`.
SHA-256: `f2157076c2865af55874642db0f1cf177748ece2adf1d4692b9439ebe4433ccd`.
Windows Authenticode reports `NotSigned`. The earlier 0.4.0 staging build is
superseded by this candidate; existing 0.3.3 artifacts remain intact.
