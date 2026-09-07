"""Run with the project Windows venv; creates only test-owned processes."""

import ctypes
from ctypes import wintypes
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('standterm_windows_job', Path(__file__).parents[1] / 'windows_job.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def owned_child_gone(pid):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return True
    code = wintypes.DWORD()
    try:
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value != 259
    finally:
        api.CloseHandle(handle)


if '--job' in sys.argv:
    job = module.WindowsJob()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps({'child': child.pid}), flush=True)
    sys.stdin.buffer.read()
    job.terminate()
else:
    for crash in [False, True]:
        owner = subprocess.Popen([sys.executable, __file__, '--job'], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            pid = json.loads(owner.stdout.readline())['child']
            assert not owned_child_gone(pid)
            if crash:
                owner.kill()
            else:
                owner.stdin.close()
                owner.stdin = None
            owner.wait(timeout=15)
            deadline = time.monotonic() + 10
            while not owned_child_gone(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert owned_child_gone(pid), 'Job cleanup left a test child running'
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait()
            owner.stdout.close()
            if owner.stdin:
                owner.stdin.close()
    print('Windows Job cleanup passed: cancellation and owner crash terminate descendants.')
