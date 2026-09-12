"""Owned desktop backend. Stdout is a private, single-frame control pipe."""

import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
import re
from pathlib import Path
import secrets
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--git-core', type=Path)
    args = parser.parse_args()
    requested_port = args.port
    if not 0 <= requested_port <= 65535:
        parser.error('port must be between 0 and 65535')
    control_output = sys.stdout
    sys.stdout = sys.stderr
    root = Path(__file__).resolve().parents[1]
    # Hold before importing Core/dependencies until process exit. Source checkouts
    # have no ownership marker and remain independent of installer maintenance.
    lifetime = ExitStack()
    core_bundle_id = None
    if args.git_core is not None or (root / '.standterm-bundle.json').exists():
        spec = importlib.util.spec_from_file_location('standterm_runtime', Path(__file__).with_name('runtime.py'))
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        managed = args.git_core.parent if args.git_core is not None else root
        if args.git_core is not None:
            runtime.check_git_root(managed)
            if args.git_core != managed / 'repo' or Path(sys.prefix) != runtime.venv_path(managed):
                raise RuntimeError('Invalid Git Core environment.')
            root = args.git_core
        lifetime.enter_context(runtime.lease(managed))
        if not runtime.venv_path(managed).is_dir():
            raise RuntimeError('Managed environment is unavailable. Run setup again.')
        try:
            marker = runtime.read_marker(root / '.standterm-bundle.json')
            candidate = marker.get('id') if isinstance(marker, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r'[a-f0-9]{64}', candidate):
                core_bundle_id = candidate
        except (OSError, ValueError, runtime.UnsafeRuntime):
            # Diagnostic metadata must not introduce a new startup failure.
            pass
    sys.path.insert(0, str(root))
    os.chdir(root)
    # Desktop policy is independent of browser-launcher environment overrides.
    os.environ.update({
        'STANDTERM_HOST': '127.0.0.1',
        'STANDTERM_ASYNC_MODE': 'threading',
        'STANDTERM_HTTPS': '0',
        'STANDTERM_DISABLE_AUTO_HTTPS': '1',
        'STANDTERM_ACCESS_UI': 'off',
        'STANDTERM_AGENT_DEV_TOKEN': '0',
        'STANDTERM_DISABLE_AGENTINFO_CURRENT': '1',
    })
    sys.argv = [str(root / 'app.py')]
    import app as standterm
    standterm.app.config['DESKTOP_FLOATING_WINDOWS'] = True
    # Finder starts apps without the login-shell environment used by Terminal.
    # Let each local shell load its own profile (MacPorts/Homebrew/user PATH).
    standterm.app.config['DESKTOP_LOGIN_SHELL'] = sys.platform == 'darwin'
    from server_startup import address_in_use, bound_server, suggested_port

    # Bind before sharing credentials. The parent decides whether to retry a
    # typed conflict; never stop or attach to the service occupying this port.
    stack = ExitStack()
    try:
        actual_port, serve = stack.enter_context(bound_server(
            standterm.app, standterm.socketio, '127.0.0.1', requested_port, None))
    except OSError as error:
        stack.close()
        if not address_in_use(error):
            raise
        control_output.write(json.dumps({
            'type': 'standterm_desktop_bind_error', 'version': 1,
            'code': 'address_in_use', 'port': requested_port,
            'suggested_port': suggested_port('127.0.0.1', requested_port),
        }) + '\n')
        control_output.flush()
        return
    standterm.DEFAULT_PORT = actual_port
    origin = f'http://127.0.0.1:{actual_port}'
    session_token = secrets.token_urlsafe(32)
    standterm.active_sessions[session_token] = time.time() + standterm.SESSION_COOKIE_MAX_AGE
    standterm.ensure_session_cleanup_task()
    standterm.write_external_agentinfo_files(base_url=origin)

    def watch_parent():
        # Both clean shutdown and a crashed parent close this private pipe.
        sys.stdin.buffer.read()
        for token in list(standterm.active_sessions):
            standterm.close_all_terminal_bridges(token)
        standterm.cleanup_external_agent_runtime_artifacts()
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    control_output.write(json.dumps({
        'type': 'standterm_desktop_ready',
        'version': 1,
        'origin': origin,
        'instance_id': standterm.LAUNCHER_INSTANCE_ID,
        'core_version': standterm.CORE_VERSION,
        'core_bundle_id': core_bundle_id,
        'python_version': '.'.join(map(str, sys.version_info[:3])),
        'launcher_token': standterm.LAUNCHER_SHUTDOWN_TOKEN,
        'cookie_name': standterm.SESSION_COOKIE_NAME,
        'session_token': session_token,
    }) + '\n')
    control_output.flush()
    try:
        serve()
    finally:
        stack.close()
        for token in list(standterm.active_sessions):
            standterm.close_all_terminal_bridges(token)
        standterm.cleanup_external_agent_runtime_artifacts()


if __name__ == '__main__':
    main()
