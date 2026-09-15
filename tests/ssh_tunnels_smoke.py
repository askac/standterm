"""Exercise user TCP tunnels and the Agent preset on real SSH transports."""
import contextlib
import shutil
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tunnel_smoke as agent_fixture
import ssh_tunnels
from ssh_forwarding import forwarding_for
from ssh_tunnels import parse_tunnel_spec

fixture = agent_fixture.fixture
ssh_server = agent_fixture.ssh_server
standterm = fixture.standterm


@contextlib.contextmanager
def tcp_server(handler):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    listener.settimeout(0.1)
    closed = threading.Event()
    errors, peers, workers = [], [], []

    def serve_peer(peer):
        try:
            peer.settimeout(10)
            handler(peer)
        except (OSError, AssertionError) as exc:
            if not closed.is_set():
                errors.append(exc)
        finally:
            peer.close()

    def accept():
        while not closed.is_set():
            try:
                peer, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            peers.append(peer)
            worker = threading.Thread(target=serve_peer, args=(peer,), daemon=True)
            workers.append(worker)
            worker.start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
        assert not errors, errors
    finally:
        closed.set()
        listener.close()
        for peer in peers:
            peer.close()
        thread.join(2)
        for worker in workers:
            worker.join(2)


def receive_all(peer):
    chunks = []
    while True:
        chunk = peer.recv(65536)
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)


class UserTunnelTests(unittest.TestCase):
    def setUp(self):
        fixture.reset_state()
        with standterm.user_ssh_tunnels_lock:
            previous = list(standterm.user_ssh_tunnels.values())
            standterm.user_ssh_tunnels.clear()
        for record in previous:
            record['tunnel'].stop()
        self.flask_client = fixture.make_flask_client()
        self.client = fixture.make_socket_client(self.flask_client)
        self.session = fixture.current_session_token()
        self.sid = fixture.current_sid_for_session(self.session)
        self.tunnels = []

    def tearDown(self):
        for tunnel in self.tunnels:
            tunnel.stop()
            tunnel.setup_done.wait(5)
        if self.client.is_connected():
            self.client.disconnect()

    def carrier(self, ssh):
        bridge = fixture.make_sftp_test_bridge(self.session, 'carrier')
        bridge.ssh = ssh
        bridge.attach(self.sid)
        standterm.set_bridge(self.session, 'carrier', bridge)
        return bridge

    def start(self, ssh, direction, port, **changes):
        if not standterm.get_bridge(self.session, 'carrier'):
            self.carrier(ssh)
        spec = {'direction': direction, 'listen_port': 0, 'target_host': '127.0.0.1', 'target_port': port, **changes}
        result = self.client.emit('ssh_tunnel', {'terminal_id': 'carrier', 'operation': 'start', 'spec': spec}, callback=True)
        self.assertEqual(result['status'], 'ok', result)
        tunnel = standterm.user_ssh_tunnels[result['tunnel']['tunnel_id']]['tunnel']
        self.tunnels.append(tunnel)
        fixture.wait_until(lambda: tunnel.status != 'starting', 'Tunnel did not settle', timeout=10)
        self.assertEqual(tunnel.status, 'listening', tunnel.snapshot())
        return tunnel

    def test_half_close_keeps_large_response_in_both_directions(self):
        payload = bytes(range(256)) * 16384

        def reply(peer):
            self.assertEqual(receive_all(peer), b'request')
            time.sleep(0.05)
            peer.sendall(payload)
            peer.shutdown(socket.SHUT_WR)

        with ssh_server(forwarding='yes') as ssh, tcp_server(reply) as port:
            for direction in ('local', 'remote'):
                tunnel = self.start(ssh, direction, port)
                with socket.create_connection(('127.0.0.1', tunnel.port), timeout=10) as client:
                    client.sendall(b'request')
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(receive_all(client), payload)
                fixture.wait_until(lambda: not tunnel.channels, 'Completed channels were retained')
                self.assertEqual(tunnel.bytes_sent, len(b'request'))
                self.assertEqual(tunnel.bytes_received, len(payload))

    def test_simultaneous_large_duplex_transfer(self):
        payload = b'x' * (8 * 1024 * 1024)

        def exchange(peer):
            peer.sendall(payload)
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(receive_all(peer), payload)

        with ssh_server(forwarding='yes') as ssh, tcp_server(exchange) as port:
            for direction in ('local', 'remote'):
                tunnel = self.start(ssh, direction, port)
                with socket.create_connection(('127.0.0.1', tunnel.port), timeout=10) as client:
                    def send():
                        client.sendall(payload)
                        client.shutdown(socket.SHUT_WR)
                    writer = threading.Thread(target=send, daemon=True)
                    writer.start()
                    self.assertEqual(receive_all(client), payload)
                    writer.join(10)
                    self.assertFalse(writer.is_alive())
                fixture.wait_until(lambda: not tunnel.channels, 'Duplex channels were retained')

    def test_agent_and_two_remote_tunnels_stop_independently(self):
        self.client.disconnect()
        helper = agent_fixture.AgentTunnelTests()
        helper.setUp()
        try:
            with ssh_server(forwarding='yes') as ssh, tcp_server(lambda peer: peer.sendall(b'alive')) as port:
                agent = helper.open_tunnel(ssh)
                self.session, self.sid, self.client = helper.session, helper.sid, helper.client
                first = self.start(ssh, 'remote', port)
                second = self.start(ssh, 'remote', port)
                self.assertEqual(helper.command(agent, 'main', 'hello')['status'], 'ok')
                first.stop()
                self.assertEqual(helper.command(agent, 'main', 'hello')['status'], 'ok')
                with socket.create_connection(('127.0.0.1', second.port), timeout=5) as client:
                    self.assertEqual(client.recv(16), b'alive')
                agent.close()
                self.assertTrue(agent._cleanup_done.wait(5))
                with socket.create_connection(('127.0.0.1', second.port), timeout=5) as client:
                    self.assertEqual(client.recv(16), b'alive')
                second.stop()
                self.assertTrue(ssh.get_transport().is_authenticated())
        finally:
            helper.tearDown()

    def test_user_remote_tunnel_reaches_core_api_without_granting_access(self):
        from werkzeug.serving import make_server
        self.client.disconnect()
        helper = agent_fixture.AgentTunnelTests()
        helper.setUp()
        server = make_server('127.0.0.1', 0, standterm.app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.session, self.sid, self.client = helper.session, helper.sid, helper.client
            token, _ = helper.mint()
            with ssh_server() as ssh:
                tunnel = self.start(ssh, 'remote', server.server_port)
                url = f'http://127.0.0.1:{tunnel.port}/agent/external/command'
                status, result = agent_fixture.request_json(url, {'op': 'hello', 'terminal_id': 'main'})
                self.assertNotEqual(result.get('status'), 'ok')
                status, result = agent_fixture.request_json(url, {'op': 'hello', 'terminal_id': 'main', 'token': token})
                self.assertEqual(status, 200)
                self.assertEqual(result['status'], 'ok')
                helper.client.emit(standterm.AGENT_EVENT_MODE_SET, {'terminal_id': 'main', 'mode': 'disabled'})
                _, result = agent_fixture.request_json(url, {'op': 'hello', 'terminal_id': 'main', 'token': token})
                self.assertNotEqual(result.get('status'), 'ok')
        finally:
            for tunnel in self.tunnels:
                tunnel.stop()
            server.shutdown()
            server.server_close()
            worker.join(5)
            helper.tearDown()

    def test_pending_remote_setup_cannot_revive_after_disconnect(self):
        with ssh_server() as ssh:
            self.carrier(ssh)
            entered, release = threading.Event(), threading.Event()
            original = ssh.get_transport().request_port_forward

            def delayed(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return original(*args, **kwargs)

            with patch.object(ssh.get_transport(), 'request_port_forward', delayed), \
                    patch.object(ssh_tunnels, 'USER_TUNNEL_SETUP_TIMEOUT', 0.1):
                result = self.client.emit('ssh_tunnel', {'terminal_id': 'carrier', 'operation': 'start', 'spec': {
                    'direction': 'remote', 'listen_port': 0, 'target_host': '127.0.0.1', 'target_port': 80,
                }}, callback=True)
                tunnel = standterm.user_ssh_tunnels[result['tunnel']['tunnel_id']]['tunnel']
                self.tunnels.append(tunnel)
                try:
                    self.assertTrue(entered.wait(2))
                    fixture.wait_until(lambda: tunnel.status == 'failed', 'Setup did not time out')
                    with self.assertRaisesRegex(RuntimeError, 'pending'):
                        forwarding_for(ssh.get_transport()).request_remote(0, object(), lambda *_: None, lambda: False)
                    self.client.disconnect()
                finally:
                    release.set()
                self.assertTrue(tunnel.setup_done.wait(5))
                self.assertNotIn(tunnel.port, forwarding_for(ssh.get_transport()).routes)
                self.assertTrue(ssh.get_transport().is_authenticated())

    def test_viewer_ownership_and_disconnect_stop_active_tunnels(self):
        with ssh_server(forwarding='yes') as ssh, tcp_server(lambda peer: receive_all(peer)) as port:
            tunnel = self.start(ssh, 'local', port)
            other = fixture.make_socket_client(self.flask_client)
            try:
                status = other.emit('ssh_tunnel', {'terminal_id': 'carrier', 'operation': 'status'}, callback=True)
                self.assertEqual(status['status'], 'ok')
                self.assertEqual(status['tunnels'], [])
                denied = other.emit('ssh_tunnel', {'terminal_id': 'carrier', 'operation': 'stop', 'tunnel_id': tunnel.id}, callback=True)
                self.assertEqual(denied['status'], 'failed')
                self.assertFalse(tunnel.closed.is_set())
                self.client.disconnect()
                self.assertTrue(tunnel.closed.is_set())
                with self.assertRaises(OSError):
                    socket.create_connection(('127.0.0.1', tunnel.port), timeout=1)
            finally:
                other.disconnect()

    def test_non_loopback_remote_peer_is_rejected(self):
        with ssh_server(gateway_ports='yes') as ssh, tcp_server(lambda peer: peer.sendall(b'ok')) as port:
            tunnel = self.start(ssh, 'remote', port)
            fake = type('Channel', (), {'closed': False, 'close': lambda channel: setattr(channel, 'closed', True)})()
            tunnel._accept_remote(fake, ('192.0.2.10', 1234), ('127.0.0.1', tunnel.port))
            self.assertTrue(fake.closed)
            with socket.create_connection(('127.0.0.1', tunnel.port), timeout=5) as client:
                self.assertEqual(client.recv(16), b'ok')

    def test_pending_owner_disconnect_and_ssh_close_prevent_setup(self):
        spec = {'direction': 'local', 'listen_port': 0, 'target_host': '127.0.0.1', 'target_port': 80}
        with ssh_server(forwarding='yes') as ssh:
            bridge = self.carrier(ssh)
            with patch.object(standterm.socketio, 'start_background_task'):
                pending = []
                for _ in range(2):
                    result = self.client.emit('ssh_tunnel', {
                        'terminal_id': 'carrier', 'operation': 'start', 'spec': spec,
                    }, callback=True)
                    tunnel = standterm.user_ssh_tunnels[result['tunnel']['tunnel_id']]['tunnel']
                    pending.append(tunnel)
                    self.tunnels.append(tunnel)
                stopped = self.client.emit('ssh_tunnel', {
                    'terminal_id': 'carrier', 'operation': 'stop', 'tunnel_id': pending[0].id,
                }, callback=True)
                self.assertEqual(stopped['tunnel']['status'], 'stopped')
                self.client.disconnect()
            for tunnel in pending:
                tunnel.start()
                self.assertTrue(tunnel.closed.is_set())
                self.assertTrue(tunnel.setup_done.is_set())
                self.assertIsNone(tunnel.listener)
            self.assertTrue(ssh.get_transport().is_authenticated())
            # SSH closure also fences an owner whose browser is still connected.
            self.client = fixture.make_socket_client(self.flask_client)
            self.sid = fixture.current_sid_for_session(self.session)
            bridge.attach(self.sid)
            with patch.object(standterm.socketio, 'start_background_task'):
                result = self.client.emit('ssh_tunnel', {
                    'terminal_id': 'carrier', 'operation': 'start', 'spec': spec,
                }, callback=True)
                tunnel = standterm.user_ssh_tunnels[result['tunnel']['tunnel_id']]['tunnel']
                self.tunnels.append(tunnel)
                standterm.close_bridge(bridge)
            tunnel.start()
            self.assertTrue(tunnel.closed.is_set())
            self.assertIsNone(tunnel.listener)

    def test_slow_remote_target_does_not_block_other_forward_or_ssh(self):
        entered, release = threading.Event(), threading.Event()
        original_connect = socket.create_connection
        blocked_host = 'blocked-target.invalid'

        def delayed_connect(address, *args, **kwargs):
            if address[0] == blocked_host:
                entered.set()
                release.wait(5)
                raise OSError('Simulated slow target')
            return original_connect(address, *args, **kwargs)

        with ssh_server(forwarding='yes') as ssh, tcp_server(lambda peer: peer.sendall(b'ok')) as port:
            slow = self.start(ssh, 'remote', port, target_host=blocked_host)
            healthy = self.start(ssh, 'remote', port)
            with patch.object(ssh_tunnels.socket, 'create_connection', delayed_connect):
                with original_connect(('127.0.0.1', slow.port), timeout=5):
                    try:
                        self.assertTrue(entered.wait(2))
                        with original_connect(('127.0.0.1', healthy.port), timeout=2) as client:
                            self.assertEqual(client.recv(16), b'ok')
                        _, stdout, _ = ssh.exec_command('printf tunnel-shell-alive', timeout=2)
                        self.assertEqual(stdout.read(), b'tunnel-shell-alive')
                        slow.stop()
                    finally:
                        release.set()
            fixture.wait_until(lambda: not slow.channels, 'Stopped slow target retained its slot')

    @unittest.skipUnless(shutil.which('sshd') or Path('/usr/sbin/sshd').is_file(), 'OpenSSH server is required')
    def test_both_directions_use_final_transport_through_three_jumps(self):
        from ssh_jump_smoke import SSHJumpTests, server
        helper = SSHJumpTests()
        helper.setUp()
        try:
            servers = [helper.stack.enter_context(server()) for _ in range(4)]
            route = helper.route(servers)
            helper.trust(route, servers)
            bridge = helper.bridge(servers)
            bridge.owner_session, bridge.terminal_id = self.session, 'carrier'
            success, result = helper.connect(bridge, route)
            self.assertTrue(success, result)
            bridge.attach(self.sid)
            standterm.set_bridge(self.session, 'carrier', bridge)
            with tcp_server(lambda peer: peer.sendall(b'final-target')) as port:
                for direction in ('local', 'remote'):
                    tunnel = self.start(bridge.ssh, direction, port)
                    self.assertIs(tunnel.transport, bridge.ssh.get_transport())
                    with socket.create_connection(('127.0.0.1', tunnel.port), timeout=5) as client:
                        self.assertEqual(client.recv(32), b'final-target')
                standterm.close_bridge(bridge)
                self.assertTrue(all(tunnel.closed.is_set() for tunnel in self.tunnels))
        finally:
            helper.doCleanups()

    def test_busy_listening_port_and_server_denial_leave_no_forward(self):
        with ssh_server(forwarding='no') as ssh, tcp_server(lambda peer: None) as port:
            self.carrier(ssh)
            for direction, listen_port in (('local', port), ('remote', 0)):
                result = self.client.emit('ssh_tunnel', {
                    'terminal_id': 'carrier', 'operation': 'start', 'spec': {
                        'direction': direction, 'listen_port': listen_port,
                        'target_host': '127.0.0.1', 'target_port': port,
                    },
                }, callback=True)
                tunnel = standterm.user_ssh_tunnels[result['tunnel']['tunnel_id']]['tunnel']
                self.tunnels.append(tunnel)
                fixture.wait_until(lambda: tunnel.setup_done.is_set(), 'Rejected setup did not finish')
                self.assertEqual(tunnel.status, 'failed')
                self.assertTrue(tunnel.closed.is_set())
                self.assertFalse(tunnel.forwarding.routes)
            self.assertTrue(ssh.get_transport().is_authenticated())

    def test_invalid_specs_and_agent_commands_do_not_create_tunnels(self):
        base = {'direction': 'local', 'listen_port': 0, 'target_host': '127.0.0.1', 'target_port': 80}
        for change in ({'direction': 'socks'}, {'direction': []}, {'listen_port': True}, {'target_port': 0},
                       {'listen_port': 65536}, {'target_host': 'http://example.com'}, {'target_host': 'a\nb'}):
            with self.assertRaises(ValueError):
                parse_tunnel_spec({**base, **change})
        self.assertEqual(parse_tunnel_spec({**base, 'target_host': '::1'})['target_host'], '::1')
        self.client.disconnect()
        helper = agent_fixture.AgentTunnelTests()
        helper.setUp()
        try:
            token, _ = helper.mint()
            hello = standterm.app.test_client().post('/agent/external/command', json={
                'op': 'hello', 'token': token, 'terminal_id': 'main',
            }, environ_base={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(hello.json['status'], 'ok')
            for operation in ('ssh_tunnel', 'ssh-tunnel', 'tunnel-start', 'tunnel-stop'):
                response = standterm.app.test_client().post('/agent/external/command', json={
                    'op': operation, 'token': token, 'terminal_id': 'main', 'spec': base,
                }, environ_base={'REMOTE_ADDR': '127.0.0.1'})
                self.assertNotEqual(response.json.get('status'), 'ok')
            self.assertFalse(standterm.user_ssh_tunnels)
        finally:
            helper.tearDown()


if __name__ == '__main__':
    unittest.main()
