# Desktop localization candidate acceptance

Date: 2026-09-20. This records an unsigned Windows x64 local evaluation
candidate, not a public release or completed Windows/macOS qualification.

## Artifact and source identity

| Field | Value |
| --- | --- |
| Desktop / Core | `0.5.2` / `2.13.1-dev` |
| Source repository | `/mnt/d/workspace/github/standterm` |
| Build source commit | `38861555f667f05a45ae499298d813a255349966` |
| Candidate directory | `desktop/dist/candidate-0.5.2-2.13.1-dev-3886155/` |
| Installer | `StandTerm-Desktop-0.5.2-2.13.1-dev-win32-x64-Setup.exe` |
| Installer bytes | `112385015` |
| Installer SHA-256 | `f31b8ee9cb1ab91af155e55eb6b48fdaf380c7cb87bf38749bd109040cf36979` |
| Exact build-source snapshot | `standterm-build-source-3886155.tar.gz` |
| Snapshot SHA-256 | `16a2fdfb818e6dce92a9a2be13b2a9bf8f7d51046b92ecdded9f1a5e33e4bba2` |
| Core bundle ID | `229a71a3026ea6c63c05d74d3ded05aed6b9886a4b9205987e25f7d96c3bdeb6` |
| Tools | Windows Node 22.14.0, Electron 44.2.0, electron-builder 26.15.3 |
| Host | Windows 10.0.26200 with WSL Ubuntu-24.04.1 |
| Authenticode | `NotSigned` |

The directory also contains the full Git source archive, release identity,
build metadata, checksum list and selected validation evidence. All 145 staged
source inputs match the Git archive bytes; Git archive honors the repository's
line-ending attributes. The build-source snapshot retains those inputs, all 270
tracked source files and the exact local build invocation. Neither archive
contains a venv, credentials or an operator profile. Acceptance-tool additions
are a later validation commit, not changes to the installer payload.

## Completed checks

| Check | Result and scope |
| --- | --- |
| Windows unit suite | 165 passed using Windows Node 22.14.0. |
| Native Windows localization rendering | English and Traditional Chinese passed with real Electron windows, staged HTML/CSP/preload and native Menu API labels. |
| Compact toolbar | All controls remain visible at 640 CSS px in idle, recording and paused states, in both languages. |
| Setup rendering | Windows, macOS and WSL text branches render on Windows in both languages at 700x500 CSS px; progress/cancellation updates and literal distribution text pass. This is not macOS OS qualification. |
| Visual evidence | Twelve captures of owned test windows retained; Traditional Chinese toolbar and setup captures visually inspected. No operator window was captured. |
| Installer payload | Extracted from the new NSIS installer; a fresh extraction passes ASAR/stage equality, 92 exact Core files, all six Python bootstrap helpers and release identity checks. |
| Packaged Windows backend | Passed authenticated terminal, sandbox/navigation, focus return, native UI actions, toolbar and isolated clipboard-routing smoke using the extracted bundled Core. |
| Packaged WSL backend | The same checks pass, including Files browse/download/transition. |
| Isolation | Separate temporary profiles, runtime and recovery paths. No installation or replacement of the running StandTerm instance. |

Evidence is retained in `desktop/dist/desktop-i18n-3886155/`; selected logs,
screenshots and JSON results are copied under the candidate's `validation/`.
The application-smoke clipboard is mocked; real terminal I/O and window behavior
are exercised. The localization probe's setup events are fixtures rendered by
Windows Electron, not dependency installations.

## Test-environment findings

The first packaged Windows smoke used `tools/.venv_win`, which lacks Flask and
exited before backend startup. The successful run uses the existing complete
Windows `tools/.venv`; WSL uses `/tmp/standterm_wslvenv`. No dependencies were
installed and no product change was needed.

Python creates bytecode in an executed extraction. Final package inspection
therefore uses a second untouched extraction; it does not relax the exact-file
allowlist to accept runtime-generated files. The inspector now explicitly checks
the six bundle-root Python helpers, closing a validation gap identified in the
independent packaging review.

## Remaining qualification

- Real install, upgrade and uninstall flows were not executed. Packaged smoke
  bypasses first-run setup and uses prepared, isolated test environments.
- Native OS dialog interaction/readability, physical clipboard/IME and a real
  recording-save failure remain manual acceptance items.
- macOS requires a native Mac build and acceptance run; no macOS artifact was
  produced. Windows rendering of macOS setup text does not substitute for it.
- No signing, public release, tag, push or mirror synchronization was performed.

To repeat the native rendering probe, run Windows Electron with
`desktop/test/native-i18n-smoke.cjs`, the Desktop stage directory and a separate
evidence directory. Leave `ELECTRON_RUN_AS_NODE` unset. The probe creates only
owned test windows and writes a structured result plus PNGs. Use
`desktop/test/package-inspect.cjs` against a fresh extracted resources directory
and its matching stage before running Core from that extraction.
