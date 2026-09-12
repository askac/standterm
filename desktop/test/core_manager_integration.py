"""Run an installed bridge against local Git and copied test dependencies."""

import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from unittest.mock import patch


def main():
    stage = Path(sys.argv[1]).resolve()
    prepared_venv = Path(sys.prefix)
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Run with a prepared platform test venv.')
    temporary = Path(tempfile.mkdtemp(prefix='standterm-core-integration-')).resolve()
    if sys.platform == 'win32':
        os.environ['LOCALAPPDATA'] = str(temporary / 'local')
    else:
        os.environ['HOME'] = str(temporary / 'home')
    os.environ.update(STANDTERM_AGENT_RUNTIME_DIR=str(temporary / 'agent'),
                      STANDTERM_SESSION_RECOVERY_STORE=str(temporary / 'recovery.json'))
    bundle = stage / 'bundle'
    spec = importlib.util.spec_from_file_location('installed_manager', bundle / 'core_manager.py')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    data = manager.bootstrap.manifest(bundle)
    git = manager.git_executable()
    assert git, 'Install Git in the test environment.'
    origin = temporary / 'origin'
    shutil.copytree(bundle / 'core', origin)
    (origin / '.gitignore').write_text('authorized/\n__pycache__/\n*.pyc\n')
    # The downloaded bridge must never run, even when it is incompatible.
    (origin / 'desktop' / 'backend.py').write_text('raise RuntimeError("Downloaded bridge must not run")\n')

    def git_call(*args):
        result = subprocess.run([git, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                 '-c', 'commit.gpgsign=false', '-C', str(origin), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        assert result.returncode == 0, result.stderr

    git_call('init', '--initial-branch=main')
    git_call('add', '--all')
    git_call('commit', '-m', 'Create isolated Core fixture')

    def manage(action):
        return manager.manage_git(action, bundle, data, git, repository=str(origin), watch_parent=False)

    # Clone and snapshot are real; copy the existing test venv to avoid network
    # dependency resolution. Ordinary checks/imports/backend execution are real.
    with patch.object(manager, 'ready_git', return_value={}):
        manage('enable')
    root = manager.runtime.git_root()
    shutil.copytree(prepared_venv, manager.runtime.venv_path(root), symlinks=True)
    manager.atomic_json(root / '.git-ready.json', {'version': 1,
        'requirements': manager.requirements_digest(root / 'repo'), 'commit': 'fixture'})
    ready = manage('check')
    assert not ready['dirty']
    process = subprocess.Popen([ready['python'], '-u', str(bundle / 'backend.py'), '--git-core', ready['core_root']],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(process.stdout.readline)
            try:
                frame = json.loads(future.result(timeout=60))
            except Exception:
                process.kill()
                raise
        assert frame['type'] == 'standterm_desktop_ready'
        assert frame['core_bundle_id'] is None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(frame['origin'] + '/launcher/status',
            headers={'X-StandTerm-Launcher-Token': frame['launcher_token']})
        with opener.open(request, timeout=5) as response:
            assert json.load(response)['instance_id'] == frame['instance_id']
        try:
            manage('update')
            raise AssertionError('Live installed bridge must exclude updates')
        except manager.runtime.RuntimeBusy:
            pass
        # Models a dead outer launcher with the actual backend still alive:
        # recovery must consult the backend lease, not the outer process state.
        recovery = subprocess.run([sys.executable, '-I', str(bundle / 'core_manager.py'),
            '--bundle', str(bundle), '--action', 'recover'], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        assert json.loads(recovery.stdout.strip())['code'] == 'setup_busy', recovery.stderr
        process.stdin.close()
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    (origin / 'core_version.py').write_text((origin / 'core_version.py').read_text() + '\n# Integration update\n')
    git_call('add', '--all')
    git_call('commit', '-m', 'Advance isolated Core fixture')
    assert manage('update')['commit'] != ready['commit']

    damaged = manager.runtime.runtime_base() / 'runtimes' / data['id']
    damaged.mkdir(parents=True)
    (damaged / 'app.py').write_text('damaged fixture')
    (damaged / 'keep.txt').write_text('user data')

    def prepare_fixture(bundle, destination, metadata, payloads):
        destination.mkdir(parents=True)
        for name, payload in payloads.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        manager.atomic_json(destination / '.standterm-bundle.json', {'id': metadata['id']})
        manager.atomic_json(destination / '.desktop-ready.json', {'id': metadata['id']})
        shutil.copytree(prepared_venv, manager.runtime.venv_path(destination), symlinks=True)

    with patch.object(manager.bootstrap, 'prepare', prepare_fixture):
        recovered = manager.recover(bundle, data)
    assert (damaged / 'keep.txt').read_text() == 'user data'
    assert (damaged / 'app.py').read_text() == 'damaged fixture'
    fresh = subprocess.run([sys.executable, '-I', str(bundle / 'bootstrap.py'), '--bundle', str(bundle)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    assert fresh.returncode == 0, fresh.stderr
    assert json.loads(fresh.stdout)['root'] == recovered['root']
    restored_root = Path(recovered['root'])
    restored = subprocess.Popen([recovered['python'], '-u', str(restored_root / 'desktop' / 'backend.py')],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(restored.stdout.readline)
            try:
                restored_frame = json.loads(future.result(timeout=60))
            except Exception:
                restored.kill()
                raise
        assert restored_frame['type'] == 'standterm_desktop_ready'
        assert restored_frame['core_bundle_id'] == data['id']
        restored.stdin.close()
        assert restored.wait(timeout=15) == 0
    finally:
        if restored.poll() is None:
            restored.kill()
            restored.wait(timeout=10)
    assert manage('check')['core_root'] == ready['core_root']
    print('Staged Core integration passed: installed bridge, authentication, live lease, Git update and persistent recovery.')
    print('Isolated fixtures retained at: ' + str(temporary))


if __name__ == '__main__':
    main()
