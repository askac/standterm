"""Isolated Git, archive and recovery lifecycle checks; no network or system pip."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('core_manager', Path(__file__).parents[1] / 'core_manager.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class CoreManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='standterm-git-test-')).resolve()
        self.base = self.temp / 'runtime-base'
        self.bundle = self.temp / 'bundle'
        files = {}
        for name in [*manager.REQUIRED, 'desktop/backend.py', 'desktop/runtime.py']:
            source = self.bundle / 'core' / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('# fixture\n')
            files[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        identity = hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest()
        self.data = {'version': 1, 'id': identity, 'files': files}
        (self.bundle / 'manifest.json').write_text(json.dumps(self.data))
        self.base_patch = patch.object(manager.runtime, 'runtime_base', return_value=self.base)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.git = shutil.which('git')

    def origin(self):
        if not self.git:
            self.skipTest('Git is not installed.')
        origin = self.temp / 'origin'
        shutil.copytree(self.bundle / 'core', origin)
        self.command(origin, 'init', '--initial-branch=main')
        self.commit(origin)
        return origin

    def command(self, root, *args):
        result = subprocess.run([self.git, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                 '-c', 'commit.gpgsign=false', '-C', str(root), *args],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, root):
        self.command(root, 'add', '--all')
        self.command(root, 'commit', '-m', 'Update fixture')

    def fake_environment(self):
        calls = []
        original = manager.Runner

        class Runner(original):
            def run(instance, args, *positional, **kwargs):
                if args[0] == self.git:
                    return super(Runner, instance).run(args, *positional, **kwargs)
                calls.append(args)
                if 'venv' in args:
                    python = manager.bootstrap.environment_python(instance.root)
                    python.parent.mkdir(parents=True, exist_ok=True)
                    python.write_text('fixture')
                return 0

        mocked = patch.object(manager, 'Runner', Runner)
        mocked.start()
        self.addCleanup(mocked.stop)
        return calls

    def manage(self, action, origin):
        return manager.manage_git(action, self.bundle, self.data, self.git,
                                  repository=str(origin), watch_parent=False)

    def assert_error(self, code, action):
        with self.assertRaises(manager.SetupError) as result:
            action()
        self.assertEqual(result.exception.code, code)

    def test_clone_fast_forward_and_requirements_readiness(self):
        origin = self.origin()
        calls = self.fake_environment()
        first = self.manage('enable', origin)
        self.assertFalse(first['dirty'])
        self.assertEqual(first['commit'], self.command(origin, 'rev-parse', 'HEAD'))
        self.assertEqual(sum('pip' in args for args in calls), 1)
        (origin / 'app.py').write_text('# second\n')
        self.commit(origin)
        second = self.manage('update', origin)
        self.assertNotEqual(first['commit'], second['commit'])
        self.assertEqual(sum('pip' in args for args in calls), 1)
        (origin / 'requirements.txt').write_text('# new requirements\n')
        self.commit(origin)
        self.manage('update', origin)
        self.assertEqual(sum('pip' in args for args in calls), 2)
        repo = manager.runtime.git_root() / 'repo'
        (repo / 'requirements.txt').write_text('# local requirements\n')
        self.assert_error('git_needs_setup', lambda: self.manage('check', origin))
        self.manage('prepare', origin)
        self.assertTrue(self.manage('check', origin)['dirty'])
        self.assertEqual(sum('pip' in args for args in calls), 3)

    def test_updates_preserve_dirty_untracked_diverged_and_changed_origin(self):
        origin = self.origin()
        self.fake_environment()
        self.manage('enable', origin)
        repo = manager.runtime.git_root() / 'repo'
        initial = self.command(repo, 'rev-parse', 'HEAD')
        (repo / 'keep.txt').write_text('keep me')
        self.assert_error('git_dirty', lambda: self.manage('update', origin))
        self.assertEqual((repo / 'keep.txt').read_text(), 'keep me')
        self.assertEqual(self.command(repo, 'rev-parse', 'HEAD'), initial)
        self.assertTrue(self.manage('check', origin)['dirty'])
        self.commit(repo)
        self.assert_error('git_diverged', lambda: self.manage('update', origin))
        self.command(repo, 'remote', 'set-url', 'origin', str(self.temp / 'other'))
        self.assert_error('git_source_changed', lambda: self.manage('update', origin))

    def test_missing_git_unowned_paths_and_live_lease_prevent_mutation(self):
        self.assert_error('git_required', lambda: manager.manage_git('enable', self.bundle, self.data, None))
        origin = self.origin()
        calls = self.fake_environment()
        with manager.owned_git() as root:
            with self.assertRaises(manager.runtime.RuntimeBusy):
                self.manage('enable', origin)
            self.assertFalse((root / 'repo').exists())
            self.assertEqual(calls, [])
        (root / '.standterm-git.json').write_text('{}')
        with self.assertRaises(manager.runtime.UnsafeRuntime):
            self.manage('enable', origin)

    def test_failed_dependency_install_never_marks_ready(self):
        origin = self.origin()
        self.fake_environment()
        original = manager.Runner.run

        def fail(instance, args, *rest, **kwargs):
            if 'pip' in args:
                raise manager.SetupError('dependencies_failed')
            return original(instance, args, *rest, **kwargs)

        with patch.object(manager.Runner, 'run', fail):
            self.assert_error('dependencies_failed', lambda: self.manage('enable', origin))
        root = manager.runtime.git_root()
        self.assertTrue((root / 'repo' / 'app.py').exists())
        self.assertIsNone(json.loads((root / '.git-ready.json').read_text())['requirements'])
        self.manage('enable', origin)

    def test_explicit_prepare_repairs_dependencies_and_invalid_ready_metadata(self):
        origin = self.origin()
        calls = self.fake_environment()
        self.manage('enable', origin)
        self.manage('prepare', origin)
        self.assertEqual(sum('--force-reinstall' in args for args in calls), 1)
        (manager.runtime.git_root() / '.git-ready.json').write_text('[]')
        self.assert_error('git_needs_setup', lambda: self.manage('check', origin))
        self.manage('prepare', origin)
        self.assertEqual(sum('--force-reinstall' in args for args in calls), 2)

    def test_archive_uses_installed_manifest_and_preserves_invalid_cache(self):
        root, payloads = manager.snapshot(self.bundle, self.data)
        self.assertEqual(set(payloads), set(self.data['files']))
        archive = root / 'core.tar.gz'
        archive.write_bytes(b'broken archive')
        (root / 'manifest.json').write_text('{}')
        _, repaired = manager.snapshot(self.bundle, self.data)
        self.assertEqual(payloads, repaired)
        self.assertEqual(len(list(root.glob('invalid-*.tar.gz'))), 1)
        self.assertEqual(json.loads((root / 'manifest.json').read_text()), self.data)
        # A valid gzip header with an invalid deflate block raises zlib.error,
        # rather than BadGzipFile or a tarfile exception.
        archive.write_bytes(bytes.fromhex('1f8b08000000000000ff07'))
        with self.assertRaises(manager.zlib.error):
            manager.read_archive(archive, self.bundle, self.data)
        _, repaired = manager.snapshot(self.bundle, self.data)
        self.assertEqual(payloads, repaired)

    def test_archive_rejects_traversal_duplicates_links_sizes_and_hashes(self):
        archive = self.temp / 'bad.tar.gz'
        for kind in ['traversal', 'duplicate', 'link', 'size', 'hash', 'missing']:
            with self.subTest(kind=kind):
                with tarfile.open(archive, 'w:gz') as output:
                    for index, name in enumerate(self.data['files']):
                        if kind == 'missing' and index == 0:
                            continue
                        payload = (self.bundle / 'core' / name).read_bytes()
                        info = tarfile.TarInfo('../app.py' if kind == 'traversal' and index == 0 else name)
                        if index == 0:
                            if kind == 'link':
                                info.type, info.linkname = tarfile.SYMTYPE, '/outside'
                            if kind == 'size':
                                payload += b'extra'
                            if kind == 'hash':
                                payload = b'x' * len(payload)
                        info.size = len(payload)
                        output.addfile(info, io.BytesIO(payload))
                        if kind == 'duplicate' and index == 0:
                            output.addfile(info, io.BytesIO(payload))
                self.assert_error('invalid_archive', lambda: manager.read_archive(archive, self.bundle, self.data))

    def test_recovery_keeps_damage_and_persists_constrained_selection(self):
        old = self.base / 'runtimes' / self.data['id']
        old.mkdir(parents=True)
        (old / 'app.py').write_text('damaged')
        (old / 'authorized').mkdir()
        (old / 'authorized' / 'keep').write_text('user data')
        prepared = []

        def prepare(bundle, root, data, payloads):
            prepared.append(root)
            root.mkdir(parents=True)
            (root / '.standterm-bundle.json').write_text(json.dumps({'id': data['id']}))
            for name, payload in payloads.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)

        with patch.object(manager.bootstrap, 'prepare', prepare), patch.object(manager.bootstrap, 'check_ready',
                side_effect=lambda root, data: root in prepared):
            result = manager.recover(self.bundle, self.data)
            selected = manager.runtime.bundled_root(self.base, self.data['id'])
            self.assertEqual(str(selected), result['root'])
            self.assertEqual(manager.recover(self.bundle, self.data)['root'], str(selected))
        self.assertEqual((old / 'app.py').read_text(), 'damaged')
        self.assertEqual((old / 'authorized' / 'keep').read_text(), 'user data')
        self.assertEqual((selected / 'app.py').read_bytes(), (self.bundle / 'core' / 'app.py').read_bytes())
        selection = self.base / 'core-recovery' / self.data['id'] / 'selected.json'
        selection.write_text(json.dumps({'version': 1, 'runtime': '../outside'}))
        with self.assertRaises(manager.runtime.UnsafeRuntime):
            manager.runtime.bundled_root(self.base, self.data['id'])

    def test_runner_cancels_owned_command_and_timeout(self):
        if sys.platform == 'win32':
            self.skipTest('Windows job termination runs in the parent-EOF subprocess test.')
        self.base.mkdir()
        runner = manager.Runner(self.base, watch_parent=False)
        timer = threading.Timer(0.2, runner.canceled.set)
        timer.start()
        try:
            self.assert_error('setup_canceled', lambda: runner.run([sys.executable, '-c', 'import time; time.sleep(60)']))
        finally:
            timer.cancel()
        runner = manager.Runner(self.base, watch_parent=False)
        with patch.object(manager, 'COMMAND_TIMEOUT', 0.1):
            self.assert_error('setup_timeout', lambda: runner.run([sys.executable, '-c', 'import time; time.sleep(60)']))

    def test_parent_eof_reaps_runner(self):
        self.base.mkdir()
        script = (f'import sys; sys.path.insert(0, {str(Path(manager.__file__).parent)!r}); import core_manager as m; '
                  f'r=m.Runner(m.Path({str(self.base)!r})); '
                  'm.bootstrap.emit("progress",stage="git"); '
                  '\ntry: r.run([sys.executable,"-c","import time; time.sleep(60)"])'
                  '\nexcept m.SetupError as e: m.bootstrap.emit("error",code=e.code); sys.exit(1)')
        child = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(json.loads(child.stdout.readline())['stage'], 'git')
            child.stdin.close()
            child.stdin = None
            output, errors = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 1, errors)
            self.assertEqual(json.loads(output.strip())['code'], 'setup_canceled')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


if __name__ == '__main__':
    unittest.main()
