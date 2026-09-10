"""Read-only bundle checks and managed-setup failure boundary tests."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('desktop_bootstrap', Path(__file__).parents[1] / 'bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def fixture(self, lease_aware=False):
        root = Path(tempfile.mkdtemp(prefix='standterm-bootstrap-test-')).resolve()
        bundle = root / 'bundle'
        files = {}
        names = ['app.py', 'desktop/backend.py', 'requirements.txt']
        if lease_aware:
            names.append('desktop/runtime.py')
        for name in names:
            source = bundle / 'core' / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b'test\n')
            files[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        identity = hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest()
        data = {'version': 1, 'id': identity, 'files': files}
        (bundle / 'manifest.json').write_text(json.dumps(data))
        return root, bundle, data

    def test_manifest_rejects_tampering(self):
        _, bundle, data = self.fixture()
        self.assertEqual(bootstrap.manifest(bundle), data)
        (bundle / 'core' / 'app.py').write_text('changed')
        with self.assertRaises(bootstrap.SetupError) as result:
            bootstrap.manifest(bundle)
        self.assertEqual(result.exception.code, 'invalid_bundle')

    def test_manifest_rejects_traversal_and_links(self):
        root, bundle, data = self.fixture()
        files = {'../escape': hashlib.sha256(b'test').hexdigest()}
        data.update(files=files, id=hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest())
        (bundle / 'manifest.json').write_text(json.dumps(data))
        with self.assertRaises(bootstrap.SetupError):
            bootstrap.manifest(bundle)
        linked = root / 'linked'
        try:
            linked.symlink_to(bundle, target_is_directory=True)
        except OSError:
            self.skipTest('Creating directory symlinks is not permitted on this test platform.')
        with self.assertRaises(bootstrap.SetupError):
            bootstrap.safe_directory(linked / 'nested')

    def test_prepare_refuses_unowned_or_modified_directories(self):
        root, bundle, data = self.fixture()
        runtime = root / 'runtime'
        runtime.mkdir()
        existing = runtime / 'keep.txt'
        existing.write_text('user data')
        with self.assertRaises(bootstrap.SetupError) as result:
            bootstrap.prepare(bundle, runtime, data)
        self.assertEqual(result.exception.code, 'unsafe_runtime_path')
        self.assertEqual(existing.read_text(), 'user data')
        owned = root / 'owned'
        owned.mkdir()
        (owned / '.standterm-bundle.json').write_text(json.dumps({'id': data['id']}))
        (owned / 'app.py').write_text('user edit')
        with self.assertRaises(bootstrap.SetupError) as result:
            bootstrap.prepare(bundle, owned, data)
        self.assertEqual(result.exception.code, 'modified_runtime')
        self.assertEqual((owned / 'app.py').read_text(), 'user edit')

    def test_dependency_failure_keeps_core_and_does_not_mark_ready(self):
        root, bundle, data = self.fixture()
        runtime = root / 'runtime'
        class Child:
            def __init__(self, args, **kwargs):
                self.args = args
                self.pid = 999999
            def wait(self, **kwargs):
                return 1 if 'pip' in self.args else 0
        with patch.object(bootstrap.threading.Thread, 'start'), patch.object(bootstrap.subprocess, 'Popen', Child):
            with self.assertRaises(bootstrap.SetupError) as result:
                bootstrap.prepare(bundle, runtime, data)
            self.assertEqual(result.exception.code, 'dependencies_failed')
            self.assertFalse((runtime / '.desktop-ready.json').exists())
            self.assertEqual((runtime / 'app.py').read_bytes(), b'test\n')
            with self.assertRaises(bootstrap.SetupError) as retry:
                bootstrap.prepare(bundle, runtime, data)
            self.assertEqual(retry.exception.code, 'dependencies_failed')

    def test_only_lease_aware_bundles_opt_partial_venvs_into_cleanup(self):
        class Child:
            pid = 999999
            def __init__(self, args, **kwargs):
                self.args = args
            def wait(self, **kwargs):
                return 1 if 'pip' in self.args else 0
        for aware in [False, True]:
            root, bundle, data = self.fixture(lease_aware=aware)
            target = root / 'runtime'
            with patch.object(bootstrap.threading.Thread, 'start'), patch.object(bootstrap.subprocess, 'Popen', Child):
                with self.assertRaises(bootstrap.SetupError):
                    bootstrap.prepare(bundle, target, data)
            marker = target / '.standterm-venv.json'
            self.assertEqual(marker.exists(), aware)
            if aware:
                self.assertEqual(json.loads(marker.read_text()), {'id': data['id'], 'lease': 1})

    def test_parent_disconnect_cancels_setup(self):
        root, bundle, _ = self.fixture()
        runtime = root / 'runtime'
        child = subprocess.Popen([sys.executable, str(Path(bootstrap.__file__)), '--bundle', str(bundle),
            '--test-root', str(runtime), '--prepare'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        try:
            while True:
                line = child.stdout.readline()
                self.assertTrue(line, 'setup must reach the venv stage')
                frame = json.loads(line)
                if frame.get('stage') == 'venv':
                    break
            child.stdin.close()
            child.stdin = None
            output, _ = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 1)
            self.assertEqual(json.loads(output.strip().splitlines()[-1])['code'], 'setup_canceled')
            self.assertFalse((runtime / '.desktop-ready.json').exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    @unittest.skipIf(sys.platform == 'win32', 'POSIX process groups only')
    def test_stubborn_process_group_is_killed(self):
        child = subprocess.Popen([sys.executable, '-c',
            'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'pid=os.fork(); print(pid,flush=True) if pid else None; time.sleep(600)'],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        descendant = int(child.stdout.readline())
        try:
            bootstrap.stop_child(child)
            self.assertIsNotNone(child.poll())
            self.assert_process_dead(descendant)
        finally:
            if child.poll() is None:
                bootstrap.stop_child(child)
            child.stdout.close()

    @unittest.skipIf(sys.platform == 'win32', 'POSIX process groups only')
    def test_exited_leader_does_not_hide_descendant(self):
        child = subprocess.Popen([sys.executable, '-c',
            'import os,signal,time; pid=os.fork(); '
            'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'print(pid,flush=True) if pid else time.sleep(600)'],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        descendant = int(child.stdout.readline())
        try:
            child.wait(timeout=5)
            bootstrap.stop_child(child)
            self.assert_process_dead(descendant)
        finally:
            bootstrap.stop_child(child)
            child.stdout.close()

    def assert_process_dead(self, pid):
        deadline = time.monotonic() + 5
        while True:
            # macOS has no /proc; inspect only the child PID owned by this test.
            result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 1 or result.stdout.strip().startswith('Z'):
                return
            self.assertEqual(result.returncode, 0, result.stderr)
            if time.monotonic() >= deadline:
                self.fail(f'Owned descendant {pid} is still running')
            time.sleep(0.02)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX process groups only')
    def test_bootstrap_eof_reaps_descendant_before_exit(self):
        root, bundle, _ = self.fixture()
        runtime = root / 'runtime'
        pid_file = root / 'descendant'
        leader = ('import os,signal,time; from pathlib import Path; pid=os.fork(); '
            'signal.signal(signal.SIGTERM,signal.SIG_IGN) if not pid else None; '
            f'Path({str(pid_file)!r}).write_text(str(os.getpid())) if not pid else None; '
            'time.sleep(600)')
        runner = (f'import sys; sys.path.insert(0,{str(Path(bootstrap.__file__).parent)!r}); '
            'import bootstrap; original=bootstrap.subprocess.Popen; '
            f'bootstrap.subprocess.Popen=lambda args,**kwargs: original([sys.executable,"-c",{leader!r}],**kwargs); '
            '\ntry: bootstrap.main()\nexcept bootstrap.SetupError as error: '
            'bootstrap.emit("error",code=error.code); sys.exit(1)')
        child = subprocess.Popen([sys.executable, '-c', runner, '--bundle', str(bundle),
            '--test-root', str(runtime), '--prepare'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        descendant = None
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() or not pid_file.read_text():
                self.assertIsNone(child.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            descendant = int(pid_file.read_text())
            child.stdin.close()
            child.stdin = None
            output, errors = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 1, errors)
            self.assertEqual(json.loads(output.strip().splitlines()[-1])['code'], 'setup_canceled')
            self.assertFalse((runtime / '.desktop-ready.json').exists())
            self.assert_process_dead(descendant)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if descendant:
                import os
                import signal
                try:
                    os.kill(descendant, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    unittest.main()
