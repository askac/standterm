"""Exercise StandTerm routes against distinct isolated OpenSSH servers."""
import base64
import contextlib
import getpass
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paramiko
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from terminal_backends.base import TerminalBridge
from terminal_backends.ssh import SSHBridge
from terminal_backends.ssh_host_keys import SSHHostKeyStore


@contextlib.contextmanager
def server(forwarding='yes'):
    with tempfile.TemporaryDirectory(prefix='standterm-jump-sshd-') as directory:
        root = Path(directory)
        host_key = paramiko.RSAKey.generate(2048)
        host_key.write_private_key_file(str(root / 'host_key'))
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        (root / 'authorized_keys').write_bytes(private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH) + b'\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        (root / 'sshd_config').write_text('\n'.join([
            f'Port {port}', 'ListenAddress 127.0.0.1', f'HostKey {root / "host_key"}',
            f'AuthorizedKeysFile {root / "authorized_keys"}', f'PidFile {root / "pid"}',
            'StrictModes no', 'UsePAM no', 'PasswordAuthentication no',
            'KbdInteractiveAuthentication no', 'PermitRootLogin prohibit-password',
            f'AllowTcpForwarding {forwarding}', 'GatewayPorts no',
            'Subsystem sftp internal-sftp', 'LogLevel ERROR',
        ]) + '\n')
        with (root / 'log').open('w+') as log:
            process = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(root / 'sshd_config')], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.02)
                yield {'port': port, 'host_key': host_key, 'private': private, 'public': public, 'root': root}
            finally:
                process.terminate()
                process.wait(timeout=5)


class SSHJumpTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='standterm-jump-test-')))
        self.known_hosts = self.directory / 'known_hosts'
        self.signatures = []
        self.progress = []

    def route(self, servers):
        return [{
            'node_id': f'node-{i}', 'host': '127.0.0.1', 'port': item['port'],
            'username': getpass.getuser(), 'password': '', 'host_key_alias': f'site-{i}',
            'browser_key': {'profile_id': f'owner-{i}', 'key_id': f'key-{i}',
                            'public_key': base64.b64encode(item['public']).decode(),
                            'fingerprint': hashlib.sha256(item['public']).hexdigest(),
                            'node_id': f'node-{i}', 'attempt_id': 'test-attempt'}
        } for i, item in enumerate(servers)]

    def bridge(self, servers):
        def sign(bridge, sid, key, data, algorithm):
            self.assertEqual(sid, 'test-sid')
            self.assertEqual(key['attempt_id'], 'test-attempt')
            index = int(key['node_id'].split('-')[1])
            self.signatures.append(index)
            return servers[index]['private'].sign(data)
        with patch.object(TerminalBridge, '_default_runtime', object()):
            bridge = SSHBridge('session', get_paramiko=lambda: paramiko, ssh_term='xterm',
                               local_public_key_types=[], known_hosts_path=self.known_hosts,
                               request_browser_signature=sign)
        bridge.emit_output = self.progress.append
        bridge.set_browser_signer_sid('test-sid')
        self.addCleanup(bridge.close)
        return bridge

    def trust(self, route, servers):
        store = SSHHostKeyStore(paramiko, self.known_hosts)
        for node, item in zip(route, servers):
            store.update(store.snapshot(node['host_key_alias'] or node['host'], node['port']), item['host_key'])

    def connect(self, bridge, route):
        return bridge.connect(route[-1]['host'], route[-1]['port'], route[-1]['username'],
                              route=route, attempt_id='test-attempt')

    def test_each_hop_requires_trust_before_signing_and_aliases_use_real_network_addresses(self):
        servers = [self.stack.enter_context(server()) for _ in range(3)]
        route = self.route(servers)
        store = SSHHostKeyStore(paramiko, self.known_hosts)
        for unknown in range(3):
            self.signatures.clear()
            bridge = self.bridge(servers)
            success, result = self.connect(bridge, route)
            self.assertFalse(success)
            self.assertEqual(result['error_code'], 'ssh_host_key_unknown')
            self.assertEqual(result['route_context']['hop'], unknown + 1)
            self.assertEqual(self.signatures, list(range(unknown)))
            store.update(bridge._host_key_snapshot, bridge._pending_host_key)
            bridge.close()
        bridge = self.bridge(servers)
        success, result = self.connect(bridge, route)
        self.assertTrue(success, result)
        self.assertNotEqual(bridge.sftp_endpoint()['route'], 'direct')
        with bridge.ssh.open_sftp() as sftp:
            with sftp.open(str(servers[-1]['root'] / 'payload'), 'w') as handle:
                handle.write('SSH route SFTP verification')
        self.assertEqual((servers[-1]['root'] / 'payload').read_text(), 'SSH route SFTP verification')
        self.assertEqual(store.snapshot('127.0.0.1', servers[-1]['port'])['keys'], [])

    def test_three_jumps_terminal_and_reverse_forward_share_final_transport(self):
        servers = [self.stack.enter_context(server()) for _ in range(4)]
        route = self.route(servers)
        self.trust(route, servers)
        bridge = self.bridge(servers)
        success, result = self.connect(bridge, route)
        self.assertTrue(success, result)
        stdin, stdout, stderr = bridge.ssh.exec_command('printf STANDTERM_JUMP_OK', timeout=5)
        self.assertEqual(stdout.read(), b'STANDTERM_JUMP_OK')
        self.assertEqual(stdout.channel.recv_exit_status(), 0)
        stdin.close(); stdout.close(); stderr.close()
        transport = bridge.ssh.get_transport()
        port = transport.request_port_forward('127.0.0.1', 0)
        with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
            channel = transport.accept(timeout=5)
            self.assertIsNotNone(channel)
            sock.sendall(b'reverse-forward')
            self.assertEqual(channel.recv(15), b'reverse-forward')
            channel.close()
        transport.cancel_port_forward('127.0.0.1', port)
        clients = [resource for resource in bridge._connection_resources if isinstance(resource, paramiko.SSHClient)]
        transports = [client.get_transport() for client in clients]
        self.assertEqual(len(transports), 4)
        bridge.close()
        for item in transports:
            item.join(3)
            self.assertFalse(item.is_alive())

    def test_forwarding_failure_never_falls_back_to_direct(self):
        servers = [self.stack.enter_context(server('no')), self.stack.enter_context(server())]
        route = self.route(servers)
        self.trust(route, servers)
        bridge = self.bridge(servers)
        success, result = self.connect(bridge, route)
        self.assertFalse(success)
        self.assertEqual(self.signatures, [0])
        self.assertEqual(result['route_context']['phase'], 'forward')
        self.assertEqual(result['route_context']['hop'], 2)

    def test_remote_loopback_does_not_inherit_core_localhost_trust(self):
        servers = [self.stack.enter_context(server()) for _ in range(2)]
        route = self.route(servers)
        route[-1]['host_key_alias'] = ''
        self.trust(route[:1], servers[:1])
        bridge = self.bridge(servers)
        with patch.object(bridge, '_connect_with_local_keys') as local_keys:
            success, result = self.connect(bridge, route)
        self.assertFalse(success)
        self.assertEqual(result['error_code'], 'ssh_host_key_unknown')
        self.assertEqual(result['route_context']['hop'], 2)
        self.assertEqual(self.signatures, [0])
        local_keys.assert_not_called()

    def test_browser_key_jump_and_password_target(self):
        jump = self.stack.enter_context(server())
        key = paramiko.RSAKey.generate(2048)
        attempts = []
        stop = threading.Event()
        class PasswordServer(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return 'password'

            def check_auth_password(self, username, password):
                attempts.append((username, password))
                return paramiko.AUTH_SUCCESSFUL if password == 'test-route-password' else paramiko.AUTH_FAILED

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_pty_request(self, *args):
                return True

            def check_channel_shell_request(self, channel):
                return True
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(10)
            port = listener.getsockname()[1]
            def serve():
                sock, _ = listener.accept()
                with paramiko.Transport(sock) as transport:
                    transport.add_server_key(key)
                    transport.start_server(server=PasswordServer())
                    channel = transport.accept(10)
                    stop.wait(10)
                    if channel:
                        channel.close()
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                route = self.route([jump]) + [{'node_id': 'password-target', 'host': '127.0.0.1', 'port': port,
                    'username': 'operator', 'password': 'test-route-password', 'host_key_alias': 'password-site', 'browser_key': None}]
                self.trust(route[:1], [jump])
                store = SSHHostKeyStore(paramiko, self.known_hosts)
                store.update(store.snapshot('password-site', port), key)
                bridge = self.bridge([jump])
                success, result = self.connect(bridge, route)
                self.assertTrue(success, result)
                self.assertEqual(attempts, [('operator', 'test-route-password')])
                self.assertEqual(self.signatures, [0])
                self.assertEqual(bridge.auth_method, 'password')
                bridge.close()
            finally:
                stop.set()
                thread.join(10)
                self.assertFalse(thread.is_alive())

    def test_cancel_during_second_hop_signing_closes_owned_connections(self):
        servers = [self.stack.enter_context(server()) for _ in range(3)]
        route = self.route(servers)
        self.trust(route, servers)
        bridge = self.bridge(servers)
        waiting, release = threading.Event(), threading.Event()
        original = bridge._request_browser_signature
        def sign(*args):
            if args[2]['node_id'] == 'node-1':
                waiting.set()
                release.wait(5)
            return original(*args)
        bridge._request_browser_signature = sign
        thread = threading.Thread(target=self.connect, args=(bridge, route))
        thread.start()
        self.assertTrue(waiting.wait(10))
        bridge.close()
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn(2, self.signatures)
        self.assertEqual(bridge._connection_resources, [])


if __name__ == '__main__':
    unittest.main()
