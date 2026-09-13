"""Verify route payload validation and concurrent SSH attempt ownership."""
import base64
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
arguments = sys.argv[1:]
sys.argv[1:] = []
import agent_backend_smoke as fixture
sys.argv[1:] = arguments
standterm = fixture.standterm


class SSHStartTests(unittest.TestCase):
    def setUp(self):
        fixture.reset_state()
        self.client = fixture.make_client()
        self.session = fixture.current_session_token()
        self.sid = fixture.current_sid_for_session(self.session)
        self.addCleanup(self.client.disconnect)

    def pending(self, attempt='attempt-b', terminal_id='main'):
        token = standterm.begin_terminal_start(self.session, terminal_id, self.sid, attempt)
        bridge = fixture.DummyBridge(self.session, terminal_id)
        bridge.attach(self.sid)
        standterm.pending_terminal_bridges[(self.session, terminal_id)] = bridge
        return token, bridge

    def test_route_validates_all_nodes_and_rejects_four_jumps_before_connecting(self):
        node = {'node_id': 'node-0', 'host': '127.0.0.1', 'port': 22, 'username': 'u', 'password': ''}
        data = {'connection_type': 'ssh', 'terminal_id': 'main', 'attempt_id': 'attempt-a',
                'route': [{**node, 'node_id': f'node-{i}'} for i in range(4)]}
        payload, error = standterm.validate_start_ssh_payload(data, '127.0.0.1')
        self.assertIsNone(error)
        self.assertEqual(len(payload['route']), 4)
        for route in [data['route'] + [{**node, 'node_id': 'extra'}], [node, node],
                      [node, {**node, 'node_id': 'node-1', 'port': 65536}],
                      [node, {**node, 'node_id': 'node-1', 'host_key_alias': '*.test'}]]:
            payload, error = standterm.validate_start_ssh_payload({**data, 'route': route}, '127.0.0.1')
            self.assertIsNone(payload)
            self.assertTrue(error)

    def test_cancel_requires_matching_attempt_and_cannot_discard_a_completed_trust_action(self):
        token, bridge = self.pending()
        self.client.emit('cancel_ssh_start', {'terminal_id': 'main', 'attempt_id': 'attempt-a'})
        self.assertTrue(standterm.is_current_terminal_start(self.session, 'main', token))
        self.assertFalse(bridge.closing)
        standterm.finish_terminal_start(self.session, 'main', token)
        action = standterm.BackendAction('confirm_ssh_host_key', 'main', {'attempt_id': 'attempt-b'}, time.time() + 30)
        standterm.pending_backend_actions.set(self.sid, 'action-b', action)
        self.client.emit('cancel_ssh_start', {'terminal_id': 'main', 'attempt_id': 'attempt-a'})
        self.assertIsNotNone(standterm.pending_backend_actions.get(self.sid))

    def test_close_all_cancels_unregistered_bridges_and_pending_signatures(self):
        token, bridge = self.pending()
        key = {'profile_id': 'owner', 'key_id': 'key', 'fingerprint': 'f' * 64,
               'attempt_id': 'attempt-b', 'node_id': 'node-b'}
        request, error = standterm.browser_ssh_sign_request_store.create(
            self.session, 'main', self.sid, 'browser', key, b'challenge', 'ssh-ed25519')
        self.assertIsNone(error)
        self.client.emit('close_all_terminals')
        self.assertFalse(standterm.is_current_terminal_start(self.session, 'main', token))
        self.assertTrue(bridge.closing)
        _, error, _ = standterm.browser_ssh_sign_request_store.wait(request)
        self.assertIsNotNone(error)
        self.assertEqual(standterm.pending_terminal_bridges, {})

    def test_signatures_require_attempt_and_node_in_addition_to_key_and_tab(self):
        store = standterm.BrowserSSHSignRequestStore()
        key = {'profile_id': 'owner', 'key_id': 'key', 'fingerprint': 'f' * 64,
               'attempt_id': 'attempt-b', 'node_id': 'node-b'}
        request, error = store.create(self.session, 'main', self.sid, 'browser', key, b'challenge', 'ssh-ed25519')
        self.assertIsNone(error)
        response = {field: request[field] for field in ('request_id', 'terminal_id', 'profile_id', 'key_id',
                                                       'challenge_sha256', 'attempt_id', 'node_id')}
        response.update(status='ok', signature=base64.b64encode(b'x' * 64).decode())
        for field in ('attempt_id', 'node_id'):
            self.assertEqual(store.resolve(self.session, self.sid, {**response, field: 'other'}), 'ssh_browser_key_sign_stale')
        self.assertIsNone(store.resolve(self.session, self.sid, response))

    def test_stale_failure_cannot_replace_new_attempt_host_key_action(self):
        plugin = standterm.TERMINAL_BACKEND_REGISTRY.get('local_shell')
        waiting, release = threading.Event(), threading.Event()
        bridge = fixture.DummyBridge(self.session, 'main')
        payload = {'terminal_id': 'main', 'connection_type': 'local_shell'}
        token = standterm.begin_terminal_start(self.session, 'main', self.sid, 'attempt-a')
        def connect(*args):
            waiting.set()
            release.wait(5)
            return False, {'message': 'stale failure'}
        with patch.object(plugin, 'create_bridge', return_value=bridge), patch.object(plugin, 'connect_bridge', side_effect=connect), \
                patch.object(plugin, 'build_connection_failure') as failure:
            thread = threading.Thread(target=standterm.start_terminal_backend, args=(self.sid, self.session, payload, token))
            thread.start()
            self.assertTrue(waiting.wait(5))
            current, new_bridge = self.pending()
            action = standterm.BackendAction('confirm_ssh_host_key', 'main', {'attempt_id': 'attempt-b'}, time.time() + 30)
            standterm.pending_backend_actions.set(self.sid, 'action-b', action)
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            failure.assert_not_called()
            self.assertTrue(standterm.is_current_terminal_start(self.session, 'main', current))
            self.assertFalse(new_bridge.closing)
            self.assertEqual(standterm.pending_backend_actions.get(self.sid)['action_id'], 'action-b')


if __name__ == '__main__':
    unittest.main()
