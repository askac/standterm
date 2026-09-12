"""Dynamic SSH forwarding for the existing external-agent HTTP contract."""
import json
import hashlib
import secrets
import select
import re
import shlex
import socket
import threading
import time
from contextvars import ContextVar
from pathlib import Path, PurePosixPath

from flask import Flask, jsonify, request
from werkzeug.serving import WSGIRequestHandler, make_server


TUNNEL_IO_TIMEOUT = 30
TUNNEL_COMMAND_TIMEOUT = 45
TUNNEL_MAX_CHANNELS = 16
TUNNEL_MAX_REQUEST_BYTES = 1024 * 1024
TUNNEL_MONITOR_INTERVAL = 1
TUNNEL_FORWARD_POLL_SECONDS = 1
TUNNEL_REMOTE_PYTHON = 'python3'
TUNNEL_HELPERS = ('cli', 'input', 'jsonl', 'repl', 'scp', 'shcmd', 'type', 'rsfile', 'mcp', 'tunnel_runtime')
TUNNEL_SKILLS = ('standterm-external-agent-skill', 'standterm-file-transfer', 'standterm-privileged-hitl')
TUNNEL_SKILL_REFERENCES = ('connection.md', 'clients.md', 'terminal-workflows.md')
tunnel_ingress = ContextVar('standterm_agent_tunnel_ingress', default=None)


class QuietRequestHandler(WSGIRequestHandler):
    timeout = TUNNEL_IO_TIMEOUT

    def log(self, type, message, *args):
        pass


class AgentTunnel:
    def __init__(self, bridge, sid, app_dir, *, build_info, dispatch, revoke):
        self.bridge = bridge
        self.sid = sid
        self.app_dir = Path(app_dir)
        self.build_info = build_info
        self.dispatch = dispatch
        self.revoke = revoke
        self.id = 'tun_' + secrets.token_urlsafe(18)
        self.transport = bridge.ssh.get_transport()
        self.runtime = None
        self.port = None
        self.active = False
        self.ready = False
        self.verified_at = None
        self.grants = {}
        self.error = None
        self.lock = threading.RLock()
        self.setup_lock = threading.RLock()
        self._channels = set()
        self._setup_channels = set()
        self._files = set()
        self._directories = set()
        self._server = None
        self._sftp = None
        self._closed = threading.Event()
        self._cleanup_done = threading.Event()
        self._forward_pending = threading.Event()

    def _channel_call(self, channel, operation):
        completed = threading.Event()
        errors = []

        def run():
            try:
                operation()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        threading.Thread(target=run, daemon=True).start()
        if not completed.wait(TUNNEL_COMMAND_TIMEOUT):
            channel.close()
            raise RuntimeError('SSH provisioning request timed out.')
        if errors:
            raise errors[0]

    def _setup_channel(self):
        channel = self.transport.open_session(timeout=TUNNEL_IO_TIMEOUT)
        channel.settimeout(TUNNEL_IO_TIMEOUT)
        with self.lock:
            if self._closed.is_set():
                channel.close()
                raise RuntimeError('SSH tunnel setup was cancelled.')
            self._setup_channels.add(channel)
        return channel

    def _exec(self, command):
        channel = self._setup_channel()
        try:
            channel.settimeout(TUNNEL_IO_TIMEOUT)
            self._channel_call(channel, lambda: channel.exec_command(command))
            output = bytearray()
            deadline = time.monotonic() + TUNNEL_COMMAND_TIMEOUT
            while True:
                if channel.recv_ready():
                    output.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    channel.recv_stderr(65536)
                if len(output) > 65536:
                    raise RuntimeError('SSH provisioning response is too large.')
                if channel.exit_status_ready() and not channel.recv_ready():
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('SSH provisioning timed out.')
                time.sleep(0.01)
            try:
                payload = json.loads(output)
            except (ValueError, UnicodeError):
                raise RuntimeError('SSH provisioning requires Python 3.9+ and a clean exec channel.') from None
            if not isinstance(payload, dict) or channel.recv_exit_status() != 0:
                raise RuntimeError(payload.get('error', 'SSH provisioning failed.')
                                   if isinstance(payload, dict) else 'SSH provisioning failed.')
            return payload
        finally:
            channel.close()
            with self.lock:
                self._setup_channels.discard(channel)

    def _mkdir(self, path):
        if path in self._directories:
            return
        if path != self.runtime['root']:
            self._mkdir(path.parent)
            self._sftp.mkdir(str(path), mode=0o700)
        self._directories.add(path)

    def write_file(self, relative_path, data):
        if self._closed.is_set():
            raise RuntimeError('SSH tunnel setup was cancelled.')
        path = self.runtime['root'] / relative_path
        self._mkdir(path.parent)
        temporary = str(path) + '.' + secrets.token_hex(8) + '.tmp'
        self._files.add(PurePosixPath(temporary))
        with self._sftp.file(temporary, 'wx') as handle:
            self._sftp.chmod(temporary, 0o600)
            handle.write(data)
        # The directory is private and every destination is owned by this instance.
        if path in self._files:
            self._sftp.posix_rename(temporary, str(path))
        else:
            self._sftp.rename(temporary, str(path))
        self._files.discard(PurePosixPath(temporary))
        self._files.add(path)
        return str(path)

    def write_json(self, relative_path, payload):
        return self.write_file(relative_path, json.dumps(payload, indent=2).encode('utf-8'))

    def remove_file(self, relative_path):
        path = self.runtime['root'] / relative_path
        if path in self._files:
            self._sftp.remove(str(path))
            self._files.discard(path)

    def _provision(self):
        required = [self.app_dir / 'scripts' / ('agent_' + name + '.py') for name in TUNNEL_HELPERS]
        for directory in TUNNEL_SKILLS:
            required.extend(self.app_dir / 'docs/examples' / directory / name
                            for name in ('SKILL.md', 'boot_prompt.txt', 'skill_prompt.txt'))
        required.extend(self.app_dir / 'docs/examples/standterm-external-agent-skill/references' / name
                        for name in TUNNEL_SKILL_REFERENCES)
        if any(not path.is_file() for path in required):
            raise RuntimeError('The Core installation has an incomplete Agent helper or skill bundle.')
        bootstrap = (self.app_dir / 'scripts' / 'agent_tunnel_runtime.py').read_text(encoding='utf-8')
        result = self._exec(shlex.quote(TUNNEL_REMOTE_PYTHON) + ' -c ' + shlex.quote(bootstrap) + ' prepare')
        root = PurePosixPath(result['root'])
        python_path = result['python_path']
        if not root.is_absolute() or not PurePosixPath(python_path).is_absolute():
            raise RuntimeError('SSH provisioning returned invalid runtime paths.')
        self.runtime = {'root': root, 'python_path': python_path}
        sftp_channel = self._setup_channel()
        self._channel_call(sftp_channel, lambda: sftp_channel.invoke_subsystem('sftp'))
        self._sftp = self.bridge._get_paramiko().SFTPClient(sftp_channel)
        sources = [self.app_dir / 'scripts' / ('agent_' + name + '.py') for name in TUNNEL_HELPERS]
        for directory in TUNNEL_SKILLS:
            sources.extend(path for path in (self.app_dir / 'docs' / 'examples' / directory).rglob('*')
                           if path.is_file())
        manifest = {}
        for source in sources:
            relative = source.relative_to(self.app_dir).as_posix()
            data = source.read_bytes()
            self.write_file(relative, data)
            manifest[relative] = hashlib.sha256(data).hexdigest()
        self.write_json('manifest.json', manifest)

    def _http_app(self):
        adapter = Flask('standterm_agent_tunnel', static_folder=None)
        adapter.config['MAX_CONTENT_LENGTH'] = TUNNEL_MAX_REQUEST_BYTES

        @adapter.after_request
        def private_response(response):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response

        @adapter.get('/agentinfo')
        def info():
            if not self.ready or not self.active:
                return jsonify(status='starting', instance_id=self.id), 503
            return jsonify(self.build_info(self))

        @adapter.post('/agent/external/command')
        def command():
            if not self.ready or not self.active:
                return jsonify(status='failed', error_code='agent_external_disconnected'), 403
            context = tunnel_ingress.set(self.id)
            try:
                payload = self.dispatch(request.get_json(silent=True))
                return jsonify(payload), 400 if payload.get('status') == 'failed' else 200
            finally:
                tunnel_ingress.reset(context)

        return adapter

    def _accept(self, channel, origin, destination):
        with self.lock:
            if (not self.active or destination != ('127.0.0.1', self.port)
                    or len(self._channels) >= TUNNEL_MAX_CHANNELS):
                channel.close()
                return
            self._channels.add(channel)
        threading.Thread(target=self._forward, args=(channel,), daemon=True).start()

    def _forward(self, channel):
        local = None
        try:
            local = socket.create_connection(('127.0.0.1', self._server.server_port), TUNNEL_IO_TIMEOUT)
            local.settimeout(TUNNEL_IO_TIMEOUT)
            channel.settimeout(TUNNEL_IO_TIMEOUT)
            while self.active:
                readable, _, _ = select.select([channel, local], [], [], TUNNEL_FORWARD_POLL_SECONDS)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (local if source is channel else channel).sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            channel.close()
            if local:
                local.close()
            with self.lock:
                self._channels.discard(channel)

    def start(self):
        with self.setup_lock:
            self._start()

    def _start(self):
        try:
            if not self.transport or not self.transport.is_authenticated():
                raise RuntimeError('SSH is not authenticated.')
            self._provision()
            if self._closed.is_set():
                raise RuntimeError('SSH tunnel setup was cancelled.')
            self._server = make_server('127.0.0.1', 0, self._http_app(), threaded=True,
                                       request_handler=QuietRequestHandler)
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
            with self.lock:
                if self._closed.is_set():
                    raise RuntimeError('SSH tunnel setup was cancelled.')
                self.active = True
            self._request_forward()
            self.runtime['base_url'] = 'http://127.0.0.1:' + str(self.port)
            result = self._exec(' '.join(shlex.quote(part) for part in (
                self.runtime['python_path'], str(self.runtime['root'] / 'scripts/agent_tunnel_runtime.py'),
                'verify', str(self.port), self.id,
            )))
            if result.get('verified') is not True:
                raise RuntimeError('Remote loopback verification failed.')
            self.verified_at = time.time()
            if self._closed.is_set():
                raise RuntimeError('SSH tunnel setup was cancelled.')
            threading.Thread(target=self._monitor, daemon=True).start()
        except Exception:
            self.close()
            raise

    def check_connection(self):
        with self.setup_lock:
            if not self.ready or not self.active or not self.transport.is_active():
                raise RuntimeError('SSH tunnel is not ready.')
            instance_id = self.build_info(self)['instance_id']
            result = self._exec(' '.join(shlex.quote(part) for part in (
                self.runtime['python_path'], str(self.runtime['root'] / 'scripts/agent_tunnel_runtime.py'),
                'verify', str(self.port), instance_id,
            )))
            if result.get('verified') is not True or not self.active:
                raise RuntimeError('Remote loopback verification failed.')
            self.verified_at = time.time()

    def _monitor(self):
        while not self._closed.wait(TUNNEL_MONITOR_INTERVAL):
            if not self.transport.is_active() or self.bridge.closing:
                self.close()
                return

    def _cancel_forward(self):
        if self.port and self.transport.is_active():
            # A cancel acknowledgement is not needed to fence access locally.
            self.transport.global_request('cancel-tcpip-forward', ('127.0.0.1', self.port), wait=False)

    def _request_forward(self):
        completed = threading.Event()
        errors = []
        self._forward_pending.set()

        def run():
            try:
                self.port = self.transport.request_port_forward('127.0.0.1', 0, handler=self._accept)
                if self._closed.is_set():
                    self._cancel_forward()
            except Exception as exc:
                errors.append(exc)
            finally:
                self._forward_pending.clear()
                completed.set()

        threading.Thread(target=run, daemon=True).start()
        if not completed.wait(TUNNEL_COMMAND_TIMEOUT):
            raise RuntimeError('SSH forwarding request timed out. Reconnect SSH if the server does not reply.')
        if errors:
            raise errors[0]
        if self._closed.is_set():
            raise RuntimeError('SSH tunnel setup was cancelled.')

    def close(self):
        with self.lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self.ready = False
            self.active = False
            channels = list(self._channels)
        self.revoke(self)
        for channel in channels:
            channel.close()
        with self.lock:
            setup_channels = list(self._setup_channels)
        for channel in setup_channels:
            if not self._sftp or channel is not self._sftp.get_channel():
                channel.close()
        threading.Thread(target=self._finish_close, daemon=True).start()

    def _finish_close(self):
        try:
            with self.setup_lock:
                self._cleanup()
        finally:
            self._cleanup_done.set()

    def _cleanup(self):
        if self.port and self.transport.is_active():
            try:
                self._cancel_forward()
            except (OSError, EOFError):
                pass
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._sftp:
            try:
                if not self.transport.is_active():
                    return
                cache = self.runtime['root'] / 'scripts/__pycache__'
                modules = {path.stem for path in self._files if path.suffix == '.py'}
                try:
                    for name in self._sftp.listdir(str(cache)):
                        if any(re.fullmatch(re.escape(module) + r'\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc', name)
                               for module in modules):
                            self._sftp.remove(str(cache / name))
                    self._sftp.rmdir(str(cache))
                except OSError:
                    pass
                for path in list(self._files):
                    try:
                        self._sftp.remove(str(path))
                    except OSError:
                        if not self.transport.is_active():
                            return
                for path in sorted(self._directories, key=lambda value: len(value.parts), reverse=True):
                    try:
                        self._sftp.rmdir(str(path))
                    except OSError:
                        if not self.transport.is_active():
                            return
            except (OSError, EOFError):
                pass
            finally:
                self._sftp.close()
