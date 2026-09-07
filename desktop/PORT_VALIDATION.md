# Desktop 0.3.3 Windows/WSL port validation

The installed 0.3.2 WSL launcher remembers port 51761. During the reproduced
failure on 2026-09-07, WSL listens there and returns HTTP 401 without credentials,
but Windows cannot connect. Windows reports the excluded range 51670-51769,
and an actual Windows loopback bind to 51761 fails with EACCES. The installed
executable and WSL shortcut are correct. Linux-only binding does not detect this
Windows-side reservation; retrying HTTP cannot make that port available.

## Correction

- Probe an explicit WSL-mode port on Windows before starting Core and probe the
  actual bound port before authenticated verification. Typed socket errors drive
  decisions; localized netsh output and display strings are not control signals.
- Accept EADDRINUSE after backend binding only provisionally: the WSL relay may
  already own the host listener. Authenticated instance verification still must
  succeed. A foreign HTTP response is never accepted or saved.
- Stop rejected candidates, keep Core's automatic candidate/service exclusions,
  and bound host failures to 20. Expected candidate exits do not trigger the
  unexpected-backend-exit dialog or terminate the retry workflow.
- Offer a verified replacement for an unavailable saved port. Cancel and Use
  once preserve its preference; Use and remember saves only after verification.
  Verification or prompt failure stops the owned candidate and does not save.

No reservation, firewall rule, WSL mode or operator profile is modified by the
fix or tests. Windows native mode retains its existing backend binding policy.
The Core manifest is unchanged from 0.3.2, so this shell update does not require
a new managed Python environment when the existing runtime verifies successfully.

## Validation

All 39 native Windows Node tests pass, including consent choices, prelaunch
host rejection, rejected automatic candidates, retry exhaustion, verification
and prompt failures, and typed EACCES/EADDRINUSE handling. Source Electron/WSL
smoke seeds only its disposable profile with 51761 and verifies an approved
replacement (57629 in that run), authenticated shell I/O, Files binary download,
PiP transition, sandbox restrictions and reload cleanup.

Smoke now exercises the same port workflow as normal startup instead of bypassing
it with port zero. STANDTERM_DESKTOP_TEST_PORT is honored only in isolated smoke
mode; it never reads or writes the installed launcher's preference. Actual
interactive installer upgrade remains a user acceptance step, not a smoke claim.

Build staging: `desktop/dist/windows-build-jk1UU1`.
Core bundle ID: `082b15dca138292abb6491b692b9d7ecc55f8de4b64493b7da0b8268d681d606`.

Final packaged Windows and WSL modes both pass authenticated terminal, PiP,
sandbox/reload, PNG and decodable WebM smoke. The packaged WSL run detects the
real Windows exclusion on 51761, verifies and remembers replacement 64544 in its
disposable profile, and passes Files browse/download/transition checks. Packaged
launcher/port helper bytes and version 0.3.3 match the tested source.

Artifact: `desktop/dist/StandTerm-Desktop-0.3.3-win32-x64-Setup.exe` (unsigned NSIS).
SHA-256: `19a9998b960592b47cc2f8c6466d29d4981c26791622cca2527396e6118cc5ea`.
No actual installer, uninstall or operator-preference change was performed by
these tests. Existing artifacts and runtime environments remain intact.
