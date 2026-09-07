"""Shared lease for managed setup, backend lifetime and optional venv removal."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
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
    return (Path(os.environ['LOCALAPPDATA']) / 'StandTermDesktop' if sys.platform == 'win32'
            else Path.home() / '.local' / 'share' / 'standterm-desktop')


def venv_path(root):
    return root / 'tools' / ('.venv_win' if sys.platform == 'win32' else '.venv_wsl')


@contextmanager
def lease(root, create=True):
    # Never unlink this file: waiters must keep locking the same inode.
    lock_path = root / '.setup.lock'
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
