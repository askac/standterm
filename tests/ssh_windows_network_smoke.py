"""Exercise real SSH through Windows networking with disposable WSL OpenSSH servers."""

import functools
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ssh_jump_smoke as fixture
from terminal_backends.windows_network import windows_network_executable, WindowsNetworkSocket


@unittest.skipUnless(windows_network_executable(), 'Requires WSL interoperability and Windows PowerShell')
class WindowsSSHTests(fixture.SSHJumpTests):
    def bridge(self, servers):
        bridge = super().bridge(servers)
        bridge.connect = functools.partial(bridge.connect, network_origin='windows')
        return bridge

    def test_windows_localhost_requires_trust_before_browser_key_or_local_authentication(self):
        servers = [self.stack.enter_context(fixture.server())]
        route = self.route(servers)
        route[0]['host_key_alias'] = ''
        bridge = self.bridge(servers)
        with patch.object(bridge, '_connect_with_local_keys') as keys:
            success, result = self.connect(bridge, route)
        self.assertFalse(success)
        self.assertEqual(result['error_code'], 'ssh_host_key_unknown')
        self.assertEqual(self.signatures, [])
        keys.assert_not_called()
        bridge.close()

        bridge = self.bridge(servers)
        prompts = []
        def reject_trust(value):
            if value['message_type'] == 'ssh_login_prompt':
                prompts.append(value['kind'])
                self.assertEqual(value['kind'], 'host_key')
                reply = {key: value[key] for key in ('attempt_id', 'node_id', 'kind', 'request_id')}
                bridge.resolve_login_input('test-sid', {**reply, 'terminal_id': 'main', 'accept': False})
        bridge.emit_output = reject_trust
        route[0]['browser_key'] = None
        with patch.object(bridge, '_connect_with_local_keys') as keys:
            success, result = bridge.connect('127.0.0.1', route[0]['port'], route[0]['username'],
                                             route=route, attempt_id='test-attempt', interactive_login=True)
        self.assertFalse(success)
        self.assertEqual(prompts, ['host_key'])
        keys.assert_not_called()

    def test_large_sftp_round_trip_preserves_origin_and_failure_never_uses_core_network(self):
        servers = [self.stack.enter_context(fixture.server())]
        route = self.route(servers)
        self.trust(route, servers)
        bridge = self.bridge(servers)
        success, result = self.connect(bridge, route)
        self.assertTrue(success, result)
        self.assertEqual(bridge.sftp_endpoint()['network_origin'], 'windows')
        with patch.object(fixture.TerminalBridge, 'metadata', return_value={}):
            self.assertEqual(bridge.metadata()['ssh_target']['network_origin'], 'windows')
        data = bytes(range(256)) * 16384
        file = servers[0]['root'] / 'binary-payload'
        with bridge.ssh.open_sftp() as sftp:
            with sftp.open(str(file), 'wb') as output:
                output.write(data)
            with sftp.open(str(file), 'rb') as source:
                self.assertEqual(source.read(), data)
        self.assertEqual(file.read_bytes(), data)
        relay = next(item for item in bridge._connection_resources if isinstance(item, WindowsNetworkSocket))
        bridge.close()
        self.assertTrue(relay.closed)
        self.assertIsNotNone(relay.process.poll())

        bridge = self.bridge(servers)
        with patch('terminal_backends.ssh.WindowsNetworkSocket.connect', side_effect=OSError('Windows unavailable')), \
                patch('terminal_backends.ssh.socket.create_connection') as direct:
            success, result = self.connect(bridge, route)
        self.assertFalse(success)
        self.assertEqual(result['error_code'], 'ssh_route_failed')
        direct.assert_not_called()


if __name__ == '__main__':
    unittest.main()
