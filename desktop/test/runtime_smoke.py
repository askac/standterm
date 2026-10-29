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
        for marker in [None, {'id': 'wrong', 'lease': 1}, {'id': 'a' * 64, 'lease': 99}]:
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


spec = importlib.util.spec_from_file_location('upgrade', Path(__file__).parents[1] / 'runtime_upgrade.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


@unittest.skipIf(sys.platform == 'darwin', 'Automatic retention awaits native macOS qualification')
class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='standterm-upgrade-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        for module in [runtime, upgrade.runtime]:
            patcher = patch.object(module, 'runtime_base', return_value=self.base)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(upgrade, 'cleanup', side_effect=cleanup.prune_obsolete)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, version, manifest=True, lease=2):
        payload = f'version = {version}\n'.encode()
        files = {'app.py': hashlib.sha256(payload).hexdigest()}
        identity = hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest()
        root = self.base / 'runtimes' / identity
        venv = runtime.venv_path(root)
        venv.mkdir(parents=True)
        (venv / 'dependency.bin').write_bytes(b'rebuildable dependency' * 100)
        (root / 'app.py').write_bytes(payload)
        for name, value in [('.standterm-bundle.json', {'id': identity}),
                            ('.standterm-venv.json', {'id': identity, 'lease': lease}),
                            ('.desktop-ready.json', {'id': identity}),
                            ('.standterm-migration.json', {'version': 1, 'destination': identity, 'source': None,
                                'schema': upgrade.DATA_SCHEMA, 'files': {}, 'complete': True}),
                            ('.standterm-retention.json', {'version': 1, 'cleanArtifacts': True})]:
            upgrade.atomic_json(root / name, value)
        if lease == 1:
            (root / '.setup.lock').touch()
        if manifest:
            upgrade.atomic_json(root / '.standterm-files.json', {'version': 1, 'id': identity, 'files': files})
        return root

    def browsers(self, root, authorized=True):
        data = {'browsers': [{'browser_id': 'a' * 64, 'public_key': 'fixture key', 'authorized_at': 1}] if authorized else []}
        upgrade.atomic_json(root / 'authorized/browsers.json', data)
        return data

    def migrate(self, current, previous):
        upgrade.atomic_json(self.base / 'active-runtime.json', {'version': 1, 'id': previous.name})
        upgrade.migrate(current)

    def test_migrate_readback_then_remove_all_old_runtimes(self):
        old, previous, current = [self.fixture(version) for version in range(1, 4)]
        expected = self.browsers(previous)
        self.migrate(current, previous)
        self.assertTrue(previous.exists())
        self.assertEqual(upgrade.read_data(current), {'browsers.json': expected})
        upgrade.finish(current, True)
        self.assertEqual(list((self.base / 'runtimes').iterdir()), [current])
        self.assertTrue(runtime.venv_path(current).is_dir())
        self.assertEqual(runtime.read_marker(self.base / 'active-runtime.json')['id'], current.name)
        self.assertEqual(cleanup.prune_obsolete(current), [])
        # Only authorization data is retained, never venvs or unchanged Core.
        self.assertFalse(any((self.base / 'retained-data').rglob('dependency.bin')))
        self.assertFalse(any((self.base / 'retained-data').rglob('app.py')))

    def test_unchecked_migrates_and_persists_across_startup(self):
        previous, current = [self.fixture(version) for version in [1, 2]]
        self.browsers(previous)
        self.migrate(current, previous)
        upgrade.finish(current, False)
        upgrade.activate(current)
        self.assertTrue(previous.exists())
        self.assertEqual(upgrade.read_data(current), upgrade.read_data(previous))
        self.assertEqual(cleanup.prune_obsolete(current), [])

    def test_interrupted_install_cannot_enable_cleanup_before_final_choice(self):
        previous, current = [self.fixture(version) for version in [1, 2]]
        (current / '.standterm-retention.json').unlink()
        self.browsers(previous)
        self.migrate(current, previous)
        upgrade.activate(current)
        self.assertTrue(previous.exists())
        self.assertEqual(cleanup.prune_obsolete(current), [])

    def test_empty_latest_authorization_never_revives_historical_grants(self):
        historical, previous, current = [self.fixture(version) for version in [1, 2, 3]]
        self.browsers(historical)
        expected = self.browsers(previous, False)
        self.migrate(current, previous)
        self.assertEqual(upgrade.read_data(current), {'browsers.json': expected})
        upgrade.finish(current, True)
        self.assertFalse(historical.exists())

    def test_only_explicit_predecessor_and_no_pairing_request_migration(self):
        previous, current = [self.fixture(version) for version in [1, 2]]
        self.browsers(previous)
        upgrade.atomic_json(previous / 'authorized/browser-authorize_fixture.json', {'type': 'request'})
        upgrade.migrate(current)
        self.assertEqual(upgrade.read_data(current), {})
        upgrade.migrate(current, previous.name)
        self.assertEqual(upgrade.read_data(current), upgrade.read_data(previous))
        self.assertFalse((current / 'authorized/browser-authorize_fixture.json').exists())

    def test_schema_malformed_data_and_conflicts_leave_source_untouched(self):
        previous, current = [self.fixture(version) for version in [1, 2]]
        self.browsers(previous)
        for path, invalid in [('authorized/browsers.json', {'browsers': [{}]}),
                              ('.standterm-data-schema.json', {'version': 99})]:
            target = previous / path
            original = target.read_bytes() if target.exists() else None
            upgrade.atomic_json(target, invalid)
            with self.assertRaises(upgrade.MigrationError):
                self.migrate(current, previous)
            self.assertTrue(runtime.venv_path(previous).exists())
            if original is None:
                target.unlink()
            else:
                target.write_bytes(original)
        self.browsers(current, False)
        with self.assertRaises(upgrade.MigrationError):
            self.migrate(current, previous)
        self.assertTrue(previous.exists())

    def test_pending_copy_retries_and_completed_copy_preserves_destination_changes(self):
        previous, current = [self.fixture(version) for version in [1, 2]]
        self.browsers(previous)
        original = upgrade.atomic_json
        def fail(path, value):
            if path == current / '.standterm-data-schema.json':
                raise OSError('Fixture interruption after copying')
            original(path, value)
        with patch.object(upgrade, 'atomic_json', side_effect=fail), self.assertRaises(OSError):
            self.migrate(current, previous)
        self.assertFalse(runtime.read_marker(current / '.standterm-migration.json')['complete'])
        self.migrate(current, previous)
        self.browsers(current, False)
        upgrade.migrate(current)
        self.assertEqual(upgrade.read_data(current)['browsers.json'], {'browsers': []})

    def test_source_revocation_after_copy_is_rechecked_before_cleanup(self):
        previous, current = [self.fixture(version) for version in [1, 2]]
        self.browsers(previous)
        self.migrate(current, previous)
        self.browsers(previous, False)
        with self.assertRaises(upgrade.MigrationError):
            upgrade.finish(current, True)
        self.assertEqual(runtime.read_marker(self.base / 'active-runtime.json')['id'], previous.name)
        with self.assertRaises(upgrade.MigrationError):
            upgrade.activate(current)
        result = cleanup.prune_obsolete(current)
        self.assertEqual(result[0]['reason'], 'source_data_changed')
        self.assertTrue(previous.exists())
        upgrade.migrate(current)
        self.assertEqual(upgrade.read_data(current)['browsers.json'], {'browsers': []})
        upgrade.finish(current, True)
        self.assertFalse(previous.exists())

    def test_both_source_and_destination_are_locked_during_copy(self):
        previous, current = [self.fixture(version) for version in [1, 2]]
        self.browsers(previous)
        original = upgrade.atomic_json
        def observed(path, value):
            if path == current / 'authorized/browsers.json':
                for root in [previous, current]:
                    with self.assertRaises(runtime.RuntimeBusy), runtime.lease(root):
                        pass
            original(path, value)
        with patch.object(upgrade, 'atomic_json', side_effect=observed):
            self.migrate(current, previous)

    def test_busy_unknown_lease_and_current_interpreter_are_retained(self):
        busy, unknown, interpreter, current = [self.fixture(version) for version in range(1, 5)]
        (unknown / '.standterm-venv.json').unlink()
        with runtime.lease(busy), patch.object(sys, 'prefix', str(runtime.venv_path(interpreter))):
            result = cleanup.prune_obsolete(current)
        self.assertIn({'id': busy.name, 'status': 'retained', 'reason': 'in_use'}, result)
        for root in [busy, unknown, interpreter, current]:
            self.assertTrue(root.exists())

    def test_unique_modified_files_archived_and_pristine_legacy_payload_verified(self):
        legacy = self.fixture(1, manifest=False)
        modified = self.fixture(2)
        current = self.fixture(3)
        (modified / 'app.py').write_text('user modification')
        for relative in ['authorized/unknown.json', 'certs/private.key', 'notes.txt']:
            target = modified / relative
            target.parent.mkdir(exist_ok=True)
            target.write_text('private data')
        cleanup.prune_obsolete(current)
        self.assertFalse(legacy.exists())
        self.assertFalse(modified.exists())
        preserved = list((self.base / 'retained-data').rglob('app.py'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), 'user modification')
        self.assertEqual(len(list((self.base / 'retained-data').rglob('private.key'))), 1)

    def test_partial_deletion_retries_retired_path_and_preserves_recreated_original(self):
        old, current = [self.fixture(version) for version in [1, 2]]
        original = cleanup.shutil.rmtree
        def fail(path):
            if path.name == runtime.venv_path(old).name:
                (path / 'dependency.bin').unlink(missing_ok=True)
                raise PermissionError('Fixture interruption')
            original(path)
        with patch.object(cleanup.shutil, 'rmtree', side_effect=fail):
            self.assertEqual(cleanup.prune_obsolete(current)[0]['status'], 'retained')
        self.assertFalse(old.exists())
        old.mkdir()
        (old / 'keep.txt').write_text('new owner')
        cleanup.prune_obsolete(current)
        self.assertEqual((old / 'keep.txt').read_text(), 'new owner')
        self.assertEqual(list((self.base / 'retired-runtimes').iterdir()), [])
        self.assertEqual(list((self.base / 'retirement-records').iterdir()), [])

    def test_invalid_manifest_and_linked_paths_do_not_delete_external_data(self):
        old, current = [self.fixture(version) for version in [1, 2]]
        manifest = old / '.standterm-files.json'
        saved = manifest.read_bytes()
        for value in ['{}', '[]', 'x' * (1024 * 1024 + 1)]:
            manifest.write_text(value)
            cleanup.prune_obsolete(current)
            self.assertTrue(runtime.venv_path(old).exists())
        manifest.write_bytes(saved)
        external = self.base / 'external'
        runtime.venv_path(old).rename(external)
        try:
            runtime.venv_path(old).symlink_to(external, target_is_directory=True)
        except OSError:
            external.rename(runtime.venv_path(old))
            self.skipTest('Symlink creation unavailable')
        cleanup.prune_obsolete(current)
        self.assertTrue((external / 'dependency.bin').exists())

    def test_source_git_restored_macos_and_unready_current_never_prune(self):
        old, current = [self.fixture(version) for version in [1, 2]]
        for root in [self.base / 'checkout', self.base / 'git-core/repo', self.base / 'restored' / current.name]:
            self.assertEqual(cleanup.prune_obsolete(root), [])
        with patch.object(sys, 'platform', 'darwin'):
            self.assertEqual(cleanup.prune_obsolete(current), [])
        (current / '.desktop-ready.json').write_text('{}')
        self.assertEqual(cleanup.prune_obsolete(current), [])
        self.assertTrue(old.exists())

    def test_legacy_locks_are_honored_and_windows_keeps_only_pruned_stub(self):
        old, current = self.fixture(1, lease=1), self.fixture(2)
        with runtime.file_lease(old / '.setup.lock'):
            self.assertEqual(cleanup.prune_obsolete(current)[0]['reason'], 'in_use')
        result = cleanup.prune_obsolete(current)
        self.assertFalse(runtime.venv_path(old).exists())
        self.assertEqual(old.exists(), sys.platform == 'win32')
        self.assertEqual(result[0]['status'], 'pruned_legacy' if sys.platform == 'win32' else 'removed')

    def test_owned_backend_waits_for_parent_window_ack_and_still_handles_eof(self):
        old = self.fixture(1)
        self.fixture(2)
        current = self.fixture(3)
        (current / 'desktop').mkdir()
        for name in ['backend.py', 'runtime.py', 'runtime_cleanup.py', 'runtime_upgrade.py']:
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

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows locked-file handling')
    def test_windows_open_dependency_is_retained_then_retried(self):
        import ctypes
        old, current = [self.fixture(version) for version in [1, 2]]
        dependency = runtime.venv_path(old) / 'dependency.bin'
        create = ctypes.windll.kernel32.CreateFileW
        create.restype = ctypes.c_void_p
        handle = create(str(dependency), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            self.assertEqual(cleanup.prune_obsolete(current)[0]['status'], 'retained')
            self.assertTrue(any(self.base.rglob('dependency.bin')))
        finally:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        cleanup.prune_obsolete(current)
        self.assertFalse(old.exists())
        self.assertEqual(list((self.base / 'retired-runtimes').iterdir()), [])

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows junction handling')
    def test_windows_nested_junction_does_not_delete_external_files(self):
        old, current = [self.fixture(version) for version in [1, 2]]
        external = self.base / 'external'
        external.mkdir()
        (external / 'keep.txt').write_text('external data')
        junction = runtime.venv_path(old) / 'linked'
        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(external)],
                       check=True, capture_output=True)
        cleanup.prune_obsolete(current)
        self.assertFalse(old.exists())
        self.assertEqual((external / 'keep.txt').read_text(), 'external data')


if __name__ == '__main__':
    unittest.main()
