"""Run with Python; no application dependencies or live terminals are required."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal_capabilities import build_capability_response
from terminal_backends.base import TerminalBridge
from terminal_backends.terminfo import add_local_rgb_terminfo, _rgb_terminfo


class RecordingBridge(TerminalBridge):
    def write(self, data):
        self.writes.append(data)


class CapabilityTests(unittest.TestCase):
    def test_truthful_bounded_responses(self):
        self.assertIn('StandTerm(', build_capability_response('version'))
        self.assertEqual(build_capability_response('terminfo', '524742;436f;5463'),
                         '\x1bP1+r524742=38\x1b\\\x1bP1+r436f=323536\x1b\\\x1bP1+r5463\x1b\\')
        self.assertEqual(build_capability_response('terminfo', '4d73;524742'), '\x1bP0+r\x1b\\')
        for names in ('', 'g1', '123', 'ff', '1b5b324a', '00'):
            result = build_capability_response('terminfo', names)
            self.assertIn(result, (None, '\x1bP0+r\x1b\\'))
        for names in (None, [], '41' * 1025, ';'.join(['524742'] * 17)):
            self.assertIsNone(build_capability_response('terminfo', names))
        self.assertIsNone(build_capability_response('input', 'rm -rf'))

    def test_replay_multiple_viewers_and_stale_generation(self):
        emitted = []
        runtime = SimpleNamespace(max_replay_events=256, max_replay_bytes=65536,
                                  append_transcript=lambda *args: None, update_headless_mirror=None,
                                  emit_socket=lambda *args, **kwargs: emitted.append((args, kwargs)))
        bridge = RecordingBridge('session', 'main', runtime=runtime)
        bridge.writes = []
        bridge.attach('first')
        bridge.attach('second')
        bridge.emit_output({'message_type': 'terminal', 'data': '\x1b[>q'})
        epoch = bridge.capability_epoch
        for _ in range(2):
            bridge.write_capability_response(epoch, 1, 0, 'version')
        bridge.write_capability_response(epoch, 1, 1, 'other-query')
        self.assertEqual(bridge.writes, ['version', 'other-query'])
        bridge.replay_to('second')
        self.assertTrue(emitted[-1][0][1]['replay'])
        self.assertNotIn('replay', bridge.replay_buffer[0])
        for args in [('old', 1, 2), (epoch, 2, 0), (epoch, True, 0), (epoch, 1, 32)]:
            bridge.write_capability_response(*args, 'invalid')
        bridge.output_seq = 300
        bridge.write_capability_response(epoch, 300, 0, 'fresh')
        bridge.write_capability_response(epoch, 1, 0, 'expired')
        self.assertEqual(bridge.writes, ['version', 'other-query', 'fresh'])
        self.assertEqual(list(bridge.capability_responses), [300])
        bridge.closing = True
        bridge.write_capability_response(epoch, 300, 1, 'closed')
        self.assertEqual(len(bridge.writes), 3)

    @unittest.skipUnless(shutil.which('tic') and shutil.which('infocmp'), 'ncurses tools unavailable')
    def test_effective_user_entry_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix='standterm-terminfo-custom-') as home:
            env = dict(os.environ, HOME=home, TERM='xterm-256color')
            env.pop('TERMINFO', None)
            env.pop('TERMINFO_DIRS', None)
            source = subprocess.check_output(['infocmp', '-x', '-1', 'xterm-256color'], env=env, text=True)
            source += '\tStandTermTestFlag,\n'
            path = Path(home) / 'custom.ti'
            path.write_text(source)
            subprocess.run(['tic', '-x', '-o', str(Path(home) / '.terminfo'), str(path)], check=True)
            add_local_rgb_terminfo(env)
            compiled = subprocess.check_output(['infocmp', '-x', '-1', 'xterm-256color'], env=env, text=True)
            self.assertIn('StandTermTestFlag,', compiled)
            self.assertIn('\tRGB,', compiled)
            shutil.rmtree(env['TERMINFO'])
            _rgb_terminfo.cache_clear()

    def test_preserve_overrides_and_fall_back(self):
        for extra in ({'TERMINFO': '/custom'}, {'TERMINFO_DIRS': ''}, {'TERM': 'vt100'}):
            env = dict(TERM='xterm-256color', **{'PATH': os.defpath})
            env.update(extra)
            before = dict(env)
            with patch('terminal_backends.terminfo._rgb_terminfo') as compile_entry:
                add_local_rgb_terminfo(env)
                compile_entry.assert_not_called()
            self.assertEqual(env, before)
        with patch('terminal_backends.terminfo.sys.platform', 'win32'):
            env = {'TERM': 'xterm-256color'}
            add_local_rgb_terminfo(env)
            self.assertNotIn('TERMINFO', env)
        _rgb_terminfo.cache_clear()
        self.assertIsNone(_rgb_terminfo('xterm-256color', '/nonexistent', '/tmp'))
        with patch('terminal_backends.terminfo.subprocess.run', side_effect=subprocess.TimeoutExpired('tic', 2)):
            self.assertIsNone(_rgb_terminfo('xterm-256color', os.defpath, '/tmp'))
        _rgb_terminfo.cache_clear()

    @unittest.skipUnless(shutil.which('tic') and shutil.which('infocmp'), 'ncurses tools unavailable')
    def test_compiled_overlay_preserves_base_and_system_fallback(self):
        with tempfile.TemporaryDirectory(prefix='standterm-terminfo-test-') as home:
            env = dict(os.environ, HOME=home, TERM='xterm-256color')
            env.pop('TERMINFO', None)
            env.pop('TERMINFO_DIRS', None)
            before = subprocess.check_output(['infocmp', '-x', '-1', 'xterm-256color'], env=env, text=True)
            add_local_rgb_terminfo(env)
            directory = env.get('TERMINFO')
            self.assertIsNotNone(directory)
            after = subprocess.check_output(['infocmp', '-x', '-1', 'xterm-256color'], env=env, text=True)
            lines = lambda text: {line.strip() for line in text.splitlines() if line.startswith('\t')}
            self.assertEqual(lines(after) - lines(before), {'RGB,', 'Tc,'})
            self.assertFalse(lines(before) - lines(after))
            subprocess.run(['infocmp', 'vt100'], env=env, check=True, capture_output=True)
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            shutil.rmtree(directory)
            _rgb_terminfo.cache_clear()


if __name__ == '__main__':
    unittest.main()
