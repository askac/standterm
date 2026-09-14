"""Verify route payload validation and concurrent SSH attempt ownership."""
import base64
import queue
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
        self.addCleanup(lambda: self.client.disconnect() if self.client.is_connected() else None)

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

    def login_wait(self):
        token = standterm.begin_terminal_start(self.session, 'main', self.sid, 'login-attempt')
        bridge = standterm.SSHBridge(self.session, get_paramiko=standterm.get_paramiko,
                                     ssh_term='xterm', local_public_key_types=[])
        bridge.attach(self.sid)
        bridge.set_browser_signer_sid(self.sid)
        bridge.attempt_id = 'login-attempt'
        bridge._connection_node = {'node_id':'login-node', 'hop':1, 'total':1, 'phase':'password'}
        standterm.pending_terminal_bridges[(self.session, 'main')] = bridge
        prompts = queue.Queue()
        bridge.emit_output = prompts.put
        results = []
        def wait():
            try:
                results.append(bridge._request_login_input(
                    {'node_id':'login-node', 'host':'site.test', 'port':22, 'username':'u'}, 'password'))
            except RuntimeError:
                results.append('cancelled')
        thread = threading.Thread(target=wait)
        thread.start()
        prompt = prompts.get(timeout=5)
        def cleanup():
            bridge.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        return bridge, prompt, results, thread

    def test_login_response_requires_current_socket_session_tab_attempt_and_request(self):
        bridge, prompt, results, thread = self.login_wait()
        response = {**prompt, 'terminal_id':'main', 'password':'correct-password'}
        other = fixture.make_client()
        self.addCleanup(other.disconnect)
        other.emit('ssh_login_response', response)
        for field in ('terminal_id', 'node_id', 'attempt_id', 'request_id', 'kind'):
            self.client.emit('ssh_login_response', {**response, field:'other'})
        self.client.emit('ssh_login_response', {**response, 'password':'x' * (standterm.MAX_PASSWORD_BYTES + 1)})
        self.assertEqual(results, [])
        self.client.emit('ssh_login_response', response)
        thread.join(5)
        self.assertEqual(results, ['correct-password'])
        self.assertIsNone(bridge._login_request)

    def test_cancel_close_and_replacement_wake_login_waits(self):
        for action in ('cancel', 'close', 'close_all', 'replace', 'disconnect'):
            with self.subTest(action=action):
                bridge, prompt, results, thread = self.login_wait()
                if action == 'cancel':
                    self.client.emit('cancel_ssh_start', {'terminal_id':'main', 'attempt_id':'login-attempt'})
                elif action == 'close':
                    self.client.emit('close_terminal', {'terminal_id':'main'})
                elif action == 'close_all':
                    self.client.emit('close_all_terminals')
                elif action == 'replace':
                    standterm.begin_terminal_start(self.session, 'main', self.sid, 'replacement')
                else:
                    self.client.disconnect()
                if self.client.is_connected():
                    self.client.emit('ssh_login_response', {**prompt, 'terminal_id':'main', 'password':'late-password'})
                thread.join(5)
                self.assertEqual(results, ['cancelled'])
                self.assertTrue(bridge._connection_cancelled.is_set())

    def test_cancellation_takes_effect_before_resource_close(self):
        bridge, prompt, results, waiter = self.login_wait()
        checked, resume_waiter = threading.Event(), threading.Event()
        closing, resume_close = threading.Event(), threading.Event()
        original_check, original_close = bridge._check_connection_active, standterm.close_bridge
        def check():
            checked.set()
            resume_waiter.wait(5)
            original_check()
        def close(value):
            closing.set()
            resume_close.wait(5)
            original_close(value)
        with patch.object(bridge, '_check_connection_active', side_effect=check), patch.object(standterm, 'close_bridge', side_effect=close):
            self.client.emit('ssh_login_response', {**prompt, 'terminal_id':'main', 'password':'answered-password'})
            self.assertTrue(checked.wait(5))
            cancelling = threading.Thread(target=standterm.cancel_terminal_starts,
                                          args=(self.session, 'main'), kwargs={'sid':self.sid})
            cancelling.start()
            try:
                self.assertTrue(closing.wait(5))
                self.assertNotIn((self.session, 'main'), standterm.pending_terminal_starts)
                resume_waiter.set()
                waiter.join(5)
                self.assertEqual(results, ['cancelled'])
            finally:
                resume_waiter.set()
                resume_close.set()
                cancelling.join(5)
                self.assertFalse(cancelling.is_alive())


if __name__ == '__main__':
    unittest.main()
