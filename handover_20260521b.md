# WebSSH Handover 2026-05-21b

## Current Status

- Main repo: `/mnt/d/workspace/github/webssh`
- Main branch: `main`
- Main latest commit: this handover commit, `Add WebSSH backend registry handover`
- Main latest code commit: `35c5b01 Add WebSSH backend registry`
- Main repo is ahead of `origin/main` by 4 commits:
  - this handover commit, `Add WebSSH backend registry handover`
  - `35c5b01 Add WebSSH backend registry`
  - `60ae3e5 Add WebSSH browser connection handover`
  - `5406892 Improve WSL browser connection handling`
- MIBCRK copy: `/mnt/d/workspace/MIBCRK/Tools/webssh`
- MIBCRK Git root: `/mnt/d/workspace/MIBCRK/Tools`
- MIBCRK branch: `master`
- MIBCRK latest WebSSH commit: `8d79d68 Add WebSSH backend registry and agent plan`
- MIBCRK `webssh/` is clean after commit.
- Main repo still has unrelated untracked `.codex`; do not use broad `git add .`.

## Work Completed

- Refactored WebSSH backend connection start flow into an internal backend registry in `app.py`.
- Added built-in backend plugin wrappers for:
  - SSH;
  - Local Shell;
  - UART.
- Kept the existing frontend and Socket.IO protocol behavior compatible.
- Moved backend-specific work behind shared plugin methods:
  - policy option generation;
  - start payload validation;
  - bridge creation;
  - bridge connection;
  - connection failure/action handling.
- Added safety checks after sub-agent design review:
  - backend payload cannot override reserved control fields such as `connection_type` or `terminal_id`;
  - registry rejects invalid or duplicate backend ids;
  - policy options must return matching `connection_type` and bool `allowed`;
  - backend bridge creation/connect exceptions are converted into typed `connection_error`;
  - SSH localhost key setup action type is allowlisted.
- Added a backend-neutral `browser_authorization` policy field while keeping current frontend compatibility.
- Changed browser authorization success wording from Local-Shell-specific to local-resource wording.
- Added `AGENTS.md` local rule:
  - MIBCRK Agent collaboration plans/deployment notes must not be committed to GitHub WebSSH;
  - place those files in `/mnt/d/workspace/MIBCRK/Tools/webssh/` and commit them in MIBCRK `Tools` git when needed.
- Wrote and committed MIBCRK-only Agent collaboration plan:
  - `/mnt/d/workspace/MIBCRK/Tools/webssh/agent_collaboration_plan.md`
  - committed in MIBCRK as `8d79d68`.

## Agent Collaboration Plan Summary

- The Agent layer should be backend-agnostic and attach to terminal sessions, not SSH specifically.
- First implementation should support SSH, Local Shell, and UART through the same Observe + Approval model.
- First version scope:
  - Agent disabled by default;
  - observe-only after attach;
  - proposed terminal input requires user approval;
  - no direct control mode;
  - no persistent disk audit log;
  - no high-frequency viewport upload;
  - mock provider first, real Agent provider later.
- Agent context should be limited to terminal-derived data:
  - sanitized transcript;
  - minimized user input metadata;
  - approved Agent input history;
  - latest same-browser xterm viewport snapshot.
- Terminal content and browser snapshots must be treated as untrusted data and must not drive WebSSH control flow.

## Files Changed

Main repo committed in `35c5b01`:

- `AGENTS.md`
- `app.py`

MIBCRK committed in `8d79d68`:

- `webssh/app.py`
- `webssh/agent_collaboration_plan.md`

## Validation Done

Validation run in `/mnt/d/workspace/github/webssh` after backend registry refactor:

- `tools/.venv_wsl/bin/python -B -c "compile(open('app.py', encoding='utf-8').read(), 'app.py', 'exec')"`
- `git diff --check -- app.py AGENTS.md`
- Flask test client:
  - request without token returns `403` and includes WebSSH access-required page;
  - request with `/?token=<ACCESS_TOKEN>` returns `200`, renders WebSSH, and sets `webssh_session`.
- Policy smoke test under localhost request context:
  - default connection remains `local_shell`;
  - SSH, Local Shell, and UART options are allowed locally.
- SSH payload validation smoke test:
  - `connection_type=ssh`, `terminal_id=main`, `host=127.0.0.1`, `port=22`, `username=aska` validates successfully.
- Reserved backend payload safety smoke test was run before commit during development and returned `backend_payload_reserved_fields` for a plugin attempting to override `terminal_id`.

Validation run in `/mnt/d/workspace/MIBCRK/Tools/webssh` after sync:

- `tools/.venv_wsl/bin/python -B -c "compile(open('app.py', encoding='utf-8').read(), 'app.py', 'exec')"`
- `git -C /mnt/d/workspace/MIBCRK/Tools diff --check -- webssh/app.py`
- Flask test client:
  - no-token request returned `403`;
  - token request returned `200` and set `webssh_session`.
- Policy and SSH payload smoke tests matched the main repo.
- `diff -q /mnt/d/workspace/github/webssh/app.py /mnt/d/workspace/MIBCRK/Tools/webssh/app.py` returned no differences before the MIBCRK commit.

## Remaining Risks / Watch Items

- The backend registry is still an internal refactor, not a full external plugin framework.
- Frontend mode controls and connection forms are still hardcoded for SSH / Local Shell / UART.
- Browser authorization UI is still mostly driven by the Local Shell option; backend now has a neutral `browser_authorization` field for future cleanup.
- UART policy generation can still call serial discovery; future plugin work should separate fast capability from slower resource discovery.
- Agent collaboration is only a committed MIBCRK plan, not implemented yet.
- User already validated the previous WSL HTTPS Chrome/Edge and WSL COM UART behavior; this backend registry refactor still needs live smoke testing after restart if desired.

## Next Steps

1. Restart MIBCRK WebSSH from `/mnt/d/workspace/MIBCRK/Tools/webssh`.
2. Smoke test Chrome/Edge WSL IP Access URL and WSL COM UART against `8d79d68`.
3. If stable, continue Agent collaboration implementation from `/mnt/d/workspace/MIBCRK/Tools/webssh/agent_collaboration_plan.md`.
4. Keep Agent plan files out of GitHub WebSSH unless explicitly requested.

## Prompt Stub Location

- `handover_20260521b_prompt.txt`
