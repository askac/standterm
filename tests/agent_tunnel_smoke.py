"""Exercise unchanged clients through a real, isolated OpenSSH reverse forward."""
import contextlib
import getpass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
test_args = sys.argv[1:]
sys.argv[1:] = []
import agent_backend_smoke as fixture
from agent_tunnel import AgentTunnel, tunnel_ingress
sys.argv[1:] = test_args

standterm = fixture.standterm
paramiko = standterm.get_paramiko()


@contextlib.contextmanager
def ssh_server(gateway_ports='no'):
    executable = shutil.which('sshd') or '/usr/sbin/sshd'
    if not Path(executable).is_file():
        raise unittest.SkipTest('OpenSSH server is required for transport integration.')
    with tempfile.TemporaryDirectory(prefix='standterm-tunnel-sshd-') as directory:
        root = Path(directory)
        host_key = paramiko.RSAKey.generate(2048)
        host_key.write_private_key_file(str(root / 'host_key'))
        key = paramiko.RSAKey.generate(2048)
        (root / 'authorized_keys').write_text(key.get_name() + ' ' + key.get_base64() + '\n')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        config = '\n'.join([
            'Port ' + str(port), 'ListenAddress 127.0.0.1',
            'HostKey ' + str(root / 'host_key'),
            'AuthorizedKeysFile ' + str(root / 'authorized_keys'),
            'PidFile ' + str(root / 'pid'),
            'StrictModes no', 'UsePAM no', 'PasswordAuthentication no',
            'KbdInteractiveAuthentication no', 'PermitRootLogin prohibit-password',
            'AllowTcpForwarding remote', 'GatewayPorts ' + gateway_ports,
            'Subsystem sftp internal-sftp', 'LogLevel ERROR',
        ])
        (root / 'sshd_config').write_text(config + '\n')
        with (root / 'log').open('w+') as log:
            process = subprocess.Popen([executable, '-D', '-e', '-f', str(root / 'sshd_config')],
                                       stdout=log, stderr=log)
            client = paramiko.SSHClient()
            client.get_host_keys().add('[127.0.0.1]:' + str(port), host_key.get_name(), host_key)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError('Fixture sshd failed: ' + log.read())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.02)
                client.connect('127.0.0.1', port=port, username=getpass.getuser(),
                               pkey=key, allow_agent=False, look_for_keys=False, timeout=5)
                yield client
            finally:
                client.close()
                process.terminate()
                process.wait(timeout=5)


def targets(session_token, sid, terminal_ids):
    result = []
    for terminal_id in terminal_ids:
        state = standterm.get_agent_state(session_token, terminal_id, sid)
        result.append({
            'terminal_id': terminal_id, 'agent_binding_id': state.agent_binding_id,
            'mode_version': state.mode_version, 'privacy_version': state.privacy_version,
        })
    return result


def request_json(url, data=None):
    request = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        return response.status, json.loads(body) if response.headers.get_content_type() == 'application/json' else body


class AgentTunnelTests(unittest.TestCase):
    def setUp(self):
        fixture.reset_state()
        self.client = fixture.make_client()
        self.session = fixture.current_session_token()
        self.sid = fixture.current_sid_for_session(self.session)
        self.bridges = {}
        for terminal_id in ('main', 'second', 'third'):
            bridge = fixture.DummyBridge(self.session, terminal_id)
            standterm.set_bridge(self.session, terminal_id, bridge)
            self.bridges[terminal_id] = bridge
            self.client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': terminal_id})
            self.client.emit(standterm.AGENT_EVENT_MODE_SET, {'terminal_id': terminal_id, 'mode': 'approval'})

    def tearDown(self):
        for tunnel in list(standterm.agent_tunnels.values()):
            tunnel.close()
            tunnel._cleanup_done.wait(5)
        self.client.disconnect()

    def mint(self, terminal_id='main'):
        token, record, error = standterm.mint_external_agent_attach_token(self.session, terminal_id, self.sid)
        self.assertIsNone(error)
        return token, record

    def carrier(self, ssh):
        bridge = standterm.SSHBridge(self.session, 'carrier', get_paramiko=lambda: paramiko,
                                     ssh_term='xterm', local_public_key_types=[])
        bridge.ssh.close()
        bridge.ssh = ssh
        standterm.set_bridge(self.session, 'carrier', bridge)
        return bridge

    def open_tunnel(self, ssh, terminal_ids=('main', 'second')):
        bridge = self.carrier(ssh)
        result = self.client.emit('agent_tunnel', {
            'terminal_id': 'carrier', 'operation': 'apply',
            'targets': targets(self.session, self.sid, terminal_ids),
        }, callback=True)
        self.assertEqual(result['status'], 'ready', result)
        return bridge.agent_tunnel

    def command(self, tunnel, terminal_id, operation, **fields):
        return request_json(tunnel.runtime['base_url'] + '/agent/external/command', {
            'op': operation, 'token': tunnel.grants[terminal_id]['token'],
            'terminal_id': terminal_id, **fields,
        })[1]

    def approve(self, terminal_id, action_id):
        state = standterm.get_agent_state(self.session, terminal_id, self.sid)
        self.client.emit(standterm.AGENT_EVENT_ACTION_APPROVE,
                         standterm.public_agent_action(state.pending_actions[action_id]))

    def test_revoke_cancels_only_its_pending_input(self):
        first, _ = self.mint()
        second, _ = self.mint()
        actions = []
        for token in (first, second):
            result = standterm.process_external_agent_command({
                'op': 'send', 'terminal_id': 'main', 'token': token, 'data': 'approved text',
            })
            actions.append(result['action_id'])
        revoked = standterm.process_external_agent_command({'op': 'revoke', 'token': first, 'terminal_id': 'main'})
        self.assertEqual(revoked['status'], 'ok')
        self.approve('main', actions[0])
        self.assertEqual(self.bridges['main'].writes, [])
        self.approve('main', actions[1])
        self.assertEqual(self.bridges['main'].writes, ['approved text'])

    def test_revoke_does_not_wait_for_in_flight_terminal_io(self):
        self.client.emit(standterm.AGENT_EVENT_MODE_SET, {'terminal_id': 'main', 'mode': 'direct'})
        token, _ = self.mint()
        entered = threading.Event()
        release = threading.Event()
        result = []
        bridge = self.bridges['main']

        def slow_write(data):
            entered.set()
            release.wait(5)
            bridge.writes.append(data)

        def send():
            result.append(standterm.process_external_agent_command({
                'op': 'send', 'token': token, 'terminal_id': 'main', 'data': 'x' * 1024,
            }))

        with patch.object(bridge, 'write', slow_write):
            thread = threading.Thread(target=send)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                revoked = standterm.process_external_agent_command({'op': 'revoke', 'token': token, 'terminal_id': 'main'})
                self.assertEqual(revoked['status'], 'ok')
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(bridge.writes), 1)
        self.assertEqual(result[0]['status'], standterm.AGENT_STATUS_FAILED)
        self.assertEqual(result[0]['error_code'], standterm.AGENT_ERROR_EXTERNAL_AGENT_REVOKED)
        state = standterm.get_agent_state(self.session, 'main', self.sid)
        self.assertTrue(all(action['status'] == standterm.AGENT_STATUS_FAILED for action in state.pending_actions.values()))

    def test_maximum_tail_wait_allows_parallel_heartbeat(self):
        with ssh_server() as ssh:
            tunnel = self.open_tunnel(ssh)
            info = standterm.build_agent_tunnel_info(tunnel)
            process = subprocess.Popen([
                info['python_path'], info['scripts']['agent_cli'], '--agentinfo', info['agentinfo_url'],
                '--terminal', 'main', 'tail', '--wait-ms', str(standterm.AGENT_EXTERNAL_TAIL_MAX_WAIT_MS),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(3):
                    time.sleep(0.1)
                    self.assertEqual(self.command(tunnel, 'second', 'heartbeat')['status'], 'ok')
                stdout, stderr = process.communicate(timeout=40)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)['status'], 'ok')
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(5)

    def test_real_forward_shared_clients_and_precise_stop(self):
        local_token, local_record = self.mint()
        local_payload = standterm.build_external_agent_token_payload(
            local_token, local_record, 'main', 'http://127.0.0.1:5010')
        local_path = Path(local_payload['handoff_path'])
        local_bytes = local_path.read_bytes()
        with ssh_server() as ssh:
            tunnel = self.open_tunnel(ssh)
            root = Path(str(tunnel.runtime['root']))
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            status, info = request_json(tunnel.runtime['base_url'] + '/agentinfo')
            self.assertEqual(status, 200)
            self.assertEqual(set(info['terminal_handoffs']), {'main', 'second'})
            self.assertNotIn('agt_', json.dumps(info))
            self.assertNotIn('tls_ca_cert_path', info)
            wrong_destination = type('Channel', (), {'closed': False, 'close': lambda channel: setattr(channel, 'closed', True)})()
            tunnel._accept(wrong_destination, ('127.0.0.1', 12345), ('127.0.0.1', tunnel.port + 1))
            self.assertTrue(wrong_destination.closed)
            for path in info['scripts'].values():
                self.assertTrue(Path(path).is_file(), path)
            for terminal_id in ('main', 'second'):
                args = [info['python_path'], info['scripts']['agent_cli'], '--agentinfo',
                        info['agentinfo_url'], '--terminal', terminal_id]
                for operation in ('discover', 'hello', 'screen'):
                    output = subprocess.run(args + [operation], capture_output=True, text=True, timeout=10)
                    self.assertEqual(output.returncode, 0, output.stderr)
                    self.assertNotIn('agt_', output.stdout)
            self.assertEqual(local_path.read_bytes(), local_bytes)
            endpoint = tunnel.runtime['base_url'] + '/agent/external/command'
            for operation in ('hello', 'heartbeat', 'revoke', 'screen'):
                status, denied = request_json(endpoint, {'op': operation, 'token': local_token, 'terminal_id': 'main'})
                self.assertEqual(status, 400)
                self.assertEqual(denied['error_code'], standterm.AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED)
            for path in ('/', '/agent/external/token', '/agent/external/dev-token',
                         '/agent/external/dev-command', '/socket.io/'):
                self.assertEqual(request_json(tunnel.runtime['base_url'] + path, {})[0], 404)
            remote_action = self.command(tunnel, 'main', 'send', data='remote input')['action_id']
            local_action = standterm.process_external_agent_command({
                'op': 'send', 'token': local_token, 'terminal_id': 'main', 'data': 'local input',
            })['action_id']
            tunnel.close()
            self.assertTrue(tunnel._cleanup_done.wait(5))
            self.approve('main', remote_action)
            self.assertEqual(self.bridges['main'].writes, [])
            self.approve('main', local_action)
            self.assertEqual(self.bridges['main'].writes, ['local input'])
            self.assertTrue(ssh.get_transport().is_active())
            with ssh.open_sftp() as sftp:
                self.assertTrue(sftp.stat('.'))
            self.assertFalse(root.exists())
            self.assertEqual(local_path.read_bytes(), local_bytes)

    def test_reenrollment_and_carrier_disconnect(self):
        with ssh_server('clientspecified') as ssh:
            tunnel = self.open_tunnel(ssh)
            old_token = tunnel.grants['main']['token']
            second_token = tunnel.grants['second']['token']
            action = self.command(tunnel, 'main', 'send', data='removed input')['action_id']
            standterm.update_agent_tunnel_targets(tunnel, targets(self.session, self.sid, ['second']))
            self.approve('main', action)
            self.assertEqual(self.bridges['main'].writes, [])
            self.assertEqual(tunnel.grants['second']['token'], second_token)
            self.assertEqual(set(standterm.build_agent_tunnel_info(tunnel)['terminal_handoffs']), {'second'})
            standterm.update_agent_tunnel_targets(tunnel, targets(self.session, self.sid, ['main', 'second']))
            self.assertNotEqual(tunnel.grants['main']['token'], old_token)
            ssh.close()
            result = standterm.process_external_agent_command({'op': 'hello', 'token': second_token})
            self.assertEqual(result['error_code'], standterm.AGENT_ERROR_EXTERNAL_AGENT_DISCONNECTED)

    def test_wildcard_listener_never_becomes_ready(self):
        with ssh_server('yes') as ssh:
            bridge = self.carrier(ssh)
            result = self.client.emit('agent_tunnel', {
                'terminal_id': 'carrier', 'operation': 'apply',
                'targets': targets(self.session, self.sid, ['main']),
            }, callback=True)
            self.assertEqual(result['status'], 'failed', result)
            self.assertIn('loopback', result['message'])
            self.assertFalse(bridge.agent_tunnel.ready)
            self.assertEqual(bridge.agent_tunnel.grants, {})
            self.assertTrue(ssh.get_transport().is_active())

    def test_incomplete_bundle_never_becomes_ready(self):
        original = AgentTunnel.write_file

        def omit_dependency(tunnel, path, data):
            if path == 'scripts/agent_input.py':
                return str(tunnel.runtime['root'] / path)
            return original(tunnel, path, data)

        with ssh_server() as ssh, patch.object(AgentTunnel, 'write_file', omit_dependency):
            bridge = self.carrier(ssh)
            result = self.client.emit('agent_tunnel', {
                'terminal_id': 'carrier', 'operation': 'apply',
                'targets': targets(self.session, self.sid, ['main']),
            }, callback=True)
            self.assertEqual(result['status'], 'failed', result)
            self.assertFalse(bridge.agent_tunnel.ready)
            self.assertEqual(bridge.agent_tunnel.grants, {})

    def test_new_target_is_not_advertised_before_its_handoff(self):
        with ssh_server() as ssh:
            tunnel = self.open_tunnel(ssh, ('main',))
            entered = threading.Event()
            release = threading.Event()
            original = tunnel.write_json
            errors = []

            def blocked_write(path, payload):
                if payload.get('terminal_id') == 'second':
                    entered.set()
                    self.assertTrue(release.wait(5))
                return original(path, payload)

            def update():
                try:
                    standterm.update_agent_tunnel_targets(tunnel, targets(self.session, self.sid, ['main', 'second']))
                except Exception as exc:
                    errors.append(exc)

            with patch.object(tunnel, 'write_json', blocked_write):
                thread = threading.Thread(target=update)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    status, info = request_json(tunnel.runtime['base_url'] + '/agentinfo')
                    self.assertEqual(status, 200)
                    self.assertEqual(set(info['terminal_handoffs']), {'main'})
                    self.assertEqual(self.command(tunnel, 'main', 'heartbeat')['status'], 'ok')
                finally:
                    release.set()
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(set(standterm.build_agent_tunnel_info(tunnel)['terminal_handoffs']), {'main', 'second'})

    def test_file_copy_keeps_approval_and_checks_both_grants(self):
        for terminal_id in ('main', 'second'):
            bridge = fixture.make_local_file_test_bridge(self.session, terminal_id)
            bridge.attach(self.sid)
            standterm.set_bridge(self.session, terminal_id, bridge)
        local_destination, _ = self.mint('second')
        with ssh_server() as ssh, tempfile.TemporaryDirectory(prefix='standterm-tunnel-copy-') as directory:
            tunnel = self.open_tunnel(ssh)
            source = Path(directory) / 'source.bin'
            destination = Path(directory) / 'destination.bin'
            source.write_bytes(b'unchanged file-copy contract')
            fields = dict(source_path=str(source), destination_path=str(destination),
                          destination_terminal_id='second', destination_token=local_destination)
            denied = self.command(tunnel, 'main', 'file-copy', **fields)
            self.assertEqual(denied['error_code'], standterm.AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED)
            fields['destination_token'] = tunnel.grants['second']['token']
            pending = self.command(tunnel, 'main', 'file-copy', **fields)
            self.assertEqual(pending['status'], standterm.AGENT_STATUS_PENDING_APPROVAL)
            self.assertFalse(destination.exists())
            self.approve('main', pending['action_id'])
            fixture.wait_until(destination.exists, 'approved tunnel file-copy did not finish')
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            fields['destination_path'] = str(Path(directory) / 'cancelled.bin')
            pending = self.command(tunnel, 'main', 'file-copy', **fields)
            standterm.update_agent_tunnel_targets(tunnel, targets(self.session, self.sid, ['main']))
            self.approve('main', pending['action_id'])
            self.assertFalse(Path(fields['destination_path']).exists())

    def test_missing_source_reference_is_rejected_before_ssh(self):
        bridge = type('Bridge', (), {'ssh': type('SSH', (), {'get_transport': lambda _: None})()})()
        original = Path.is_file
        for missing in ('docs/examples/standterm-file-transfer',
                        'docs/examples/standterm-privileged-hitl/SKILL.md',
                        'docs/examples/standterm-external-agent-skill/references/connection.md'):
            tunnel = AgentTunnel(bridge, self.sid, standterm.APP_DIR, build_info=None,
                                 dispatch=None, revoke=lambda _: None)
            def is_file(path):
                return False if missing in path.as_posix() else original(path)
            with patch.object(Path, 'is_file', is_file), self.assertRaisesRegex(RuntimeError, 'incomplete'):
                tunnel._provision()

    def test_exec_and_forward_timeouts_preserve_the_ssh_transport(self):
        with ssh_server() as ssh:
            bridge = self.carrier(ssh)
            tunnel = AgentTunnel(bridge, self.sid, standterm.APP_DIR,
                                 build_info=None, dispatch=None, revoke=lambda _: None)
            channel = tunnel._setup_channel()
            release_exec = threading.Event()
            with patch('agent_tunnel.TUNNEL_COMMAND_TIMEOUT', 0.1):
                try:
                    with self.assertRaisesRegex(RuntimeError, 'timed out'):
                        tunnel._channel_call(channel, lambda: release_exec.wait(5))
                    self.assertTrue(channel.closed)
                finally:
                    release_exec.set()
            release_forward = threading.Event()
            original = ssh.get_transport().request_port_forward

            def delayed_forward(*args, **kwargs):
                release_forward.wait(5)
                return original(*args, **kwargs)

            with patch.object(ssh.get_transport(), 'request_port_forward', delayed_forward), \
                    patch('agent_tunnel.TUNNEL_COMMAND_TIMEOUT', 0.1):
                try:
                    with self.assertRaisesRegex(RuntimeError, 'timed out'):
                        tunnel._request_forward()
                    self.assertTrue(tunnel._forward_pending.is_set())
                    started = time.monotonic()
                    tunnel.close()
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertFalse(tunnel.active)
                    _, stdout, _ = ssh.exec_command('printf ssh-alive', timeout=2)
                    self.assertEqual(stdout.read(), b'ssh-alive')
                finally:
                    release_forward.set()
                fixture.wait_until(lambda: not tunnel._forward_pending.is_set(), 'late forward did not settle')
                self.assertTrue(ssh.get_transport().is_active())

    def test_slow_cancel_does_not_delay_revocation_or_stop(self):
        with ssh_server() as ssh:
            tunnel = self.open_tunnel(ssh)
            token = tunnel.grants['main']['token']
            entered = threading.Event()
            release = threading.Event()
            original = ssh.get_transport().global_request

            def slow_cancel(kind, data=None, wait=True):
                if kind == 'cancel-tcpip-forward':
                    self.assertFalse(wait)
                    entered.set()
                    release.wait(5)
                return original(kind, data, wait)

            with patch.object(ssh.get_transport(), 'global_request', slow_cancel):
                try:
                    started = time.monotonic()
                    tunnel.close()
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(standterm.agent_tunnel_status(tunnel)['cleanup_pending'])
                    result = standterm.process_external_agent_command({'op': 'hello', 'token': token})
                    self.assertEqual(result['error_code'], standterm.AGENT_ERROR_EXTERNAL_AGENT_REVOKED)
                    _, stdout, _ = ssh.exec_command('printf ssh-alive', timeout=2)
                    self.assertEqual(stdout.read(), b'ssh-alive')
                finally:
                    release.set()
                    self.assertTrue(tunnel._cleanup_done.wait(5))

    def test_remote_commands_do_not_inherit_windows_paths_or_ca(self):
        with ssh_server() as ssh:
            tunnel = self.open_tunnel(ssh)
            with patch.object(standterm.sys, 'platform', 'win32'), \
                    patch.object(standterm, 'get_external_agent_tls_ca_cert_path', return_value='C:\\core\\ca.crt'):
                info = standterm.build_agent_tunnel_info(tunnel)
                grant = tunnel.grants['main']
                runtime = {**tunnel.runtime, 'terminal_handoffs': info['terminal_handoffs']}
                payload = standterm.build_external_agent_token_payload(
                    grant['token'], grant['record'], 'main', runtime['base_url'], runtime=runtime)
                self.assertNotIn('tls_ca_cert_path', info)
                self.assertNotIn('tls_ca_cert_path', payload['transport'])
                for command in payload['cli_commands'].values():
                    self.assertNotIn('--token', command)
                    self.assertNotIn('--ca-file', command)
                    self.assertIn(str(runtime['root']), command)


if __name__ == '__main__':
    unittest.main()
