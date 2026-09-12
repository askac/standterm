import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paramiko

from terminal_backends.base import BackendAction, BackendActionStore, TerminalBridge
from terminal_backends.ssh import SSHBridge
from terminal_backends.ssh_host_keys import SSHHostKeyStore, fingerprint, host_key_name


class SSHHostKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_a = paramiko.RSAKey.generate(2048)
        cls.key_b = paramiko.RSAKey.generate(2048)
        cls.key_c = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='standterm-host-keys-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / '.ssh' / 'known_hosts'
        self.store = SSHHostKeyStore(paramiko, self.path)

    def line(self, target, key=None, comment=''):
        key = key or self.key_a
        return f'{target} {key.get_name()} {key.get_base64()}{comment}\n'

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(text.encode('utf-8'))

    def test_replace_and_forget_preserve_aliases_hashes_comments_and_ports(self):
        target = '192.168.167.254'
        hashed = paramiko.HostKeys.hash_host(target)
        other_port = self.line(f'[{target}]:2222', self.key_b)
        other_host = self.line('other.test', self.key_b, ' # other')
        original = ('# header\r\n\n' + self.line(f'{target},alias.test', comment=' # alias')
                    + self.line(hashed, self.key_c) + other_port + other_host)
        self.write(original)
        snapshot = self.store.snapshot(target, 22)
        self.assertEqual(len(snapshot['keys']), 2)
        self.store.update(snapshot, self.key_b)
        expected = ('# header\r\n\n' + self.line('alias.test', comment=' # alias')
                    + other_port + other_host + self.line(target, self.key_b))
        self.assertEqual(self.path.read_bytes(), expected.encode())
        self.store.update(self.store.snapshot(target, 22))
        self.assertEqual(self.path.read_bytes(), expected.removesuffix(self.line(target, self.key_b)).encode())
        self.assertEqual(self.store.snapshot(target, 22)['keys'], [])
        self.assertEqual(self.store.snapshot(target, 2222)['keys'], [self.key_b])

    def test_store_conflicts_and_external_edits_are_preserved(self):
        a = self.store.snapshot('a.test', 22)
        b = self.store.snapshot('b.test', 22)
        self.store.update(a, self.key_a)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.store.update(b, self.key_b)
        snapshot = self.store.snapshot('a.test', 22)
        external = self.path.read_bytes() + self.line('external.test').encode()
        original_fsync = os.fsync

        def edit_during_write(fd):
            original_fsync(fd)
            self.path.write_bytes(external)

        with patch('terminal_backends.ssh_host_keys.os.fsync', side_effect=edit_during_write):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.store.update(snapshot, self.key_b)
        self.assertEqual(self.path.read_bytes(), external)

    def test_policy_records_and_host_injection_fail_closed(self):
        for target in ('host.test', paramiko.HostKeys.hash_host('host.test'), '*.test'):
            for marker in ('@revoked', '@cert-authority'):
                self.write(f'{marker} ' + self.line(target))
                with self.assertRaisesRegex(ValueError, 'marked'):
                    self.store.snapshot('host.test', 22)
        self.write(self.line('*.test'))
        with self.assertRaisesRegex(ValueError, 'patterns'):
            self.store.snapshot('host.test', 22)
        for marker in ('@revoked ', '@cert-authority ', ''):
            self.write(marker + self.line('[*.test]:2222'))
            with self.assertRaises(ValueError):
                self.store.snapshot('host.test', 2222)
            self.assertEqual(self.store.snapshot('host.test', 22)['keys'], [])
        self.write('@revoked ' + self.line('other.test'))
        self.assertEqual(self.store.snapshot('host.test', 22)['keys'], [])
        for target in ('host,alias', 'host name', 'host\nname', '*.test', '|1|bad', '@host'):
            with self.assertRaises(ValueError):
                host_key_name(target, 22)
        self.assertEqual(host_key_name('::1', 2222), '[::1]:2222')

    def test_action_is_sid_bound_expiring_and_consumed_once(self):
        store = BackendActionStore(time_func=lambda: 10)
        action = BackendAction('confirm_ssh_host_key', 'main', {}, 20)
        store.set('sid-a', 'token', action)
        self.assertIsNone(store.get('sid-b', 'token', secrets.compare_digest, consume=True)[0])
        self.assertIsNone(store.get('sid-a', 'wrong', secrets.compare_digest, consume=True)[0])
        results = []
        barrier = threading.Barrier(3)

        def consume():
            barrier.wait()
            results.append(store.get('sid-a', 'token', secrets.compare_digest, consume=True)[0])

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(action), 1)
        store.set('sid-a', 'expired', BackendAction('confirm_ssh_host_key', 'main', {}, 9))
        self.assertEqual(store.get('sid-a', 'expired', secrets.compare_digest, consume=True)[1], 'backend_action_expired')

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires Windows privileges.')
    def test_symlinked_keys_can_be_read_but_not_rewritten(self):
        self.path.parent.mkdir(parents=True)
        actual = self.path.parent / 'shared_hosts'
        actual.write_text(self.line('host.test'))
        self.path.symlink_to(actual)
        snapshot = self.store.snapshot('host.test', 22)
        self.assertEqual(snapshot['keys'], [self.key_a])
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            self.store.update(snapshot, self.key_b)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(actual.read_text(), self.line('host.test'))

    def attempt(self, key):
        client_socket, server_socket = socket.socketpair()
        transport = paramiko.Transport(server_socket)
        transport.add_server_key(key)
        attempts = []

        class Server(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                attempts.append(username)
                return paramiko.AUTH_FAILED

        def serve():
            try:
                transport.start_server(server=Server())
            except (EOFError, paramiko.SSHException):
                pass

        thread = threading.Thread(target=serve)
        thread.start()
        original_connect = paramiko.SSHClient.connect

        def connect(client, *args, **kwargs):
            return original_connect(client, *args, sock=client_socket, **kwargs)

        with patch.object(TerminalBridge, '_default_runtime', object()):
            bridge = SSHBridge('session', get_paramiko=lambda: paramiko, ssh_term='xterm',
                               local_public_key_types=(), known_hosts_path=self.path)
        try:
            with patch.object(paramiko.SSHClient, 'connect', connect):
                success, result = bridge.connect('192.168.167.254', 22, 'operator', 'test-only')
            self.assertFalse(success)
            action = bridge.prepare_backend_action(
                result.get('action_type'), {'terminal_id': 'main'}, time.time() + 30,
            )
            return result, action, attempts
        finally:
            bridge.ssh.close()
            transport.close()
            client_socket.close()
            server_socket.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def confirm(self, action):
        return SSHBridge.execute_backend_action(action, get_paramiko=lambda: paramiko, known_hosts_path=self.path)

    def test_real_handshake_requires_confirmation_before_authentication(self):
        result, action, attempts = self.attempt(self.key_a)
        self.assertEqual(result['error_code'], 'ssh_host_key_unknown')
        self.assertIn(fingerprint(self.key_a), result['action_message'])
        self.assertEqual(attempts, [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.confirm(action)['status'], 'success')
        result, action, attempts = self.attempt(self.key_a)
        self.assertIsNone(action)
        self.assertEqual(attempts, ['operator'])
        result, action, attempts = self.attempt(self.key_b)
        self.assertEqual(result['error_code'], 'ssh_host_key_changed')
        self.assertIn(fingerprint(self.key_a), result['action_message'])
        self.assertIn(fingerprint(self.key_b), result['action_message'])
        self.assertEqual(attempts, [])
        self.assertEqual(self.confirm(action)['status'], 'success')
        result, action, attempts = self.attempt(self.key_c)
        self.assertEqual(result['error_code'], 'ssh_host_key_changed')
        self.assertEqual(attempts, [])
        self.store.update(self.store.snapshot('192.168.167.254', 22))
        result, action, attempts = self.attempt(self.key_b)
        self.assertEqual(result['error_code'], 'ssh_host_key_unknown')
        self.assertEqual(attempts, [])


if __name__ == '__main__':
    unittest.main()
