"""Exercise staged login against independent SSH servers and live forwarding."""
import contextlib
from pathlib import Path
import queue
import select
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paramiko
from terminal_backends.base import TerminalBridge
from terminal_backends.ssh import SSHBridge
from terminal_backends.ssh_host_keys import SSHHostKeyStore


@contextlib.contextmanager
def password_server(password='test-password', deny_shell=False, keyboard_prompts=0):
    key = paramiko.RSAKey.generate(2048)
    stopped = threading.Event()
    transports, threads, attempts = [], [], []
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(5)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]

    class Server(paramiko.ServerInterface):
        def __init__(self):
            self.destinations = {}

        def get_allowed_auths(self, username):
            return 'keyboard-interactive' if keyboard_prompts else 'password'

        def check_auth_password(self, username, supplied):
            if keyboard_prompts:
                return paramiko.AUTH_FAILED
            attempts.append((username, supplied))
            return paramiko.AUTH_SUCCESSFUL if supplied == password else paramiko.AUTH_FAILED

        def check_auth_interactive(self, username, submethods):
            return paramiko.InteractiveQuery('Sign in', '', *[('Password:', False)] * keyboard_prompts)

        def check_auth_interactive_response(self, responses):
            attempts.append(('interactive', responses))
            return paramiko.AUTH_SUCCESSFUL if responses == [password] else paramiko.AUTH_FAILED

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_direct_tcpip_request(self, channel_id, origin, destination):
            self.destinations[channel_id] = destination
            return paramiko.OPEN_SUCCEEDED

        def check_channel_pty_request(self, *args):
            return True

        def check_channel_shell_request(self, channel):
            return not deny_shell

    def relay(channel, destination):
        try:
            with socket.create_connection(destination, timeout=3) as peer:
                while not stopped.is_set():
                    ready, _, _ = select.select([channel, peer], [], [], 0.1)
                    for source in ready:
                        data = source.recv(32768)
                        if not data:
                            return
                        (peer if source is channel else channel).sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            with contextlib.suppress(EOFError, OSError):
                channel.close()

    def serve(sock):
        with paramiko.Transport(sock) as transport:
            transports.append(transport)
            transport.add_server_key(key)
            server = Server()
            try:
                transport.start_server(server=server)
                while not stopped.is_set() and transport.is_active():
                    channel = transport.accept(0.1)
                    if channel is None:
                        continue
                    destination = server.destinations.get(channel.get_id())
                    if destination:
                        thread = threading.Thread(target=relay, args=(channel, destination))
                        threads.append(thread)
                        thread.start()
                    else:
                        stopped.wait(15)
                        channel.close()
            except (paramiko.SSHException, EOFError, OSError):
                pass

    def accept():
        while not stopped.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=serve, args=(sock,))
            threads.append(thread)
            thread.start()

    accept_thread = threading.Thread(target=accept)
    accept_thread.start()
    try:
        yield {'port': port, 'key': key, 'attempts': attempts, 'transports': transports}
    finally:
        stopped.set()
        listener.close()
        accept_thread.join(3)
        for transport in transports:
            transport.close()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive(), 'SSH test server did not stop'


class SSHLoginTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='standterm-login-'))
        self.known_hosts = Path(directory) / 'known_hosts'
        self.events = queue.Queue()
        self.outputs = []
        self.result = []

    def start(self, servers, changed=False):
        self.route = [{'node_id': f'node-{i}', 'host': '127.0.0.1', 'port': server['port'],
                       'username': f'user-{i}', 'password': '', 'host_key_alias': f'login-site-{i}',
                       'browser_key': None} for i, server in enumerate(servers)]
        if changed:
            store = SSHHostKeyStore(paramiko, self.known_hosts)
            store.update(store.snapshot('login-site-1', servers[1]['port']), paramiko.RSAKey.generate(2048))
        with patch.object(TerminalBridge, '_default_runtime', object()):
            self.bridge = SSHBridge('session', get_paramiko=lambda: paramiko, ssh_term='xterm',
                                    local_public_key_types=[], known_hosts_path=self.known_hosts)
        self.bridge.set_browser_signer_sid('initiator')
        def emit(value):
            self.outputs.append(value)
            if value['message_type'] == 'ssh_login_prompt':
                self.events.put(value)
        self.bridge.emit_output = emit
        def connect():
            self.result.append(self.bridge.connect('unused', 22, 'unused', route=self.route,
                                                  attempt_id='attempt-a', interactive_login=True))
        self.thread = threading.Thread(target=connect)
        self.thread.start()
        def cleanup():
            self.bridge.close()
            self.thread.join(5)
            self.assertFalse(self.thread.is_alive())
        self.addCleanup(cleanup)

    def prompt(self, node, kind):
        value = self.events.get(timeout=10)
        self.assertEqual((value['node_id'], value['kind']), (f'node-{node}', kind))
        return value

    def answer(self, prompt, **fields):
        response = {key: prompt[key] for key in ('attempt_id', 'node_id', 'kind', 'request_id')}
        response.update(terminal_id='main', **fields)
        self.assertTrue(self.bridge.resolve_login_input('initiator', response))
        self.assertFalse(self.bridge.resolve_login_input('initiator', response))

    def test_three_sites_pause_before_password_and_keep_upstream_logins(self):
        servers = [self.stack.enter_context(password_server(f'password-{i}')) for i in range(3)]
        self.start(servers, changed=True)
        for i in range(3):
            trust = self.prompt(i, 'host_key')
            self.assertEqual(servers[i]['attempts'], [])
            if i == 1:
                self.assertIn('changed', trust['message'])
            self.answer(trust, accept=True)
            password = self.prompt(i, 'password')
            if i == 1:
                self.answer(password, password='wrong-password')
                password = self.prompt(i, 'password')
                self.assertIn('rejected', password['message'])
                self.assertEqual(len(servers[0]['attempts']), 1)
            self.answer(password, password=f'password-{i}')
        self.thread.join(10)
        self.assertEqual(self.result, [(True, None)])
        self.assertEqual([len(server['attempts']) for server in servers], [1, 2, 1])
        self.assertEqual([len(server['transports']) for server in servers], [1, 1, 1])
        self.assertEqual([event['node_id'] for event in self.outputs if event.get('phase') == 'authenticated'],
                         ['node-0', 'node-1', 'node-2'])
        self.assertFalse(any('password-0' in str(event) for event in self.outputs))
        self.assertIsNone(self.bridge._login_request)

    def test_cancel_while_waiting_closes_every_upstream_transport(self):
        servers = [self.stack.enter_context(password_server()) for _ in range(2)]
        self.start(servers)
        self.answer(self.prompt(0, 'host_key'), accept=True)
        self.answer(self.prompt(0, 'password'), password='test-password')
        waiting = self.prompt(1, 'host_key')
        self.bridge.close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.result[0][0])
        self.assertFalse(self.bridge.resolve_login_input('initiator', {**waiting, 'terminal_id':'main', 'accept':True}))
        self.assertEqual(servers[1]['attempts'], [])
        self.assertEqual(self.bridge._connection_resources, [])

    def test_wrong_binding_and_stale_known_hosts_cannot_resume(self):
        server = self.stack.enter_context(password_server())
        self.start([server])
        prompt = self.prompt(0, 'host_key')
        response = {**prompt, 'terminal_id':'main', 'accept':True}
        for field in ('terminal_id', 'node_id', 'attempt_id', 'request_id', 'kind'):
            self.assertFalse(self.bridge.resolve_login_input('initiator', {**response, field:'other'}))
        self.assertFalse(self.bridge.resolve_login_input('other-sid', response))
        store = SSHHostKeyStore(paramiko, self.known_hosts)
        store.update(store.snapshot('login-site-0', server['port']), paramiko.RSAKey.generate(2048))
        self.answer(prompt, accept=True)
        self.thread.join(5)
        self.assertFalse(self.result[0][0])
        self.assertEqual(server['attempts'], [])

    def test_shell_failure_does_not_complete_target(self):
        server = self.stack.enter_context(password_server(deny_shell=True))
        self.start([server])
        self.answer(self.prompt(0, 'host_key'), accept=True)
        self.answer(self.prompt(0, 'password'), password='test-password')
        self.thread.join(5)
        self.assertFalse(self.result[0][0])
        self.assertEqual(self.result[0][1]['route_context']['phase'], 'shell')

    def test_prompt_timeout_is_bounded(self):
        server = self.stack.enter_context(password_server())
        with patch('terminal_backends.ssh.SSH_LOGIN_TIMEOUT_SECONDS', 0.1):
            self.start([server])
            self.prompt(0, 'host_key')
            self.thread.join(5)
        self.assertFalse(self.result[0][0])
        self.assertIn('timed out', self.result[0][1]['message'])
        self.assertIsNone(self.bridge._login_request)

    def test_single_password_keyboard_interactive_remains_supported(self):
        server = self.stack.enter_context(password_server(keyboard_prompts=1))
        self.start([server])
        self.answer(self.prompt(0, 'host_key'), accept=True)
        self.answer(self.prompt(0, 'password'), password='test-password')
        self.thread.join(5)
        self.assertEqual(self.result, [(True, None)])
        self.assertEqual(server['attempts'], [('interactive', ['test-password'])])

    def test_multiple_keyboard_prompts_fail_without_completing_node(self):
        server = self.stack.enter_context(password_server(keyboard_prompts=2))
        self.start([server])
        self.answer(self.prompt(0, 'host_key'), accept=True)
        self.answer(self.prompt(0, 'password'), password='test-password')
        self.thread.join(5)
        self.assertFalse(self.result[0][0])
        self.assertIn('additional interactive SSH authentication', self.result[0][1]['message'])
        self.assertFalse(any(event.get('phase') == 'authenticated' for event in self.outputs))

    def test_direct_localhost_keeps_local_keys_and_setup_offer(self):
        node = {'node_id':'local', 'host':'127.0.0.1', 'port':22, 'username':'local-user',
                'password':'', 'host_key_alias':'', 'browser_key':None}
        with patch.object(TerminalBridge, '_default_runtime', object()):
            bridge = SSHBridge('session', get_paramiko=lambda:paramiko, ssh_term='xterm',
                               local_public_key_types=[], known_hosts_path=self.known_hosts)
        self.addCleanup(bridge.close)
        bridge.emit_output = self.outputs.append
        bridge.ssh = Mock()
        with patch.object(bridge, '_connect_with_local_keys', return_value=(True, None)) as local_keys:
            success, result = bridge.connect('127.0.0.1', 22, 'local-user', route=[node],
                                             attempt_id='local-attempt', interactive_login=True)
        self.assertTrue(success, result)
        local_keys.assert_called_once()
        self.assertEqual(bridge.auth_method, 'host-key')
        with patch.object(bridge, '_connect_with_local_keys', return_value=(False, 'not authorized')), \
                patch.object(bridge, '_get_local_key_setup_availability', return_value={'can_offer':True}), \
                patch.object(bridge, '_get_missing_local_public_keys', return_value=['key']), \
                patch.object(bridge, '_build_local_key_setup_hint', return_value={'action_type':'offer_localhost_key_setup'}):
            success, result = bridge.connect('127.0.0.1', 22, 'local-user', route=[node],
                                             attempt_id='setup-attempt', interactive_login=True)
        self.assertFalse(success)
        self.assertEqual(result['action_type'], 'offer_localhost_key_setup')
        self.assertEqual(result['route_context']['node_id'], 'local')


if __name__ == '__main__':
    unittest.main()
