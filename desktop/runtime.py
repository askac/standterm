"""Shared lease for managed setup, backend lifetime and optional venv removal."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import sys


class RuntimeBusy(Exception):
    pass


class UnsafeRuntime(Exception):
    pass


def linked(path):
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def check_path(path):
    if not path.is_absolute() or any(linked(item) for item in [path, *path.parents]):
        raise UnsafeRuntime()


def read_marker(path):
    check_path(path)
    if path.stat().st_size > 4096:
        raise UnsafeRuntime()
    return json.loads(path.read_text(encoding='utf-8'))


def runtime_base():
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'StandTermDesktop'
    return (Path(os.environ['LOCALAPPDATA']) / 'StandTermDesktop' if sys.platform == 'win32'
            else Path.home() / '.local' / 'share' / 'standterm-desktop')


def venv_path(root):
    name = '.venv_win' if sys.platform == 'win32' else '.venv_macos' if sys.platform == 'darwin' else '.venv_wsl'
    return root / 'tools' / name


def git_root():
    return runtime_base() / 'git-core'


def check_git_root(root):
    check_path(root)
    if root != git_root() or read_marker(root / '.standterm-git.json') != {'version': 1, 'kind': 'git-core'}:
        raise UnsafeRuntime()
    for relative in ['repo', 'repo/.git', 'tools']:
        check_path(root / relative)


def bundled_root(base, bundle_id):
    recovery = base / 'core-recovery' / bundle_id
    selection = recovery / 'selected.json'
    check_path(selection)
    if not selection.exists():
        return base / 'runtimes' / bundle_id
    saved = read_marker(selection)
    identity = saved.get('runtime') if isinstance(saved, dict) else None
    if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise UnsafeRuntime()
    # Keep Windows extension/DLL paths short; the full bundle hash belongs in
    # the selection record's directory, not every Python import path.
    root = base / 'restored' / identity
    if read_marker(root / '.standterm-bundle.json') != {'id': bundle_id}:
        raise UnsafeRuntime()
    return root


@contextmanager
def file_lease(lock_path, create=True):
    check_path(lock_path)
    with lock_path.open('a+b' if create else 'r+b') as lock:
        try:
            if sys.platform == 'win32':
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeBusy() from None
        try:
            yield
        finally:
            if sys.platform == 'win32':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def managed_identity(root):
    base = runtime_base()
    if root.parent == base / 'runtimes' and re.fullmatch('[a-f0-9]{64}', root.name):
        return root.name
    if root.parent == base / 'retired-runtimes' and re.fullmatch('[a-f0-9]{64}-[a-f0-9]{32}', root.name):
        return root.name[:64]
    return None


@contextmanager
def lease(root, create=True):
    identity = managed_identity(root)
    if identity is None:
        with file_lease(root / '.setup.lock', create=create):
            yield
        return
    # The stable lock outlives the disposable runtime directory. Never unlink
    # it: setup and retirement of the same bundle must use the same inode.
    locks = runtime_base() / 'runtime-locks'
    check_path(locks)
    locks.mkdir(mode=0o700, exist_ok=True)
    with file_lease(locks / (identity + '.lock')):
        check_path(root)
        if not root.is_dir():
            raise UnsafeRuntime()
        legacy = root / '.setup.lock'
        check_path(legacy)
        if legacy.exists():
            # Older backends only hold the in-directory lease. Honor it until
            # that runtime is retired; it may prevent renaming on Windows.
            with file_lease(legacy, create=False):
                yield
        else:
            yield
