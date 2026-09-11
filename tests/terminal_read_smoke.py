"""Exercise bounded terminal reads without network connections or user shells."""
import codecs
from collections import deque
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_backends.base import TerminalBridge
from terminal_backends.local_shell import LocalShellBridge, LOCAL_SHELL_IDLE_WAIT_SECONDS
from terminal_backends.ssh import SSHBridge, SSH_READ_IDLE_SECONDS


class Runtime:
    max_replay_events = 1000
    max_replay_bytes = 1024 * 1024
    close_process = None
    update_headless_mirror = None

    def __init__(self):
        self.events = []
        self.sleeps = []
        self.unregistered = []
        self.on_sleep = lambda _seconds: None

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > 1000:
            raise AssertionError('Read loop did not stop')
        self.on_sleep(seconds)

    def emit_socket(self, _event, payload, room=None):
        self.events.append(dict(payload))

    def build_metadata(self, *_args): return {}
    def append_transcript(self, *_args): pass
    def unregister_bridge(self, *args): self.unregistered.append(args)


class Channel:
    def __init__(self, data, *, exited=True):
        self.data = data
        self.exited = exited
        self.reads = 0

    def recv_ready(self): return bool(self.data)
    def exit_status_ready(self): return self.exited

    def recv(self, size):
        self.reads += 1
        part, self.data = self.data[:size], self.data[size:]
        return part


def ssh_bridge(channel):
    runtime = Runtime()
    bridge = SSHBridge.__new__(SSHBridge)
    TerminalBridge.__init__(bridge, 'fixture-owner', 'main', runtime=runtime)
    bridge._output_decoder = codecs.getincrementaldecoder('utf-8')(errors='ignore')
    bridge.channel = channel
    bridge.attach('fixture-viewer')
    return bridge, runtime


def local_bridge(process):
    runtime = Runtime()
    config = {'shell_display': 'fixture', 'shell_command': ['fixture'],
              'terminal_kind': 'local', 'terminal_label': 'fixture'}
    bridge = LocalShellBridge('fixture-owner', 'main', config, ssh_term='xterm-256color',
                              get_default_local_shell_config=lambda: (config, None), runtime=runtime)
    bridge.process = process
    bridge.attach('fixture-viewer')
    return bridge, runtime


class Process:
    fd = 999

    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.sizes = []

    def read(self, size):
        self.sizes.append(size)
        if not self.chunks:
            raise EOFError()
        return self.chunks.popleft()

    def isalive(self): return False


class TerminalReadTests(unittest.TestCase):
    def terminal_text(self, runtime):
        return ''.join(event['data'] for event in runtime.events if event['message_type'] == 'terminal')

    def assert_closed_last(self, runtime):
        self.assertEqual([event['message_type'] for event in runtime.events].count('ssh_closed'), 1)
        self.assertEqual(runtime.events[-1]['message_type'], 'ssh_closed')
        self.assertNotIn('error_code', runtime.events[-1])

    def test_ssh_batches_preserve_utf8_control_sequences_and_exit_tail(self):
        # Force both UTF-8 and VT sequences across packet and batch boundaries.
        text = '\x1b[?2026h' + ('x' * 4090 + '\u4e2d\u6587') * 64 + '\x1b[?2026lEND'
        bridge, runtime = ssh_bridge(Channel(text.encode('utf-8')))
        with patch('terminal_backends.ssh.time.monotonic', return_value=0):
            bridge.read_loop()
        self.assertEqual(self.terminal_text(runtime), text)
        self.assert_closed_last(runtime)
        self.assertTrue(runtime.unregistered)
        self.assertTrue(all(wait == 0 for wait in runtime.sleeps))
        packets = [event['data'] for event in runtime.events if event['message_type'] == 'terminal']
        self.assertGreater(len(packets), 1)
        self.assertTrue(all(len(data.encode('utf-8')) <= 65539 for data in packets))
        self.assertEqual(len(packets), len(runtime.sleeps))

    def test_ssh_batch_also_has_a_time_bound(self):
        bridge, runtime = ssh_bridge(Channel(b'x' * 8192))
        with patch('terminal_backends.ssh.time.monotonic', side_effect=[0, .003, .004, .007]):
            bridge.read_loop()
        self.assertEqual(len(runtime.events), 3)
        self.assertEqual(self.terminal_text(runtime), 'x' * 8192)

    def test_ssh_idle_waits_and_can_be_closed(self):
        bridge, runtime = ssh_bridge(Channel(b'', exited=False))
        runtime.on_sleep = lambda seconds: setattr(bridge, 'channel', None) if seconds else None
        bridge.read_loop()
        self.assertEqual(runtime.sleeps, [0, SSH_READ_IDLE_SECONDS, 0])
        self.assertFalse(runtime.events)
        self.assertTrue(runtime.unregistered)

    def test_ssh_read_error_preserves_prior_data(self):
        channel = Channel(b'head')
        channel.recv_ready = lambda: True
        channel.recv = unittest.mock.Mock(side_effect=[b'head', OSError('fixture read failure')])
        bridge, runtime = ssh_bridge(channel)
        bridge.read_loop()
        self.assertEqual(self.terminal_text(runtime), 'head')
        self.assertEqual(runtime.events[-1].get('error_code'), 'ssh_read_error')

    def test_posix_ready_output_has_no_timed_sleep(self):
        encoded = '\u4e2d\u6587'.encode('utf-8')
        bridge, runtime = local_bridge(Process([encoded[:2], encoded[2:]]))
        with patch('terminal_backends.local_shell.sys.platform', 'linux'), \
                patch('terminal_backends.local_shell.select.select', return_value=([999], [], [])) as ready:
            bridge.read_loop()
        self.assertEqual(self.terminal_text(runtime), '\u4e2d\u6587')
        self.assert_closed_last(runtime)
        self.assertEqual(runtime.sleeps, [0, 0, 0])
        self.assertTrue(all(call.args[-1] == LOCAL_SHELL_IDLE_WAIT_SECONDS for call in ready.call_args_list))

    def test_posix_idle_uses_readiness_wait(self):
        bridge, runtime = local_bridge(Process([]))
        with patch('terminal_backends.local_shell.sys.platform', 'linux'), \
                patch('terminal_backends.local_shell.select.select', return_value=([], [], [])) as ready:
            bridge.read_loop()
        self.assertEqual(ready.call_args.args[-1], LOCAL_SHELL_IDLE_WAIT_SECONDS)
        self.assertEqual(runtime.sleeps, [0])
        self.assert_closed_last(runtime)

    def test_windows_dead_process_is_drained_before_closed(self):
        bridge, runtime = local_bridge(Process(['head', 'tail']))
        with patch('terminal_backends.local_shell.sys.platform', 'win32'):
            bridge.read_loop()
        self.assertEqual(self.terminal_text(runtime), 'headtail')
        self.assert_closed_last(runtime)
        self.assertEqual(runtime.sleeps, [0, 0, 0])

    def test_windows_empty_live_read_waits(self):
        process = Process([''])
        process.isalive = lambda: True
        bridge, runtime = local_bridge(process)
        self.assertTrue(bridge._read_windows_once())
        self.assertEqual(runtime.sleeps, [LOCAL_SHELL_IDLE_WAIT_SECONDS])


if __name__ == '__main__':
    unittest.main()
