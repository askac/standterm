"""Check SSH network-origin routing without Windows or network connections."""
import base64
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
arguments = sys.argv[1:]
sys.argv[1:] = []
import agent_backend_smoke as fixture
sys.argv[1:] = arguments

import paramiko
from terminal_backends import ssh

standterm = fixture.standterm


class SSHNetworkOriginTests(unittest.TestCase):
    def setUp(self):
        fixture.reset_state()
        self.plugin = standterm.TERMINAL_BACKEND_REGISTRY.get('ssh')
        self.direct = {'connection_type': 'ssh', 'terminal_id': 'main',
                       'host': 'localhost', 'port': 22, 'username': 'fixture', 'password': ''}
        self.node = {'node_id': 'first', 'host': 'localhost', 'port': 22,
                     'username': 'fixture', 'password': '', 'browser_key': None}

    def validate(self, data):
        return standterm.validate_start_ssh_payload(data, '127.0.0.1')

    def bridge(self):
        bridge = ssh.SSHBridge('fixture-session', get_paramiko=lambda: paramiko,
                               ssh_term='xterm', local_public_key_types=[])
        bridge.emit_output = Mock()
        self.addCleanup(bridge.close)
        return bridge

    def test_default_and_explicit_core_preserve_existing_payload(self):
        route = {**self.direct, 'attempt_id': 'fixture-attempt', 'route': [self.node]}
        with patch.object(ssh, 'windows_network_executable', return_value=None):
            for data in (self.direct, route):
                with self.subTest(route='route' in data):
                    original, error = self.validate(data)
                    self.assertIsNone(error)
                    explicit, error = self.validate({**data, 'network_origin': 'core'})
                    self.assertIsNone(error)
                    self.assertEqual(explicit, original)
                    self.assertNotIn('network_origin', original)

    def test_windows_capability_controls_schema_and_validation(self):
        for executable in (None, '/fixture/powershell.exe'):
            with self.subTest(available=bool(executable)), patch.object(
                    ssh, 'windows_network_executable', return_value=executable):
                fields = {field.name: field for field in self.plugin.get_start_form_schema()}
                self.assertEqual('network_origin' in fields, bool(executable))
                if executable:
                    self.assertEqual(fields['network_origin'].default_value, 'core')
                    self.assertEqual([item['value'] for item in fields['network_origin'].options], ['core', 'windows'])
                for data in (self.direct, {**self.direct, 'attempt_id': 'fixture-attempt', 'route': [self.node]}):
                    payload, error = self.validate({**data, 'network_origin': 'windows'})
                    if executable:
                        self.assertIsNone(error)
                        self.assertEqual(payload['network_origin'], 'windows')
                        for node in payload.get('route', []):
                            self.assertNotIn('network_origin', node)
                    else:
                        self.assertIsNone(payload)
                        self.assertEqual(error['error_code'], 'ssh_windows_network_unavailable')

    def test_unknown_origins_and_per_node_origins_are_rejected(self):
        with patch.object(ssh, 'windows_network_executable', return_value='/fixture/powershell.exe'):
            for origin in ('Windows', 'other', '', None, False, {}, []):
                with self.subTest(origin=origin):
                    payload, error = self.validate({**self.direct, 'network_origin': origin})
                    self.assertIsNone(payload)
                    self.assertTrue(error)
            for origin in ('core', 'windows'):
                payload, error = self.validate({**self.direct, 'network_origin': 'windows',
                    'attempt_id': 'fixture-attempt', 'route': [{**self.node, 'network_origin': origin}]})
                self.assertIsNone(payload)
                self.assertTrue(error)

    def test_connect_bridge_forwards_origin_without_changing_core_calls(self):
        for origin in ('core', 'windows'):
            bridge = Mock()
            payload = {**self.direct, 'browser_key': None}
            if origin == 'windows':
                payload['network_origin'] = origin
            self.plugin.connect_bridge(bridge, payload, 90, 30)
            options = {'browser_key': None, 'cols': 90, 'rows': 30}
            if origin == 'windows':
                options['network_origin'] = origin
            bridge.connect.assert_called_once_with('localhost', 22, 'fixture', '', **options)
            bridge.reset_mock()
            payload.update(route=[self.node], attempt_id='fixture-attempt', interactive_login=True)
            self.plugin.connect_bridge(bridge, payload, 90, 30)
            options.update(route=[self.node], attempt_id='fixture-attempt', interactive_login=True)
            bridge.connect.assert_called_once_with('localhost', 22, 'fixture', '', **options)

    def test_windows_direct_interactive_and_browser_key_never_use_local_shortcuts(self):
        browser_key = {'public_key': base64.b64encode(b'fixture-key').decode()}
        for interactive in (False, True):
            for key in (None, browser_key):
                with self.subTest(interactive=interactive, browser_key=bool(key)):
                    bridge = self.bridge()
                    with patch.object(bridge, '_connect_route', return_value=(True, None)) as route, \
                            patch.object(bridge, '_connect_with_local_keys') as local_keys, \
                            patch.object(bridge, '_connect_with_browser_key') as local_browser_key:
                        result = bridge.connect('localhost', 22, 'fixture', browser_key=key,
                            network_origin='windows', interactive_login=interactive)
                    self.assertEqual(result, (True, None))
                    route.assert_called_once()
                    self.assertEqual(route.call_args.args[0][0]['browser_key'], key)
                    self.assertEqual(route.call_args.kwargs, {'interactive_login': interactive})
                    local_keys.assert_not_called()
                    local_browser_key.assert_not_called()

    def test_bridge_rejects_unknown_origin_before_any_connection(self):
        bridge = self.bridge()
        with patch.object(bridge, '_connect_route') as route:
            success, error = bridge.connect('localhost', 22, 'fixture', network_origin='other')
        self.assertFalse(success)
        self.assertEqual(error['error_code'], 'ssh_network_origin_invalid')
        route.assert_not_called()

    def test_windows_route_owns_first_hop_before_connect_and_preserves_remote_jumps(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive), ExitStack() as stack:
                clients = []
                def client():
                    value = Mock()
                    clients.append(value)
                    return value
                stack.enter_context(patch.object(paramiko, 'SSHClient', side_effect=client))
                bridge = self.bridge()
                stack.enter_context(patch.object(bridge._host_key_store, 'snapshot',
                    return_value={'keys': [], 'host_key_name': 'localhost'}))
                reset = stack.enter_context(patch.object(bridge, '_reset_ssh_client', wraps=bridge._reset_ssh_client))
                stack.enter_context(patch.object(bridge, '_login_auth_strategy', return_value=object()))
                relay = Mock()
                relay.connect.side_effect = lambda *args, **kwargs: self.assertIn(relay, bridge._connection_resources)
                factory = stack.enter_context(patch.object(ssh, 'WindowsNetworkSocket', return_value=relay))
                direct = stack.enter_context(patch.object(ssh.socket, 'create_connection', side_effect=AssertionError('Core socket used')))
                nodes = [self.node, {**self.node, 'node_id': 'target', 'host': 'target.test'}]
                success, error = bridge.connect('target.test', 22, 'fixture', route=nodes,
                    network_origin='windows', interactive_login=interactive)
                self.assertTrue(success, error)
                factory.assert_called_once_with()
                relay.connect.assert_called_once_with(('localhost', 22), timeout=ssh.SSH_CONNECT_TIMEOUT_SECONDS)
                direct.assert_not_called()
                for call in reset.call_args_list:
                    self.assertIs(call.kwargs['local_direct'], False)
                    self.assertFalse(call.kwargs.get('trust_unknown_host', False))
                first, target = clients[-2:]
                first.get_transport().open_channel.assert_called_once_with('direct-tcpip',
                    ('target.test', 22), ('127.0.0.1', 0), timeout=ssh.SSH_FORWARD_TIMEOUT_SECONDS)
                self.assertIs(first.connect.call_args.kwargs['sock'], relay)
                self.assertIs(target.connect.call_args.kwargs['sock'], first.get_transport().open_channel.return_value)
                self.assertEqual(bridge.sftp_endpoint()['network_origin'], 'windows')
                bridge.close()
                self.assertTrue(relay.close.called)

    def test_sftp_same_route_keeps_network_origins_distinct(self):
        endpoints = []
        for origin in ('core', 'windows'):
            with ExitStack() as stack:
                stack.enter_context(patch.object(paramiko, 'SSHClient', side_effect=Mock))
                bridge = self.bridge()
                stack.enter_context(patch.object(bridge._host_key_store, 'snapshot',
                    return_value={'keys': [], 'host_key_name': 'localhost'}))
                stack.enter_context(patch.object(ssh, 'WindowsNetworkSocket', return_value=Mock()))
                stack.enter_context(patch.object(ssh.socket, 'create_connection', return_value=Mock()))
                bridge.network_origin = origin
                success, error = bridge._connect_route([self.node], 80, 24)
                self.assertTrue(success, error)
                endpoints.append(bridge.sftp_endpoint())
        self.assertNotEqual(endpoints[0], endpoints[1])
        self.assertNotIn('network_origin', endpoints[0])
        self.assertEqual(endpoints[1].pop('network_origin'), 'windows')
        self.assertEqual(endpoints[0], endpoints[1])

    def test_windows_connect_failure_never_falls_back_to_core(self):
        bridge = self.bridge()
        relay = Mock()
        relay.connect.side_effect = OSError('Fixture Windows connection failure')
        with patch.object(bridge._host_key_store, 'snapshot', return_value={'keys': [], 'host_key_name': 'localhost'}), \
                patch.object(ssh, 'WindowsNetworkSocket', return_value=relay), \
                patch.object(ssh.socket, 'create_connection') as direct:
            success, error = bridge.connect('localhost', 22, 'fixture', network_origin='windows')
        self.assertFalse(success)
        self.assertEqual(error['error_code'], 'ssh_route_failed')
        self.assertIsNone(bridge.sftp_endpoint())
        direct.assert_not_called()
        bridge.close()
        relay.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
