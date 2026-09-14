"""Verify independent SSH credentials and editor-scoped host identity actions."""
import base64
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_backend_smoke as fixture
from terminal_backends.ssh_host_keys import SSHHostKeyStore

app = fixture.standterm


class NodeCredentialTests(unittest.TestCase):
    def setUp(self):
        fixture.reset_state()
        self.client = fixture.make_client()
        self.session = fixture.current_session_token()
        self.sid = fixture.current_sid_for_session(self.session)
        self.addCleanup(lambda: self.client.disconnect() if self.client.is_connected() else None)

    def test_credential_owner_is_exclusive_and_signatures_keep_attempt_binding(self):
        data = {'host': 'site.test', 'port': 22, 'username': 'operator', 'password': '',
                'use_browser_key': True, 'credential_id': 'credential-a', 'key_id': 'credential-a',
                'browser_public_key': base64.b64encode(b'a' * 32).decode()}
        plugin = app.TERMINAL_BACKEND_REGISTRY.get('ssh')
        payload, error = plugin.validate_start_payload(data, 'main', '127.0.0.1')
        self.assertIsNone(error)
        self.assertNotIn('profile_id', payload['browser_key'])
        for changes in ({'profile_id': 'owner'}, {'credential_id': 'other'}, {'credential_id': []}):
            self.assertIsNotNone(plugin.validate_start_payload({**data, **changes}, 'main', '127.0.0.1')[1])
        key = {**payload['browser_key'], 'node_id': 'node-a', 'attempt_id': 'attempt-a'}
        request, error = app.browser_ssh_sign_request_store.create(self.session, 'main', self.sid, 'browser', key, b'challenge', 'ssh-ed25519')
        self.assertIsNone(error)
        response = {field: request[field] for field in ('request_id', 'terminal_id', 'credential_id', 'key_id', 'challenge_sha256', 'node_id', 'attempt_id')}
        response.update(status='ok', signature=base64.b64encode(b'x' * 64).decode())
        for change in ({'profile_id': 'owner'}, {'credential_id': 'other'}, {'node_id': 'other'}, {'attempt_id': 'other'}):
            self.assertEqual(app.browser_ssh_sign_request_store.resolve(self.session, self.sid, {**response, **change}), 'ssh_browser_key_sign_stale')
        self.assertIsNone(app.browser_ssh_sign_request_store.resolve(self.session, self.sid, response))

    def test_fingerprint_confirm_is_bound_to_editor_and_cannot_cross_rpc(self):
        plugin = app.TERMINAL_BACKEND_REGISTRY.get('ssh')
        paramiko = app.get_paramiko()
        with tempfile.TemporaryDirectory(prefix='standterm-node-identity-') as directory:
            path = Path(directory) / 'known_hosts'
            store = SSHHostKeyStore(paramiko, path)
            store.update(store.snapshot('location-a', 22), paramiko.RSAKey.generate(2048))
            store.update(store.snapshot('location-b', 22), paramiko.RSAKey.generate(2048))
            target = {'terminal_id': 'main', 'host': '192.168.167.254', 'port': 22, 'host_key_alias': 'location-a',
                      'editor_id': 'editor-a', 'node_id': 'node-a', 'request_id': 'request-a'}
            def call(operation, **changes):
                return self.client.emit('ssh_host_identity', {**target, 'operation': operation, **changes}, callback=True)
            with patch.dict(plugin._bridge_kwargs, {'known_hosts_path': path}):
                inspected = call('inspect')
                self.assertEqual(inspected['identity'], 'location-a')
                self.assertEqual(len(inspected['fingerprints']), 1)
                original = path.read_bytes()
                prompt = call('prepare_forget')
                self.assertEqual(prompt['status'], 'confirm')
                for operation in ('confirm', 'cancel'):
                    self.client.emit('ssh_host_key_action', {**target, 'operation': operation, 'action_id': prompt['action_id']})
                    self.assertEqual(path.read_bytes(), original)
                    self.assertIsNotNone(app.pending_backend_actions.get(self.sid))
                for field in ('editor_id', 'node_id', 'terminal_id', 'host', 'host_key_alias'):
                    result = call('confirm', action_id=prompt['action_id'], **{field: 'other'})
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(path.read_bytes(), original)
                    self.assertIsNotNone(app.pending_backend_actions.get(self.sid))
                app.socket_client_ips[self.sid] = '203.0.113.4'
                self.assertEqual(call('confirm', action_id=prompt['action_id'])['status'], 'failed')
                app.socket_client_ips[self.sid] = '127.0.0.1'
                self.assertEqual(call('confirm', action_id=prompt['action_id'])['status'], 'success')
                self.assertEqual(store.snapshot('location-a', 22)['keys'], [])
                self.assertEqual(len(store.snapshot('location-b', 22)['keys']), 1)
                self.assertEqual(call('confirm', action_id=prompt['action_id'])['status'], 'failed')
                # The old API's action is also unusable from the editor API.
                self.client.emit('ssh_host_key_action', {**target, 'host_key_alias': 'location-b', 'operation': 'forget'})
                legacy = fixture.last_payload(self.client, 'ssh_output')
                self.assertEqual(call('confirm', host_key_alias='location-b', action_id=legacy['action_id'])['status'], 'failed')
                self.assertIsNotNone(app.pending_backend_actions.get(self.sid))
                self.client.emit('ssh_host_key_action', {**target, 'operation': 'cancel', 'action_id': legacy['action_id']})
                prompt = call('prepare_forget', host_key_alias='location-b')
                store.update(store.snapshot('other-host', 22), paramiko.RSAKey.generate(2048))
                latest = path.read_bytes()
                self.assertEqual(call('confirm', host_key_alias='location-b', action_id=prompt['action_id'])['status'], 'failed')
                self.assertEqual(path.read_bytes(), latest)


if __name__ == '__main__':
    unittest.main()
