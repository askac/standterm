"""Installed-shell operations for an opt-in Git Core and bundled recovery."""

import argparse
from contextlib import contextmanager
import hashlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zlib

spec = importlib.util.spec_from_file_location('standterm_bootstrap', Path(__file__).with_name('bootstrap.py'))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
runtime = bootstrap.runtime
SetupError = bootstrap.SetupError

REPOSITORY = 'https://github.com/askac/standterm.git'
BRANCH = 'main'
COMMAND_TIMEOUT = 1200
MAX_OUTPUT = 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
REQUIRED = ['app.py', 'requirements.txt', 'core_version.py', 'server_startup.py']


def atomic_json(target, data):
    runtime.check_path(target)
    bootstrap.safe_directory(target.parent)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.write-', delete=False) as output:
        output.write(json.dumps(data).encode())
        temporary = Path(output.name)
    os.replace(temporary, target)


class Runner:
    def __init__(self, root, watch_parent=True):
        self.root = root
        self.canceled = threading.Event()
        if bootstrap.WINDOWS and bootstrap.WINDOWS_JOB is None:
            spec = importlib.util.spec_from_file_location('standterm_windows_job', Path(__file__).with_name('windows_job.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            bootstrap.WINDOWS_JOB = module.WindowsJob()
        if watch_parent:
            def watch():
                while os.read(sys.stdin.fileno(), 4096):
                    pass
                self.canceled.set()
            threading.Thread(target=watch, daemon=True).start()

    def run(self, args, failure='git_failed', *, cwd=None, capture=False, accepted=(0,)):
        if self.canceled.is_set():
            raise SetupError('setup_canceled')
        log_path = self.root / 'setup.log'
        runtime.check_path(log_path)
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if bootstrap.WINDOWS else {'start_new_session': True}
        with log_path.open('ab') as log, tempfile.TemporaryFile() as output:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output if capture else log,
                                       stderr=log, cwd=cwd, env=env, **options)
            deadline = time.monotonic() + COMMAND_TIMEOUT
            try:
                while True:
                    if self.canceled.is_set() or time.monotonic() >= deadline:
                        code = 'setup_canceled' if self.canceled.is_set() else 'setup_timeout'
                        if bootstrap.WINDOWS:
                            bootstrap.emit('error', code=code)
                            bootstrap.stop_child(process)
                        raise SetupError(code)
                    try:
                        result = process.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                if self.canceled.is_set():
                    raise SetupError('setup_canceled')
            finally:
                if not bootstrap.WINDOWS:
                    bootstrap.stop_child(process)
            if result not in accepted:
                raise SetupError(failure)
            output.seek(0)
            value = output.read(MAX_OUTPUT + 1)
            if len(value) > MAX_OUTPUT:
                raise SetupError('invalid_git_workspace')
            return value.decode('utf-8', errors='replace').strip() if capture else result


def git_executable():
    git = shutil.which('git')
    if not git:
        return None
    if sys.platform == 'darwin' and Path(git).resolve() == Path('/usr/bin/git'):
        # The Apple stub can open an installation prompt. Probe the toolchain
        # selector first; it does not install Command Line Tools.
        try:
            if subprocess.run(['/usr/bin/xcode-select', '-p'], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode:
                return None
        except (OSError, subprocess.TimeoutExpired):
            return None
    try:
        result = subprocess.run([git, '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5,
                                **({'creationflags': subprocess.CREATE_NO_WINDOW} if bootstrap.WINDOWS else {}))
        return git if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


@contextmanager
def owned_git():
    root = runtime.git_root()
    bootstrap.safe_directory(root)
    with runtime.lease(root):
        marker = root / '.standterm-git.json'
        if not marker.exists():
            if set(root.iterdir()) != {root / '.setup.lock'}:
                raise SetupError('unsafe_runtime_path')
            atomic_json(marker, {'version': 1, 'kind': 'git-core'})
        runtime.check_git_root(root)
        yield root


def git_command(git, runner, repo, *args, **kwargs):
    return runner.run([git, '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
                       '-c', 'core.autocrlf=false', '-C', str(repo), *args], **kwargs)


def git_status(git, runner, repo, repository=REPOSITORY):
    for name in ['.git', *REQUIRED]:
        runtime.check_path(repo / name)
    if not (repo / '.git').is_dir() or any(not (repo / name).is_file() for name in REQUIRED):
        raise SetupError('invalid_git_workspace')
    run = lambda *args: git_command(git, runner, repo, *args, capture=True)
    if Path(run('rev-parse', '--show-toplevel')) != repo or Path(run('rev-parse', '--absolute-git-dir')) != repo / '.git':
        raise SetupError('invalid_git_workspace')
    if run('remote', 'get-url', '--all', 'origin') != repository or run('symbolic-ref', '--short', 'HEAD') != BRANCH:
        raise SetupError('git_source_changed')
    commit = run('rev-parse', 'HEAD')
    if not re.fullmatch(r'[a-f0-9]{40,64}', commit):
        raise SetupError('invalid_git_workspace')
    return {'commit': commit, 'dirty': bool(run('status', '--porcelain=v1', '-z', '--untracked-files=normal'))}


def requirements_digest(repo):
    target = repo / 'requirements.txt'
    runtime.check_path(target)
    if target.stat().st_size > MAX_OUTPUT:
        raise SetupError('invalid_git_workspace')
    return hashlib.sha256(target.read_bytes()).hexdigest()


def ready_git(root, status, runner, prepare=False, repair=False):
    repo = root / 'repo'
    marker = root / '.git-ready.json'
    python = bootstrap.environment_python(root)
    expected = {'version': 1, 'requirements': requirements_digest(repo), 'commit': status['commit']}
    runtime.check_path(marker)
    runtime.check_path(runtime.venv_path(root))
    try:
        saved = runtime.read_marker(marker)
        ready = (isinstance(saved, dict) and saved.get('version') == 1
                 and saved.get('requirements') == expected['requirements'] and python.is_file())
    except (OSError, ValueError):
        ready = False
    if not ready and not prepare:
        raise SetupError('git_needs_setup')
    if not ready or repair:
        # A failed repair must not leave an older ready marker authorizing a
        # partially changed environment on the next ordinary launch.
        atomic_json(marker, {'version': 1, 'requirements': None, 'commit': status['commit']})
        bootstrap.safe_directory(runtime.venv_path(root).parent)
        bootstrap.emit('progress', stage='venv')
        runner.run([sys.executable, '-I', '-m', 'venv', str(runtime.venv_path(root))], 'venv_failed')
        bootstrap.emit('progress', stage='dependencies')
        runner.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                    *(['--force-reinstall'] if repair else []), '-r', str(repo / 'requirements.txt')],
                   'dependencies_failed', cwd=repo)
    bootstrap.emit('progress', stage='verify')
    runner.run([str(python), '-I', '-c', bootstrap.VERIFY_IMPORTS], 'dependencies_failed')
    atomic_json(marker, expected)
    return {'type': 'ready', 'source': 'git', 'root': str(root), 'core_root': str(repo),
            'python': str(python), **status}


def read_archive(archive, bundle, data):
    runtime.check_path(archive)
    if archive.stat().st_size > MAX_ARCHIVE:
        raise SetupError('invalid_archive')
    payloads = {}
    total = 0
    with gzip.open(archive, 'rb') as compressed:
        unpacked = compressed.read(MAX_ARCHIVE + 1)
    if len(unpacked) > MAX_ARCHIVE:
        raise SetupError('invalid_archive')
    with tarfile.open(fileobj=io.BytesIO(unpacked), mode='r:') as source:
        for member in source:
            if member.name not in data['files'] or member.name in payloads or not member.isfile():
                raise SetupError('invalid_archive')
            expected_size = (bundle / 'core' / member.name).stat().st_size
            total += member.size
            if member.size != expected_size or total > MAX_ARCHIVE:
                raise SetupError('invalid_archive')
            with source.extractfile(member) as entry:
                payload = entry.read(expected_size + 1)
            if hashlib.sha256(payload).hexdigest() != data['files'][member.name]:
                raise SetupError('invalid_archive')
            payloads[member.name] = payload
    if set(payloads) != set(data['files']):
        raise SetupError('invalid_archive')
    return payloads


def snapshot(bundle, data):
    root = runtime.runtime_base() / 'core-recovery' / data['id']
    bootstrap.safe_directory(root)
    archive = root / 'core.tar.gz'
    with runtime.lease(root):
        if archive.exists():
            try:
                return root, read_archive(archive, bundle, data)
            except (SetupError, OSError, tarfile.TarError, EOFError, zlib.error):
                runtime.check_path(archive)
                archive.rename(root / ('invalid-' + uuid.uuid4().hex + '.tar.gz'))
        payloads = {name: (bundle / 'core' / name).read_bytes() for name in data['files']}
        if sum(map(len, payloads.values())) > MAX_ARCHIVE:
            raise SetupError('invalid_bundle')
        with tempfile.NamedTemporaryFile(dir=root, prefix='.snapshot-', delete=False) as output:
            temporary = Path(output.name)
            with tarfile.open(fileobj=output, mode='w:gz') as target:
                for name, payload in payloads.items():
                    if hashlib.sha256(payload).hexdigest() != data['files'][name]:
                        raise SetupError('invalid_bundle')
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(payload), 0o600
                    target.addfile(info, io.BytesIO(payload))
        read_archive(temporary, bundle, data)
        os.replace(temporary, archive)
        atomic_json(root / 'manifest.json', data)
    return root, payloads


def recover(bundle, data):
    recovery, payloads = snapshot(bundle, data)
    with runtime.lease(recovery):
        try:
            root = runtime.bundled_root(runtime.runtime_base(), data['id'])
            if bootstrap.check_ready(root, data):
                return {'type': 'ready', 'source': 'bundled', 'bundle_id': data['id'],
                        'root': str(root), 'python': str(bootstrap.environment_python(root))}
        except (SetupError, runtime.UnsafeRuntime, OSError, ValueError):
                pass
        identity = uuid.uuid4().hex
        root = runtime.runtime_base() / 'restored' / identity
        bootstrap.prepare(bundle, root, data, payloads=payloads)
        if not bootstrap.check_ready(root, data):
            raise SetupError('dependencies_failed')
        atomic_json(recovery / 'selected.json', {'version': 1, 'runtime': identity})
        return {'type': 'ready', 'source': 'bundled', 'bundle_id': data['id'],
                'root': str(root), 'python': str(bootstrap.environment_python(root))}


def manage_git(action, bundle, data, git, *, repository=REPOSITORY, watch_parent=True):
    if not git:
        raise SetupError('git_required')
    with owned_git() as root:
        runner = Runner(root, watch_parent=watch_parent)
        repo = root / 'repo'
        if action in ['enable', 'update', 'prepare']:
            snapshot(bundle, data)
        if not repo.exists():
            if action != 'enable':
                raise SetupError('invalid_git_workspace')
            temporary = root / ('clone-' + uuid.uuid4().hex)
            bootstrap.emit('progress', stage='git')
            runner.run([git, '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.autocrlf=false',
                        'clone', '--branch', BRANCH, '--single-branch', '--', repository, str(temporary)])
            git_status(git, runner, temporary, repository)
            temporary.rename(repo)
        status = git_status(git, runner, repo, repository)
        if action == 'update':
            if status['dirty']:
                raise SetupError('git_dirty')
            bootstrap.emit('progress', stage='git')
            git_command(git, runner, repo, 'fetch', '--no-tags', 'origin', 'refs/heads/' + BRANCH)
            current = git_status(git, runner, repo, repository)
            if current != status or current['dirty']:
                raise SetupError('git_dirty')
            # Reject ahead/diverged local histories too: an explicit update must
            # select the fetched official revision without rewriting local work.
            git_command(git, runner, repo, 'merge-base', '--is-ancestor', 'HEAD', 'FETCH_HEAD', failure='git_diverged')
            git_command(git, runner, repo, 'merge', '--ff-only', 'FETCH_HEAD', failure='git_diverged')
            status = git_status(git, runner, repo, repository)
        return ready_git(root, status, runner, prepare=action != 'check', repair=action == 'prepare')


def status_info():
    git = git_executable()
    result = {'type': 'core_status', 'git_available': bool(git), 'workspace': 'absent'}
    root = runtime.git_root()
    if root.exists():
        try:
            runtime.check_git_root(root)
            if git:
                # Read-only Git commands may run while the backend holds its
                # lease. All source/venv mutations require owned_git instead.
                result.update(git_status(git, Runner(root), root / 'repo'))
                result['workspace'] = 'present'
            else:
                result['workspace'] = 'unavailable'
        except (SetupError, runtime.UnsafeRuntime, OSError, ValueError):
            result['workspace'] = 'invalid'
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--action', choices=['status', 'enable', 'update', 'prepare', 'check', 'recover'], required=True)
    args = parser.parse_args()
    if sys.version_info < (3, 10) or not importlib.util.find_spec('venv') or not importlib.util.find_spec('ensurepip'):
        raise SetupError('python_required')
    if args.action == 'status':
        result = status_info()
    else:
        data = bootstrap.manifest(args.bundle)
        if args.action == 'recover':
            # A WSL launcher can exit before its backend. Exclude a still-live
            # Git backend before switching to bundled, using its exact lock.
            lock = runtime.git_root() / '.setup.lock'
            runtime.check_path(lock)
            if lock.exists():
                with runtime.lease(runtime.git_root(), create=False):
                    result = recover(args.bundle, data)
            else:
                result = recover(args.bundle, data)
        else:
            result = manage_git(args.action, args.bundle, data, git_executable())
    bootstrap.emit(**{'kind': result.pop('type'), **result})


if __name__ == '__main__':
    try:
        main()
    except SetupError as error:
        bootstrap.emit('error', code=error.code)
        sys.exit(1)
    except runtime.RuntimeBusy:
        bootstrap.emit('error', code='setup_busy')
        sys.exit(1)
    except runtime.UnsafeRuntime:
        bootstrap.emit('error', code='unsafe_runtime_path')
        sys.exit(1)
    except Exception:
        bootstrap.emit('error', code='setup_failed')
        sys.exit(1)
