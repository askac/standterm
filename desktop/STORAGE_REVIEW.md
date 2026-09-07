# Desktop Core-equivalent browser storage

Decision: retain Core's existing browser storage without a new settings or key
serialization layer. Use a stable persistent Chromium partition per backend
mode, with the same origin isolation as Core. Reset launcher authentication
before navigation and on shutdown; leave localStorage and IndexedDB intact.
Tests use disposable profiles and synthetic keys, never the installed profile.

The user explicitly requested Core-equivalent behavior, including saved keys.
Alternatives considered: memory-only storage (loses data on full exit), an
appearance-only JSON store (does not preserve Core SSH profiles/keys), and a
cross-origin migration layer (expands scope and changes credential policy).

Two bounded independent review rounds completed. No blocking security or
correctness finding remained in the reviewed implementation; runtime validation
and the final decision remain the main implementer's responsibility.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| New helper may be omitted from the installer | Medium, conditional | `stage-windows.cjs` copies an explicit shell allowlist | Include the session helper explicitly | Accept | Add `browser-session.cjs` alongside diagnostics | Fresh staging output contains both helpers |
| Reload is insufficient persistence evidence | Validation requirement | Memory-only partitions already survive reload; Core stores CryptoKeys in IndexedDB | Use separate Electron processes and restarted backend at the same origin | Accept | Add `test/browser-storage-smoke.cjs` using actual Core schemas/helpers | Preferences, SSH profiles, browser identity and SSH keys survive process/backend restart |
| Persisted authentication and workers must not cross backend lifetimes | Security requirement | Core refreshes a cookie with a persistent lifetime; abrupt exit can leave it on disk | Clear stale auth before the fresh credential, preserve key stores | Accept | Reset cookies, HTTP auth, service workers and Cache Storage on startup and normal/error shutdown | Unit tests check exact storage scope/fail-closed behavior; real restart clears a persisted synthetic stale cookie |
| Random test port can equal the previous port | Low, test reliability | OS allocation does not guarantee a new port | Retry new-origin allocation within a bound | Accept | Retry at most five times; assert different origin | Real mode/origin isolation phases pass |

Review-driven changes: explicit staging inclusion, full-process persistence
coverage, error-path authentication cleanup, and bounded new-origin allocation.
Preserved: Core data schemas, browser-owned non-extractable SSH CryptoKeys,
default-deny request/permission policy, sandbox/no-Node/no-preload, and fresh
launcher credentials. No private-key export, backend key store or new IPC bridge
was added. Non-extractability is not encryption against OS-account compromise.

Deferred: cross-port migration/stable virtual origins, additional OS keychain
integration, and copying existing Chrome/Edge profiles. Revisit only with an
explicit new user requirement and credential-policy review. No unresolved policy
choice blocks Core-equivalent persistence. Changing the port still changes the
origin; old memory-only data cannot be recovered.

## Validation, 2026-09-07

- All 45 Desktop Node tests pass.
- Windows Electron with native test Core passes four separate process phases:
  write, same-origin restart/read, separate backend-mode partition, new origin.
  Browser identity and SSH keys remain non-extractable and usable for signing;
  SSH private-key export is rejected. The backend token changes on restart.
- Windows Electron with WSL Core passes authenticated shell, reload, sandbox,
  navigation guards, PiP, Files browse/download and transition smoke.
- Fresh installer staging includes the two new production helpers. This is not
  an installer rebuild or an installed-upgrade test.

Next executable step: build a new evaluation installer and validate its packaged
payload before an explicitly approved installed upgrade. The previously delivered
0.3.3 installer does not contain this persistence or diagnostics change.
