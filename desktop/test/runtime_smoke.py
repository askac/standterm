"""Synthetic runtime cleanup tests; never enumerate actual installed runtimes."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).parents[1] / 'runtime_cleanup.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)
runtime = cleanup.runtime


class RuntimeTests(unittest.TestCase):
    def test_macos_uses_application_support_and_its_own_venv(self):
        with patch.object(sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path('/Users/test')):
            self.assertEqual(runtime.runtime_base(), Path('/Users/test/Library/Application Support/StandTermDesktop'))
            self.assertEqual(runtime.venv_path(Path('/runtime')), Path('/runtime/tools/.venv_macos'))

    def fixture(self, marker=True):
        base = Path(tempfile.mkdtemp(prefix='standterm-runtime-test-')).resolve()
        root = base / 'runtimes' / ('a' * 64)
        venv = runtime.venv_path(root)
        venv.mkdir(parents=True)
        (venv / 'keep.bin').write_bytes(b'important\x00data')
        (root / 'app.py').write_text('user Core file')
        (root / '.standterm-bundle.json').write_text(json.dumps({'id': root.name}))
        (root / '.setup.lock').touch()
        if marker:
            (root / '.standterm-venv.json').write_text(json.dumps({'id': root.name, 'lease': 1}))
        return base, root, venv

    def test_detach_is_recoverable_and_preserves_core(self):
        base, root, venv = self.fixture()
        result = cleanup.detach(base, [root.name])[0]
        self.assertEqual(result['status'], 'detached')
        self.assertFalse(venv.exists())
        recovery = Path(result['recovery'])
        self.assertEqual((recovery / venv.name / 'keep.bin').read_bytes(), b'important\x00data')
        self.assertEqual(json.loads((recovery / 'restore.json').read_text())['source'], str(venv))
        self.assertEqual((root / 'app.py').read_text(), 'user Core file')
        self.assertEqual(cleanup.detach(base, [root.name])[0]['reason'], 'absent')
        (recovery / venv.name).rename(venv)
        self.assertTrue((venv / 'keep.bin').exists())

    def test_legacy_unowned_and_modified_markers_are_retained(self):
        for marker in [None, {'id': 'wrong', 'lease': 1}, {'id': 'a' * 64, 'lease': 2}]:
            base, root, venv = self.fixture(marker=False)
            if marker:
                (root / '.standterm-venv.json').write_text(json.dumps(marker))
            self.assertEqual(cleanup.detach(base, [root.name])[0]['status'], 'retained')
            self.assertTrue(venv.is_dir())

    def test_busy_backend_or_setup_retains_venv(self):
        base, root, venv = self.fixture()
        runner = ('import importlib.util,sys; from pathlib import Path; '
            f'spec=importlib.util.spec_from_file_location("runtime", {str(Path(runtime.__file__))!r}); '
            'm=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); '
            f'lease=m.lease(Path({str(root)!r})); lease.__enter__(); '
            'print("locked",flush=True); sys.stdin.buffer.read()')
        child = subprocess.Popen([sys.executable, '-I', '-c', runner], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            self.assertEqual(cleanup.detach(base, [root.name])[0]['reason'], 'in_use')
            self.assertTrue(venv.is_dir())
            with self.assertRaises(runtime.RuntimeBusy):
                with runtime.lease(root):
                    self.fail('Concurrent lease granted')
        finally:
            child.communicate(timeout=10)
        self.assertEqual(cleanup.detach(base, [root.name])[0]['status'], 'detached')

    def test_cleanup_lock_blocks_backend_start_and_setup(self):
        _, root, _ = self.fixture()
        # Independent handles exercise flock/locking, not an in-memory registry.
        with runtime.lease(root):
            with self.assertRaises(runtime.RuntimeBusy):
                with runtime.lease(root):
                    self.fail('Concurrent lease granted')

    def test_actual_backend_holds_lease_before_core_import_until_exit(self):
        base, root, venv = self.fixture()
        (root / 'desktop').mkdir()
        for name in ['backend.py', 'runtime.py']:
            shutil.copyfile(Path(__file__).parents[1] / name, root / 'desktop' / name)
        # A stand-in Core import pauses on stdin without requiring dependencies.
        (root / 'app.py').write_text('import sys\nprint("core_imported", file=sys.stderr, flush=True)\n'
                                     'sys.stdin.buffer.read()\nraise SystemExit(0)\n')
        child = subprocess.Popen([sys.executable, '-I', str(root / 'desktop' / 'backend.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stderr.readline().strip(), 'core_imported')
            self.assertEqual(cleanup.detach(base, [root.name])[0]['reason'], 'in_use')
            self.assertTrue(venv.is_dir())
        finally:
            child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)
        with runtime.lease(root):
            blocked = subprocess.run([sys.executable, '-I', str(root / 'desktop' / 'backend.py')],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertNotIn('core_imported', blocked.stderr)
        self.assertEqual(cleanup.detach(base, [root.name])[0]['status'], 'detached')

    def test_linked_parent_and_venv_are_retained(self):
        base, root, venv = self.fixture()
        external = base / 'external'
        venv.rename(external)
        try:
            venv.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        self.assertEqual(cleanup.detach(base, [root.name])[0]['status'], 'retained')
        self.assertTrue((external / 'keep.bin').is_file())

    def test_cleanup_interpreter_and_failed_rename_are_retained(self):
        base, root, venv = self.fixture()
        with patch.object(sys, 'prefix', str(venv)):
            self.assertEqual(cleanup.detach(base, [root.name])[0]['reason'], 'cleanup_interpreter')
        with patch.object(cleanup.os, 'rename', side_effect=PermissionError()):
            self.assertEqual(cleanup.detach(base, [root.name])[0]['status'], 'retained')
        self.assertTrue((venv / 'keep.bin').exists())

    def test_inventory_is_read_only_and_empty_selection_never_moves(self):
        base, root, venv = self.fixture()
        self.assertEqual(cleanup.inventory(base)[0]['status'], 'candidate')
        self.assertFalse((base / 'venv-recovery').exists())
        self.assertEqual(cleanup.detach(base, [])[0]['status'], 'retained')
        self.assertTrue(venv.is_dir())
        with runtime.lease(root):
            self.assertEqual(cleanup.detach(base, [root.name])[0]['reason'], 'in_use')
        self.assertTrue(venv.is_dir())

    def test_oversized_inventory_fails_before_any_move(self):
        base, root, venv = self.fixture()
        for index in range(257):
            (base / 'runtimes' / f'{index:064x}').mkdir()
        with self.assertRaises(runtime.UnsafeRuntime):
            cleanup.detach(base, [root.name])
        self.assertTrue(venv.is_dir())
        self.assertFalse((base / 'venv-recovery').exists())

    def test_response_budget_is_checked_before_mutation(self):
        base, root, venv = self.fixture()
        original = cleanup.json.dumps
        with patch.object(cleanup.json, 'dumps', side_effect=lambda value: original(value) + (' ' * 65536)):
            with self.assertRaises(runtime.UnsafeRuntime):
                cleanup.detach(base, [root.name])
        self.assertTrue(venv.is_dir())
        self.assertFalse((base / 'venv-recovery').exists())


@unittest.skipIf(sys.platform == 'darwin', 'Automatic retention awaits native macOS qualification')
class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='standterm-retention-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        patcher = patch.object(runtime, 'runtime_base', return_value=self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, version, manifest=True):
        payload = f'version = {version}\n'.encode()
        files = {'app.py': hashlib.sha256(payload).hexdigest()}
        identity = hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest()
        root = self.base / 'runtimes' / identity
        venv = runtime.venv_path(root)
        venv.mkdir(parents=True)
        (venv / 'dependency.bin').write_bytes(b'rebuildable dependency' * 100)
        (root / 'app.py').write_bytes(payload)
        for name, value in [('.standterm-bundle.json', {'id': identity}),
                            ('.standterm-venv.json', {'id': identity, 'lease': 1}),
                            ('.desktop-ready.json', {'id': identity})]:
            (root / name).write_text(json.dumps(value))
        (root / '.setup.lock').touch()
        if manifest:
            (root / '.standterm-files.json').write_text(json.dumps({'version': 1, 'id': identity, 'files': files}))
        os.utime(root / '.desktop-ready.json', ns=(version * 1000000000, version * 1000000000))
        return root

    def test_retains_current_and_previous_and_preserves_all_user_files(self):
        old, previous, current = [self.fixture(version) for version in range(1, 4)]
        for relative in ['authorized/credentials.json', 'certs/private.key', 'notes.txt', 'setup.log']:
            target = old / relative
            target.parent.mkdir(exist_ok=True)
            target.write_text('user data')
        inode = (old / '.setup.lock').stat().st_ino
        result = cleanup.prune_obsolete(current)
        self.assertEqual(result, [{'id': old.name, 'status': 'pruned'}])
        self.assertFalse(runtime.venv_path(old).exists())
        self.assertFalse((old / 'app.py').exists())
        self.assertFalse((old / '.desktop-ready.json').exists())
        self.assertEqual((old / '.setup.lock').stat().st_ino, inode)
        for relative in ['authorized/credentials.json', 'certs/private.key', 'notes.txt', 'setup.log']:
            self.assertEqual((old / relative).read_text(), 'user data')
        for root in [previous, current]:
            self.assertTrue(runtime.venv_path(root).is_dir())
            self.assertTrue((root / 'app.py').is_file())
        self.assertFalse((self.base / 'venv-recovery').exists())
        self.assertEqual(cleanup.prune_obsolete(current)[0]['status'], 'pruned')

    def test_legacy_core_and_modified_source_are_preserved(self):
        legacy = self.fixture(1, manifest=False)
        modified = self.fixture(2)
        (modified / 'app.py').write_text('user modification')
        self.fixture(3)
        current = self.fixture(4)
        cleanup.prune_obsolete(current)
        self.assertTrue((legacy / 'app.py').exists())
        self.assertEqual((modified / 'app.py').read_text(), 'user modification')
        self.assertFalse(runtime.venv_path(legacy).exists())
        self.assertFalse(runtime.venv_path(modified).exists())

    def test_busy_unverified_and_cleanup_interpreter_are_retained(self):
        busy, unverified, interpreter, previous, current = [self.fixture(version) for version in range(1, 6)]
        (unverified / '.standterm-venv.json').write_text('{}')
        with runtime.lease(busy), patch.object(sys, 'prefix', str(runtime.venv_path(interpreter))):
            result = cleanup.prune_obsolete(current)
        self.assertIn({'id': busy.name, 'status': 'retained', 'reason': 'in_use'}, result)
        for root in [busy, unverified, interpreter, previous, current]:
            self.assertTrue(runtime.venv_path(root).is_dir())

    def test_partial_deletion_is_not_a_fallback_and_retries(self):
        old, previous, current = [self.fixture(version) for version in range(1, 4)]
        def fail_after_deleting_one_file(venv):
            (venv / 'dependency.bin').unlink()
            raise PermissionError('Fixture locked file')
        with patch.object(cleanup.shutil, 'rmtree', side_effect=fail_after_deleting_one_file):
            self.assertEqual(cleanup.prune_obsolete(current)[0]['status'], 'retained')
        self.assertTrue(runtime.venv_path(old).exists())
        self.assertFalse((old / '.desktop-ready.json').exists())
        cleanup.prune_obsolete(current)
        self.assertFalse(runtime.venv_path(old).exists())
        self.assertTrue(runtime.venv_path(previous).is_dir())

    def test_more_than_32_roots_are_reclaimed_independently(self):
        roots = [self.fixture(version) for version in range(1, 42)]
        (roots[0] / '.standterm-bundle.json').write_text('invalid')
        cleanup.prune_obsolete(roots[-1])
        self.assertTrue(runtime.venv_path(roots[0]).exists())
        self.assertEqual(sum(runtime.venv_path(root).is_dir() for root in roots), 3)

    def test_invalid_manifests_preserve_the_environment(self):
        old, previous, current = [self.fixture(version) for version in range(1, 4)]
        for content in ['[]', '{}', 'x' * (1024 * 1024 + 1)]:
            (old / '.standterm-files.json').write_text(content)
            cleanup.prune_obsolete(current)
            self.assertTrue(runtime.venv_path(old).exists())
            self.assertTrue((old / 'app.py').exists())

    def test_manifest_traversal_is_rejected_even_with_matching_digest(self):
        old = self.fixture(1)
        self.fixture(2)
        current = self.fixture(3)
        files = {'../outside': hashlib.sha256(b'private').hexdigest()}
        identity = hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest()
        forged = old.with_name(identity)
        old.rename(forged)
        for name, value in [('.standterm-bundle.json', {'id': identity}),
                            ('.standterm-venv.json', {'id': identity, 'lease': 1}),
                            ('.desktop-ready.json', {'id': identity}),
                            ('.standterm-files.json', {'version': 1, 'id': identity, 'files': files})]:
            (forged / name).write_text(json.dumps(value))
        os.utime(forged / '.desktop-ready.json', ns=(1000000000, 1000000000))
        cleanup.prune_obsolete(current)
        self.assertTrue(runtime.venv_path(forged).exists())

    def test_linked_core_parent_and_venv_are_not_followed(self):
        old = self.fixture(1)
        self.fixture(2)
        current = self.fixture(3)
        venv = runtime.venv_path(old)
        external = self.base / 'external'
        venv.rename(external)
        try:
            venv.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        cleanup.prune_obsolete(current)
        self.assertTrue((external / 'dependency.bin').is_file())
        venv.unlink()
        external.rename(venv)
        (old / 'app.py').unlink()
        (old / 'app.py').symlink_to(external)
        cleanup.prune_obsolete(current)
        self.assertTrue(venv.is_dir())

    def test_reprepared_runtime_after_inventory_is_retained(self):
        old = self.fixture(1)
        self.fixture(2)
        current = self.fixture(3)
        lease = runtime.lease
        def changed(root, **kwargs):
            os.utime(root / '.desktop-ready.json', None)
            return lease(root, **kwargs)
        with patch.object(runtime, 'lease', side_effect=changed):
            cleanup.prune_obsolete(current)
        self.assertTrue(runtime.venv_path(old).is_dir())

    def test_source_git_restored_macos_and_unready_current_never_prune(self):
        old = self.fixture(1)
        self.fixture(2)
        current = self.fixture(3)
        for root in [self.base / 'checkout', self.base / 'git-core' / 'repo', self.base / 'restored' / current.name]:
            self.assertEqual(cleanup.prune_obsolete(root), [])
        with patch.object(sys, 'platform', 'darwin'):
            self.assertEqual(cleanup.prune_obsolete(current), [])
        (current / '.desktop-ready.json').write_text('{}')
        self.assertEqual(cleanup.prune_obsolete(current), [])
        self.assertTrue(runtime.venv_path(old).is_dir())

    def test_owned_backend_waits_for_parent_window_ack_and_still_handles_eof(self):
        old = self.fixture(1)
        self.fixture(2)
        current = self.fixture(3)
        (current / 'desktop').mkdir()
        for name in ['backend.py', 'runtime.py', 'runtime_cleanup.py']:
            shutil.copyfile(Path(__file__).parents[1] / name, current / 'desktop' / name)
        # Override only this disposable backend's managed root, never HOME or a
        # live installation. Run the real backend, lease and retention code.
        with (current / 'desktop' / 'runtime.py').open('a') as output:
            output.write(f'\ndef runtime_base():\n    return Path({str(self.base)!r})\n')
        (current / 'app.py').write_text(
            'from types import SimpleNamespace\n'
            'app = SimpleNamespace(config={})\nsocketio = None\nactive_sessions = {}\n'
            'CORE_VERSION = "1.0.0"\nLAUNCHER_INSTANCE_ID = "fixture"\n'
            'LAUNCHER_SHUTDOWN_TOKEN = "fixture"\nSESSION_COOKIE_NAME = "fixture"\n'
            'def create_desktop_session(): return "fixture"\n'
            'def ensure_session_cleanup_task(): pass\n'
            'def write_external_agentinfo_files(**kwargs): pass\n'
            'def cleanup_external_agent_runtime_artifacts(): pass\n')
        (current / 'server_startup.py').write_text(
            'from contextlib import contextmanager\nimport threading\n'
            '@contextmanager\ndef bound_server(*args):\n'
            '    yield 54321, lambda: threading.Event().wait()\n'
            'def address_in_use(error): return False\n'
            'def suggested_port(*args): return None\n')
        original = (current / 'app.py').read_text()
        for failure in ['raise RuntimeError("Fixture import failure")\n',
                        'def create_desktop_session(): raise RuntimeError("Fixture session failure")\n']:
            (current / 'app.py').write_text(original + failure)
            failed = subprocess.run([sys.executable, '-I', str(current / 'desktop' / 'backend.py')],
                input='', capture_output=True, text=True, timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, '')
            self.assertTrue(runtime.venv_path(old).exists())
        (current / 'app.py').write_text(original)
        for acknowledge in [False, True]:
            child = subprocess.Popen([sys.executable, '-I', str(current / 'desktop' / 'backend.py')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                frame = json.loads(child.stdout.readline())
                self.assertEqual(frame['type'], 'standterm_desktop_ready')
                child.stdin.write('{"type":"standterm_desktop_window_ready","version":2}\n')
                child.stdin.flush()
                self.assertTrue(runtime.venv_path(old).exists())
                if acknowledge:
                    child.stdin.write('{"type":"standterm_desktop_window_ready","version":1}\n')
                    child.stdin.flush()
                    deadline = time.monotonic() + 10
                    while (old / 'app.py').exists():
                        self.assertIsNone(child.poll())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    self.assertFalse(runtime.venv_path(old).exists())
            finally:
                child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0)
            if not acknowledge:
                self.assertTrue(runtime.venv_path(old).exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows file locking')
    def test_windows_locked_dependency_retries_without_touching_user_data(self):
        import ctypes
        old = self.fixture(1)
        previous = self.fixture(2)
        current = self.fixture(3)
        dependency = runtime.venv_path(old) / 'dependency.bin'
        create = ctypes.windll.kernel32.CreateFileW
        create.restype = ctypes.c_void_p
        handle = create(str(dependency), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            result = cleanup.prune_obsolete(current)
            self.assertEqual(result[0]['status'], 'retained')
            self.assertTrue(dependency.exists())
            self.assertFalse((old / '.desktop-ready.json').exists())
            self.assertTrue(runtime.venv_path(previous).exists())
        finally:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        cleanup.prune_obsolete(current)
        self.assertFalse(runtime.venv_path(old).exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows junction handling')
    def test_windows_nested_junction_does_not_delete_external_files(self):
        old = self.fixture(1)
        self.fixture(2)
        current = self.fixture(3)
        external = self.base / 'external'
        external.mkdir()
        (external / 'keep.txt').write_text('external data')
        junction = runtime.venv_path(old) / 'linked'
        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(external)],
                       check=True, capture_output=True)
        cleanup.prune_obsolete(current)
        self.assertFalse(runtime.venv_path(old).exists())
        self.assertEqual((external / 'keep.txt').read_text(), 'external data')


if __name__ == '__main__':
    unittest.main()
