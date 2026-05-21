# WebSSH Handover 2026-05-21a

## Current Status

- Main repo: `/mnt/d/workspace/github/webssh`
- Main branch: `main`
- Main latest code commit: `5406892 Improve WSL browser connection handling`
- MIBCRK copy: `/mnt/d/workspace/MIBCRK/Tools/webssh`
- MIBCRK Git root: `/mnt/d/workspace/MIBCRK/Tools`
- MIBCRK branch: `master`
- MIBCRK latest commit: `558c462 Improve WebSSH WSL browser connection handling`
- Main repo still has unrelated untracked `.codex` and `AGENTS.md`; do not use broad `git add .`.
- MIBCRK `webssh/` is clean after commit.

## Work Completed

- Fixed fragile Edge/WSL browser connection behavior after the Local Shell, HTTPS, Authorizer, and UART work from `handover_20260521.md`.
- Changed Socket.IO frontend setup from WebSocket-only to polling plus WebSocket upgrade:
  - old: `transports: ['websocket']`
  - new: `transports: ['polling', 'websocket']`
- Changed token access flow:
  - old: `/?token=...` returned `302 /` and relied on the browser accepting the session cookie across redirect;
  - new: `/?token=...` directly renders WebSSH with HTTP 200, sets the session cookie, and frontend removes `token` from the address bar with `history.replaceState`.
- Added a typed access-required diagnostic page for missing token/session instead of the default bare Flask 403.
- Added `Secure` to the session cookie when HTTPS is enabled.
- Added `run.sh` progress messages for:
  - selected Python venv;
  - venv activation;
  - dependency checks;
  - missing pyserial/cryptography recheck triggers;
  - Windows COM bridge dependency check.
- Updated README to document multi-browser Windows-to-WSL behavior:
  - each browser should open the full launcher Access URL including `?token=...`;
  - each browser can independently show HTTPS trust warnings until the WebSSH CA is trusted.

## Problem / Symptom

- User reported Edge could intermittently fail or time out when opening the same WSL IP Access URL that worked in Chrome.
- Server logs showed Socket.IO `transport close` and Werkzeug `write() before start_response` after WebSocket close.
- `run.sh` appeared to hang immediately after the banner because several slow startup checks had no progress output.

## Root Cause / Inference

- Confirmed by code inspection and observed logs: WebSocket-only transport was brittle with Flask-SocketIO threading mode, `simple-websocket`, Werkzeug, HTTPS, and Edge.
- Confirmed by test client: the original token flow depended on a redirect plus browser cookie persistence. The revised flow avoids that redirect.
- Confirmed during live checks: MIBCRK WebSSH was listening on `0.0.0.0:5000`; Windows `curl.exe` could reach `https://172.17.186.221:5000/`, so the later Edge timeout was not a persistent WSL networking or bind failure.
- HTTPS certificate trust warning is expected until the generated WebSSH CA is trusted by Windows/browser.

## Files Changed

Main repo committed in `5406892`:

- `README.md`
- `app.py`
- `run.sh`
- `templates/index.html`

MIBCRK committed in `558c462`:

- `webssh/README.md`
- `webssh/app.py`
- `webssh/run.sh`
- `webssh/templates/index.html`

## Validation Done

Validation run in `/mnt/d/workspace/github/webssh`:

- `tools/.venv_wsl/bin/python -B -c "compile(open('app.py', encoding='utf-8').read(), 'app.py', 'exec')"`
- `bash -n run.sh`
- inline template script extraction with Node `new Function(...)`
- `git diff --check -- README.md app.py run.sh templates/index.html`

Validation run in `/mnt/d/workspace/MIBCRK/Tools/webssh`:

- `tools/.venv_wsl/bin/python -B -c "compile(open('app.py', encoding='utf-8').read(), 'app.py', 'exec')"`
- `bash -n run.sh`
- inline template script extraction with Node `new Function(...)`
- `git -C /mnt/d/workspace/MIBCRK/Tools diff --check -- webssh/README.md webssh/app.py webssh/run.sh webssh/templates/index.html`

Behavior checks:

- Flask test client without token/session returns `403` and includes the access-token hint.
- Flask test client with `/?token=<ACCESS_TOKEN>` returns `200`, renders the WebSSH page, does not redirect, and sets `webssh_session`.
- HTTPS-mode test confirmed `Set-Cookie` includes `Secure`.
- Live MIBCRK service check showed `python -u /home/aska/MIBCRK/Tools/webssh/app.py` running, `0.0.0.0:5000` listening, and Windows `curl.exe -k -I https://172.17.186.221:5000/` returning HTTP response.

## Remaining Risks / Watch Items

- Edge/Chrome may still show HTTPS trust warnings until the WebSSH CA is imported into Windows Trusted Root Certification Authorities.
- Existing already-open browser tabs may still be running old frontend JavaScript; restart WebSSH and open a fresh launcher URL for clean verification.
- WSL Windows COM bridge still depends on Windows Python and pyserial; `run.sh` now prints clearer progress, but locked-down Windows Python environments may still need manual repair.
- The debug overlay gated by `&debug=1` and `WEBSSH_DEBUG_POLICY=1` remain available for future policy/UI diagnosis.

## Next Steps

1. Restart MIBCRK WebSSH from `/mnt/d/workspace/MIBCRK/Tools/webssh`.
2. Open the freshly printed WSL IP Access URL in Chrome and Edge.
3. Verify both browsers reach the WebSSH page, Local Shell is selected by default when allowed, and no automatic shell is started.
4. Test WSL COM UART with a real `COMx` device.

## Prompt Stub Location

- `handover_20260521a_prompt.txt`
