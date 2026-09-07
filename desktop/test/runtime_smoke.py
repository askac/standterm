"""Synthetic runtime cleanup tests; never enumerate actual installed runtimes."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).parents[1] / 'runtime_cleanup.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)
runtime = cleanup.runtime


class RuntimeTests(unittest.TestCase):
    def fixture(self, marker=True):
        base = Path(tempfile.mkdtemp(prefix='standterm-runtime-test-'))
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


if __name__ == '__main__':
    unittest.main()
