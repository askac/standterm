"""Create only an explicitly approved, user-owned desktop runtime."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

spec = importlib.util.spec_from_file_location('standterm_runtime', Path(__file__).with_name('runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class SetupError(Exception):
    def __init__(self, code):
        self.code = code


WINDOWS = sys.platform == 'win32'
WINDOWS_JOB = None
VERIFY_IMPORTS = ('import flask, flask_socketio, paramiko, cryptography, webauthn, serial, simple_websocket; '
                  + ('import winpty' if WINDOWS else 'import ptyprocess'))


def linked(path):
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def environment_python(root):
    return runtime.venv_path(root) / ('Scripts/python.exe' if WINDOWS else 'bin/python')


def stop_child(process):
    if WINDOWS:
        WINDOWS_JOB.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        except ProcessLookupError:
            pass
        finally:
            # The leader may exit before a descendant that ignores SIGTERM.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait(timeout=15)


def emit(kind, **data):
    print(json.dumps({'type': kind, **data}), flush=True)


def manifest(bundle):
    data = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('files'), dict):
        raise SetupError('invalid_bundle')
    files = data['files']
    encoded = json.dumps(files, separators=(',', ':'), ensure_ascii=False).encode()
    if hashlib.sha256(encoded).hexdigest() != data.get('id'):
        raise SetupError('invalid_bundle')
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or not relative.parts:
            raise SetupError('invalid_bundle')
        source = bundle / 'core' / name
        if linked(source) or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise SetupError('invalid_bundle')
    for required in ['app.py', 'requirements.txt', 'desktop/backend.py']:
        if required not in files:
            raise SetupError('invalid_bundle')
    return data


def check_directory(directory):
    for parent in [directory, *directory.parents]:
        if linked(parent):
            raise SetupError('unsafe_runtime_path')


def safe_directory(directory):
    check_directory(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)


def prepare(bundle, root, data, payloads=None):
    global WINDOWS_JOB
    if WINDOWS and WINDOWS_JOB is None:
        # -I excludes the script directory from imports; load only this sibling.
        spec = importlib.util.spec_from_file_location('standterm_windows_job', Path(__file__).with_name('windows_job.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        WINDOWS_JOB = module.WindowsJob()
    safe_directory(root)
    lock_path = root / '.setup.lock'
    if linked(lock_path):
        raise SetupError('unsafe_runtime_path')
    with runtime.lease(root):
        owner = root / '.standterm-bundle.json'
        if owner.exists():
            if linked(owner) or json.loads(owner.read_text()) != {'id': data['id']}:
                raise SetupError('unsafe_runtime_path')
        else:
            if set(root.iterdir()) != {lock_path}:
                raise SetupError('unsafe_runtime_path')
            with owner.open('x') as output:
                json.dump({'id': data['id']}, output)
        emit('progress', stage='copy')
        for name, digest in data['files'].items():
            destination = root / name
            safe_directory(destination.parent)
            if linked(destination):
                raise SetupError('unsafe_runtime_path')
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise SetupError('modified_runtime')
            else:
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.setup-', delete=False) as output:
                    output.write(payloads[name] if payloads is not None else (bundle / 'core' / name).read_bytes())
                    temporary = Path(output.name)
                os.link(temporary, destination)
                temporary.unlink()
        venv = environment_python(root).parents[1]
        safe_directory(venv.parent)
        if linked(venv):
            raise SetupError('unsafe_runtime_path')
        # Only new bundles whose backend holds the lifetime lease may opt in.
        marker = root / '.standterm-venv.json'
        if 'desktop/runtime.py' in data['files']:
            runtime.check_path(marker)
            marker.write_text(json.dumps({'id': data['id'], 'lease': 1}), encoding='utf-8')
        # All children get their own process group. Pipe EOF cancels an install
        # if the Windows parent is closed or crashes; no sudo/system pip is used.
        canceled = threading.Event()

        def watch_parent():
            # Do not hold Python's buffered-stdin lock during interpreter exit.
            while os.read(sys.stdin.fileno(), 4096):
                pass
            canceled.set()

        threading.Thread(target=watch_parent, daemon=True).start()
        log_path = root / 'setup.log'
        if linked(log_path):
            raise SetupError('unsafe_runtime_path')
        with log_path.open('ab') as log:
            os.chmod(log_path, 0o600)

            def run(args, failure):
                if canceled.is_set():
                    raise SetupError('setup_canceled')
                options = {'creationflags': subprocess.CREATE_NO_WINDOW} if WINDOWS else {'start_new_session': True}
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
                deadline = time.monotonic() + 1200
                try:
                    while True:
                        if canceled.is_set() or time.monotonic() >= deadline:
                            code = 'setup_canceled' if canceled.is_set() else 'setup_timeout'
                            if WINDOWS:
                                emit('error', code=code)
                                stop_child(process)
                            raise SetupError(code)
                        try:
                            result = process.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                    if canceled.is_set():
                        if WINDOWS:
                            emit('error', code='setup_canceled')
                            stop_child(process)
                        raise SetupError('setup_canceled')
                finally:
                    # Cleanup belongs to the main thread, including descendants
                    # whose leader has already exited. Never exit ahead of it.
                    if not WINDOWS:
                        stop_child(process)
                if result != 0:
                    raise SetupError(failure)

            emit('progress', stage='venv')
            run([sys.executable, '-I', '-m', 'venv', str(venv)], 'venv_failed')
            python = str(environment_python(root))
            emit('progress', stage='dependencies')
            run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(root / 'requirements.txt')], 'dependencies_failed')
            emit('progress', stage='verify')
            run([python, '-I', '-c', VERIFY_IMPORTS], 'dependencies_failed')
        ready = root / '.desktop-ready.json'
        if linked(ready):
            raise SetupError('unsafe_runtime_path')
        ready.write_text(json.dumps({'id': data['id']}), encoding='utf-8')


def check_ready(root, data):
    runtime.check_path(root)
    ready = root / '.desktop-ready.json'
    python = environment_python(root)
    healthy = False
    if ready.is_file() and not linked(ready) and python.is_file():
        try:
            if (runtime.read_marker(root / '.standterm-bundle.json') == {'id': data['id']}
                    and json.loads(ready.read_text()) == {'id': data['id']}):
                for name, digest in data['files'].items():
                    installed = root / name
                    check_directory(installed.parent)
                    if linked(installed) or hashlib.sha256(installed.read_bytes()).hexdigest() != digest:
                        raise SetupError('modified_runtime')
                with runtime.lease(root):
                    healthy = subprocess.run([str(python), '-I', '-c', VERIFY_IMPORTS],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                        **({'creationflags': subprocess.CREATE_NO_WINDOW} if WINDOWS else {})).returncode == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return healthy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--prepare', action='store_true')
    # Only the test harness uses this override; the desktop UI has no path input.
    parser.add_argument('--test-root', type=Path)
    args = parser.parse_args()
    if sys.version_info < (3, 10) or not importlib.util.find_spec('venv') or not importlib.util.find_spec('ensurepip'):
        raise SetupError('python_required')
    data = manifest(args.bundle)
    root = args.test_root or runtime.bundled_root(runtime.runtime_base(), data['id'])
    runtime.check_path(root)
    if args.prepare:
        prepare(args.bundle, root, data)
    if not check_ready(root, data):
        emit('needs_setup', bundle_id=data['id'])
        return
    emit('ready', bundle_id=data['id'], root=str(root), python=str(environment_python(root)))


if __name__ == '__main__':
    try:
        main()
    except SetupError as error:
        emit('error', code=error.code)
        sys.exit(1)
    except runtime.RuntimeBusy:
        emit('error', code='setup_busy')
        sys.exit(1)
    except runtime.UnsafeRuntime:
        emit('error', code='unsafe_runtime_path')
        sys.exit(1)
    except Exception:
        emit('error', code='setup_failed')
        sys.exit(1)
