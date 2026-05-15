import os
import sys
import subprocess
import base64
import getpass
import logging
import secrets
import socket
import time
import argparse
import select
import shutil
import re
from collections import deque
from pathlib import Path
from flask import Flask, render_template, request, abort, make_response, redirect
from flask_socketio import SocketIO

try:
    from ptyprocess import PtyProcessUnicode
except Exception:
    PtyProcessUnicode = None

try:
    from winpty import PtyProcess as WinPtyProcess
except Exception:
    WinPtyProcess = None

paramiko = None

ASYNC_MODE = os.getenv('WEBSSH_ASYNC_MODE', '').strip().lower()
if not ASYNC_MODE:
    ASYNC_MODE = 'threading'

if ASYNC_MODE == 'eventlet':
    try:
        import eventlet
        eventlet.monkey_patch()
    except Exception as exc:
        print(f"[!] Eventlet unavailable ({exc}); falling back to threading.", file=sys.stderr)
        ASYNC_MODE = 'threading'

app = Flask(__name__, static_url_path='/static', static_folder='static')
app.config['SECRET_KEY'] = secrets.token_hex(16)
ACCESS_TOKEN = secrets.token_urlsafe(16)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Default to threading for consistent cross-platform behavior.
socketio = SocketIO(app, async_mode=ASYNC_MODE, logger=False, engineio_logger=False)

# SSH Configuration
SSH_HOST = '127.0.0.1'
SSH_PORT = 22
SSH_USER = os.getenv('USER', 'aska')
DEFAULT_BIND_HOST = os.getenv('WEBSSH_HOST', '').strip()
DEFAULT_PORT = int(os.getenv('WEBSSH_PORT', '5000'))
SSH_TERM = 'xterm-256color'
MAX_SSH_INPUT_BYTES = 65536
MAX_PASSWORD_BYTES = 4096
MAX_HOST_LENGTH = 255
MAX_USERNAME_LENGTH = 128
SESSION_COOKIE_NAME = 'webssh_session'
SESSION_COOKIE_MAX_AGE = 12 * 60 * 60
SESSION_CLEANUP_INTERVAL_SECONDS = 60
LOCALHOST_KEY_SETUP_TTL_SECONDS = 120
MIN_TERMINAL_COLS = 2
MAX_TERMINAL_COLS = 500
MIN_TERMINAL_ROWS = 2
MAX_TERMINAL_ROWS = 500
CONNECTION_TYPE_SSH = 'ssh'
CONNECTION_TYPE_LOCAL_SHELL = 'local_shell'
TERMINAL_ID_MAIN = 'main'
MAX_TERMINAL_ID_LENGTH = 64
MAX_TERMINALS_PER_CLIENT = 12
MAX_TERMINAL_REPLAY_EVENTS = 1000
MAX_TERMINAL_REPLAY_BYTES = 200000
TERMINAL_ID_PATTERN = re.compile(r'^[A-Za-z0-9_.:-]+$')
LOCAL_PUBLIC_KEY_TYPES = {
    'ssh-ed25519',
    'ssh-rsa',
    'ssh-dss',
    'ecdsa-sha2-nistp256',
    'ecdsa-sha2-nistp384',
    'ecdsa-sha2-nistp521',
    'sk-ssh-ed25519@openssh.com',
    'sk-ecdsa-sha2-nistp256@openssh.com',
}

def normalize_connection_type(value):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace('-', '_')
    if normalized in {CONNECTION_TYPE_SSH, CONNECTION_TYPE_LOCAL_SHELL}:
        return normalized
    return None

def connection_type_cli_value(value):
    normalized = normalize_connection_type(value)
    if not normalized:
        raise argparse.ArgumentTypeError('expected ssh or local-shell')
    return normalized

def parse_cli_args(argv):
    parser = argparse.ArgumentParser(description='WebSSH server')
    parser.add_argument(
        '--default-connection',
        type=connection_type_cli_value,
        default=CONNECTION_TYPE_SSH,
        metavar='ssh|local-shell',
        help='Default connection mode shown in the UI.',
    )
    parser.add_argument(
        '--force-connection',
        type=connection_type_cli_value,
        default=None,
        metavar='ssh|local-shell',
        help='Force one connection mode in both the UI and backend.',
    )
    parser.add_argument(
        '--force',
        '-f',
        action='store_true',
        help='Accepted for run script compatibility; dependency checks are handled by the launcher.',
    )
    return parser.parse_args(argv)

CLI_ARGS = parse_cli_args(sys.argv[1:])
DEFAULT_CONNECTION_TYPE = CLI_ARGS.default_connection
FORCE_CONNECTION_TYPE = CLI_ARGS.force_connection

def build_terminal_policy():
    return {
        'default_connection': DEFAULT_CONNECTION_TYPE,
        'force_connection': FORCE_CONNECTION_TYPE,
    }

def build_terminal_metadata(connection_type, terminal_id, terminal_kind, terminal_label, cols, rows):
    return {
        'connection_type': connection_type,
        'terminal_id': terminal_id,
        'terminal_kind': terminal_kind,
        'terminal_label': terminal_label,
        'term': SSH_TERM,
        'cols': cols,
        'rows': rows,
    }

def get_request_client_ip():
    return request.remote_addr or request.environ.get('REMOTE_ADDR') or 'unknown'

def get_paramiko():
    global paramiko
    if paramiko is None:
        import paramiko as paramiko_module
        paramiko = paramiko_module
    return paramiko

def get_shell_kind(shell_path):
    shell_name = Path(shell_path).name.lower()
    if shell_name in {'bash', 'zsh', 'sh', 'fish', 'dash', 'ksh'}:
        return shell_name
    return 'shell'

def get_shell_label(shell_kind):
    labels = {
        'bash': 'bash',
        'zsh': 'zsh',
        'sh': 'sh',
        'fish': 'fish',
        'dash': 'dash',
        'ksh': 'ksh',
        'powershell': 'PowerShell',
        'pwsh': 'PowerShell',
        'cmd': 'cmd',
        'shell': 'Shell',
    }
    return labels.get(shell_kind, 'Shell')

def get_windows_shell_path():
    for shell_name in ('pwsh.exe', 'powershell.exe', 'cmd.exe'):
        shell_path = shutil.which(shell_name)
        if shell_path:
            return shell_path
    return 'cmd.exe'

def get_windows_shell_kind(shell_path):
    shell_name = Path(shell_path).name.lower()
    if shell_name == 'pwsh.exe':
        return 'pwsh'
    if shell_name == 'powershell.exe':
        return 'powershell'
    if shell_name == 'cmd.exe':
        return 'cmd'
    return 'shell'

class TerminalBridge:
    connection_type = None
    terminal_kind = None
    terminal_label = None

    def __init__(self, owner_session, terminal_id):
        self.owner_session = owner_session
        self.terminal_id = terminal_id
        self.attached_sids = set()
        self.sid = None
        self.closing = False
        self.replay_buffer = deque()
        self.replay_buffer_bytes = 0

    def metadata(self, cols=80, rows=24):
        return build_terminal_metadata(
            self.connection_type,
            self.terminal_id,
            self.terminal_kind,
            self.terminal_label,
            cols,
            rows,
        )

    def attach(self, sid):
        self.sid = sid
        self.attached_sids.add(sid)

    def detach(self, sid):
        self.attached_sids.discard(sid)
        if self.sid == sid:
            self.sid = next(iter(self.attached_sids), None)

    def emit_output(self, payload):
        payload = dict(payload)
        payload.setdefault('connection_type', self.connection_type)
        payload.setdefault('terminal_id', self.terminal_id)
        if payload.get('message_type') == 'terminal':
            self._remember_terminal_payload(payload)
        for sid in list(self.attached_sids):
            socketio.emit('ssh_output', payload, room=sid)

    def _remember_terminal_payload(self, payload):
        data = payload.get('data')
        if not isinstance(data, str) or not data:
            return
        payload_size = len(data.encode('utf-8', errors='ignore'))
        self.replay_buffer.append(dict(payload))
        self.replay_buffer_bytes += payload_size
        while (
            len(self.replay_buffer) > MAX_TERMINAL_REPLAY_EVENTS
            or self.replay_buffer_bytes > MAX_TERMINAL_REPLAY_BYTES
        ):
            removed = self.replay_buffer.popleft()
            removed_data = removed.get('data', '')
            self.replay_buffer_bytes -= len(removed_data.encode('utf-8', errors='ignore'))

    def replay_to(self, sid):
        for payload in list(self.replay_buffer):
            socketio.emit('ssh_output', payload, room=sid)

    def read_loop(self):
        raise NotImplementedError

    def write(self, data):
        raise NotImplementedError

    def resize(self, cols, rows):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

class SSHBridge(TerminalBridge):
    connection_type = CONNECTION_TYPE_SSH
    terminal_kind = 'ssh'
    terminal_label = 'SSH'

    def __init__(self, sid, terminal_id=TERMINAL_ID_MAIN):
        super().__init__(sid, terminal_id)
        self.ssh = None
        self._reset_ssh_client()
        self.channel = None

    def _reset_ssh_client(self, trust_unknown_host=False):
        paramiko_module = get_paramiko()
        if self.ssh:
            self.ssh.close()
        self.ssh = paramiko_module.SSHClient()
        self.ssh.load_system_host_keys()
        if trust_unknown_host:
            self.ssh.set_missing_host_key_policy(paramiko_module.AutoAddPolicy())
        else:
            self.ssh.set_missing_host_key_policy(paramiko_module.RejectPolicy())

    @staticmethod
    def _is_local_target(host):
        if not host:
            return False
        normalized = host.strip().lower()
        return normalized in {'127.0.0.1', 'localhost', '::1'}

    @staticmethod
    def _iter_local_private_key_files():
        ssh_dir = Path.home() / '.ssh'
        key_names = (
            'id_ed25519',
            'id_ecdsa',
            'id_rsa',
            'id_dsa',
            'id_ed25519_sk',
            'id_ecdsa_sk',
        )
        for key_name in key_names:
            key_path = ssh_dir / key_name
            if key_path.is_file():
                yield key_path

    @staticmethod
    def _iter_local_public_key_files():
        ssh_dir = Path.home() / '.ssh'
        key_names = (
            'id_ed25519.pub',
            'id_ecdsa.pub',
            'id_rsa.pub',
            'id_dsa.pub',
            'id_ed25519_sk.pub',
            'id_ecdsa_sk.pub',
        )
        for key_name in key_names:
            key_path = ssh_dir / key_name
            if key_path.is_file():
                yield key_path

    @staticmethod
    def _parse_public_key_line(line):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            return None

        parts = stripped.split()
        for index in range(len(parts) - 1):
            key_type = parts[index]
            key_body = parts[index + 1]
            if key_type not in LOCAL_PUBLIC_KEY_TYPES:
                continue
            try:
                base64.b64decode(key_body.encode('ascii'), validate=True)
            except Exception:
                continue
            return {
                'key_type': key_type,
                'key_body': key_body,
                'line': stripped,
            }
        return None

    def _get_local_public_key_entries(self):
        entries = []
        for key_path in self._iter_local_public_key_files():
            try:
                line = key_path.read_text(encoding='utf-8').strip()
            except OSError:
                continue
            parsed = self._parse_public_key_line(line)
            if parsed:
                parsed['path'] = key_path
                entries.append(parsed)
        return entries

    def _get_authorized_keys_path(self):
        return Path.home() / '.ssh' / 'authorized_keys'

    def _read_authorized_key_fingerprints(self):
        authorized_keys_path = self._get_authorized_keys_path()
        fingerprints = set()
        if not authorized_keys_path.is_file():
            return fingerprints

        try:
            lines = authorized_keys_path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return fingerprints

        for line in lines:
            parsed = self._parse_public_key_line(line)
            if parsed:
                fingerprints.add((parsed['key_type'], parsed['key_body']))
        return fingerprints

    def _get_missing_local_public_keys(self):
        authorized_fingerprints = self._read_authorized_key_fingerprints()
        missing_entries = []
        for entry in self._get_local_public_key_entries():
            fingerprint = (entry['key_type'], entry['key_body'])
            if fingerprint not in authorized_fingerprints:
                missing_entries.append(entry)
        return missing_entries

    def _can_offer_local_key_setup(self, user):
        availability = self._get_local_key_setup_availability(user)
        return availability['can_offer']

    def _get_local_key_setup_availability(self, user):
        current_user = getpass.getuser()
        if user != current_user:
            return {
                'can_offer': False,
                'reason': 'Automatic localhost key setup is only available for the current local user.',
                'error_code': 'localhost_key_setup_unsupported_user',
            }

        if os.name == 'nt':
            return {
                'can_offer': False,
                'reason': (
                    'Automatic localhost key setup is not supported on native Windows yet. '
                    'Windows OpenSSH may require a different authorized keys file, such as '
                    '%USERPROFILE%\\.ssh\\authorized_keys for a regular user or '
                    'C:\\ProgramData\\ssh\\administrators_authorized_keys for an administrator '
                    'account. Please add your public key manually, then try again.'
                ),
                'error_code': 'localhost_key_setup_unsupported_windows',
            }

        return {'can_offer': True}

    def _append_public_key_entry_to_authorized_keys(self, entry):
        fingerprint = (entry['key_type'], entry['key_body'])
        if fingerprint in self._read_authorized_key_fingerprints():
            return False, {
                'status': 'already_configured',
                'message': 'Your local public key is already present in ~/.ssh/authorized_keys.',
            }

        ssh_dir = Path.home() / '.ssh'
        authorized_keys_path = self._get_authorized_keys_path()

        try:
            ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(ssh_dir, 0o700)

            existing_text = ''
            if authorized_keys_path.exists():
                existing_text = authorized_keys_path.read_text(encoding='utf-8')

            with authorized_keys_path.open('a', encoding='utf-8') as authorized_keys_file:
                if existing_text and not existing_text.endswith('\n'):
                    authorized_keys_file.write('\n')
                authorized_keys_file.write(entry['line'] + '\n')
            os.chmod(authorized_keys_path, 0o600)
        except OSError as exc:
            return False, {
                'status': 'failed',
                'message': f'Failed to update ~/.ssh/authorized_keys: {exc}',
            }

        return True, {
            'status': 'success',
            'message': (
                f'Added {entry["path"].name} to ~/.ssh/authorized_keys. '
                'Try connecting to localhost again.'
            ),
        }

    def _append_local_public_key_to_authorized_keys(self):
        missing_entries = self._get_missing_local_public_keys()
        if not missing_entries:
            return False, {
                'status': 'already_configured',
                'message': 'Your local public key is already present in ~/.ssh/authorized_keys.',
            }

        return self._append_public_key_entry_to_authorized_keys(missing_entries[0])

    def _build_local_key_setup_hint(self):
        message = (
            'Local public key authentication for localhost failed, and your local public key was not '
            'found in ~/.ssh/authorized_keys on this machine. Add your public key to '
            '~/.ssh/authorized_keys, or enter your SSH password and try again.'
        )
        question = (
            'Do you want to add your public key to ~/.ssh/authorized_keys?'
        )
        return {
            'message': message,
            'error_code': 'localhost_key_not_authorized',
            'action_type': 'offer_localhost_key_setup',
            'action_message': message,
            'action_question': question,
        }

    @staticmethod
    def _build_manual_local_key_setup_hint(reason, error_code):
        return {
            'message': reason,
            'error_code': error_code,
        }

    @staticmethod
    def _load_private_key(key_path, passphrase=None):
        paramiko_module = get_paramiko()
        key_types = []
        for key_type_name in ('Ed25519Key', 'ECDSAKey', 'RSAKey', 'DSSKey'):
            key_type = getattr(paramiko_module, key_type_name, None)
            if key_type is not None:
                key_types.append(key_type)
        last_error = None
        for key_type in key_types:
            try:
                return key_type.from_private_key_file(str(key_path), password=passphrase)
            except paramiko_module.PasswordRequiredException:
                raise
            except paramiko_module.SSHException as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise paramiko_module.SSHException(f"Unsupported key format: {key_path}")

    def _connect_with_local_keys(self, host, port, user, password):
        paramiko_module = get_paramiko()
        auth_errors = []
        passphrase = password or None

        try:
            self._reset_ssh_client(trust_unknown_host=True)
            self.ssh.connect(
                host,
                port=int(port),
                username=user,
                password=None,
                timeout=15,
                allow_agent=True,
                look_for_keys=True,
            )
            print(f"[+] Local key auth succeeded via agent/default keys for {self.sid}")
            return True, None
        except paramiko_module.AuthenticationException as exc:
            auth_errors.append(f"agent/default keys: {exc}")
        except Exception as exc:
            auth_errors.append(f"agent/default keys: {exc}")

        for key_path in self._iter_local_private_key_files():
            try:
                pkey = self._load_private_key(key_path, passphrase=passphrase)
            except paramiko_module.PasswordRequiredException:
                auth_errors.append(f"{key_path.name}: passphrase required")
                continue
            except Exception as exc:
                auth_errors.append(f"{key_path.name}: {exc}")
                continue

            try:
                self._reset_ssh_client(trust_unknown_host=True)
                self.ssh.connect(
                    host,
                    port=int(port),
                    username=user,
                    password=None,
                    pkey=pkey,
                    timeout=15,
                    allow_agent=False,
                    look_for_keys=False,
                )
                print(f"[+] Local key auth succeeded via {key_path.name} for {self.sid}")
                return True, None
            except Exception as exc:
                auth_errors.append(f"{key_path.name}: {exc}")

        return False, '; '.join(auth_errors)

    def connect(self, host, port, user, password=None, cols=80, rows=24):
        paramiko_module = get_paramiko()
        try:
            pwd = password if password else ""
            print(f"[*] Attempting SSH connection for {user!r} at {host!r}:{port}...")

            is_localhost = self._is_local_target(host)
            if is_localhost and not pwd:
                success, key_error = self._connect_with_local_keys(host, port, user, None)
                if not success:
                    setup_availability = self._get_local_key_setup_availability(user)
                    if setup_availability['can_offer']:
                        missing_local_keys = self._get_missing_local_public_keys()
                        if missing_local_keys:
                            hint = self._build_local_key_setup_hint()
                            print(f"[*] Local key auth failed for {self.sid}; offering localhost key setup.")
                            return False, hint
                    elif setup_availability.get('reason'):
                        print(f"[*] Local key auth failed for {self.sid}; auto setup unavailable.")
                        return False, self._build_manual_local_key_setup_hint(
                            setup_availability['reason'],
                            setup_availability.get('error_code'),
                        )
                    raise paramiko_module.AuthenticationException(
                        f"Local public key auth failed: {key_error or 'no usable local key found'}"
                    )
            else:
                self._reset_ssh_client(trust_unknown_host=is_localhost)
                self.ssh.connect(
                    host,
                    port=int(port),
                    username=user,
                    password=pwd,
                    timeout=15,
                    allow_agent=False,
                    look_for_keys=False,
                )

            self.channel = self.ssh.invoke_shell(term=SSH_TERM, width=cols, height=rows)
            self.channel.setblocking(0)
            print(f"[+] SSH connection established for {self.sid}")
            return True, None
        except Exception as e:
            error_msg = str(e)
            print(f"[!] SSH Connection Error: {error_msg}")
            return False, {'message': error_msg}

    def read_loop(self):
        print(f"[*] Starting SSH read loop for {self.sid}")
        while True:
            # Short sleep to prevent CPU hogging while allowing high responsiveness
            socketio.sleep(0.01)
            if not self.channel:
                break
            
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096).decode('utf-8', errors='ignore')
                    if data:
                        self.emit_output({
                            'message_type': 'terminal',
                            'data': data,
                        })
                
                if self.channel.exit_status_ready():
                    print(f"[*] SSH session exited for {self.sid}")
                    self.emit_output({
                        'message_type': 'ssh_closed',
                        'message': 'SSH session closed.',
                    })
                    break
            except Exception as e:
                if self.closing:
                    break
                print(f"[!] Read error: {e}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'SSH connection closed due to a read error.',
                    'error_code': 'ssh_read_error',
                })
                break
        print(f"[*] SSH read loop terminated for {self.sid}")
        unregister_terminal_bridge(self.owner_session, self.terminal_id, self)

    def write(self, data):
        if self.channel:
            try:
                self.channel.send(data)
            except Exception as e:
                print(f"[!] Write error: {e}")

    def resize(self, cols, rows):
        if self.channel:
            try:
                self.channel.resize_pty(width=cols, height=rows)
            except Exception as e:
                print(f"[!] Resize error: {e}")

    def close(self):
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
            self.channel = None
        if self.ssh:
            try:
                self.ssh.close()
            except Exception:
                pass

class LocalShellBridge(TerminalBridge):
    connection_type = CONNECTION_TYPE_LOCAL_SHELL

    def __init__(self, sid, terminal_id=TERMINAL_ID_MAIN):
        super().__init__(sid, terminal_id)
        self.process = None
        shell = get_windows_shell_path() if sys.platform.startswith('win') else (os.environ.get('SHELL') or '/bin/sh')
        self.shell = shell
        self.terminal_kind = get_windows_shell_kind(shell) if sys.platform.startswith('win') else get_shell_kind(shell)
        self.terminal_label = get_shell_label(self.terminal_kind)

    def connect(self, cols=80, rows=24):
        if sys.platform.startswith('win'):
            return self._connect_windows(cols, rows)
        if PtyProcessUnicode is None:
            return False, {
                'message': 'Local Shell requires ptyprocess. Re-run the launcher with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = dict(os.environ)
            env['TERM'] = SSH_TERM
            cwd = str(Path.home())
            self.process = PtyProcessUnicode.spawn(
                [self.shell],
                cwd=cwd,
                env=env,
                dimensions=(rows, cols),
            )
            print(f"[+] Local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            print(f"[!] Local shell start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'local_shell_start_failed'}

    def _connect_windows(self, cols, rows):
        if WinPtyProcess is None:
            return False, {
                'message': 'Local Shell on Windows requires pywinpty. Re-run run.bat with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = dict(os.environ)
            env['TERM'] = SSH_TERM
            cwd = str(Path.home())
            self.process = self._spawn_windows_process(cols, rows, cwd, env)
            self.resize(cols, rows)
            print(f"[+] Windows local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            print(f"[!] Windows local shell start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'local_shell_start_failed'}

    def _spawn_windows_process(self, cols, rows, cwd, env):
        spawn_attempts = (
            lambda: WinPtyProcess.spawn(self.shell, cwd=cwd, env=env, dimensions=(rows, cols)),
            lambda: WinPtyProcess.spawn(self.shell, cwd=cwd, env=env),
            lambda: WinPtyProcess.spawn(self.shell, dimensions=(rows, cols)),
            lambda: WinPtyProcess.spawn(self.shell),
        )
        last_error = None
        for spawn in spawn_attempts:
            try:
                return spawn()
            except TypeError as exc:
                last_error = exc
        raise last_error

    def read_loop(self):
        print(f"[*] Starting local shell read loop for {self.sid}")
        while True:
            socketio.sleep(0.01)
            if not self.process:
                break

            if sys.platform.startswith('win'):
                if not self._read_windows_once():
                    break
                continue

            try:
                readable, _, _ = select.select([self.process.fd], [], [], 0)
                if not readable:
                    if self.closing:
                        break
                    if not self.process.isalive():
                        self.emit_output({
                            'message_type': 'ssh_closed',
                            'message': 'Local shell session closed.',
                        })
                        break
                    continue

                data = self.process.read(size=4096)
                if data:
                    self.emit_output({
                        'message_type': 'terminal',
                        'data': data,
                    })
            except EOFError:
                if self.closing:
                    break
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell session closed.',
                })
                break
            except Exception as exc:
                if self.closing:
                    break
                print(f"[!] Local shell read error: {exc}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell closed due to a read error.',
                    'error_code': 'local_shell_read_error',
                })
                break

        print(f"[*] Local shell read loop terminated for {self.sid}")
        unregister_terminal_bridge(self.owner_session, self.terminal_id, self)

    def _read_windows_once(self):
        try:
            data = self.process.read(4096)
            if data:
                self.emit_output({
                    'message_type': 'terminal',
                    'data': data,
                })
            if not self.process.isalive():
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell session closed.',
                })
                return False
            return True
        except EOFError:
            if self.closing:
                return False
            self.emit_output({
                'message_type': 'ssh_closed',
                'message': 'Local shell session closed.',
            })
            return False
        except Exception as exc:
            if self.closing:
                return False
            print(f"[!] Windows local shell read error: {exc}")
            self.emit_output({
                'message_type': 'ssh_closed',
                'message': 'Local shell closed due to a read error.',
                'error_code': 'local_shell_read_error',
            })
            return False

    def write(self, data):
        if self.process:
            try:
                self.process.write(data)
            except Exception as exc:
                print(f"[!] Local shell write error: {exc}")

    def resize(self, cols, rows):
        if self.process:
            try:
                if sys.platform.startswith('win'):
                    if hasattr(self.process, 'set_size'):
                        self.process.set_size(cols, rows)
                    elif hasattr(self.process, 'setwinsize'):
                        self.process.setwinsize(rows, cols)
                    elif hasattr(self.process, 'resize'):
                        self.process.resize(cols, rows)
                else:
                    self.process.setwinsize(rows, cols)
            except Exception as exc:
                print(f"[!] Local shell resize error: {exc}")

    def close(self):
        if not self.process:
            return
        try:
            if sys.platform.startswith('win') and hasattr(self.process, 'terminate'):
                self.process.terminate()
            elif sys.platform.startswith('win') and hasattr(self.process, 'kill'):
                self.process.kill()
            else:
                self.process.close(force=True)
        except TypeError:
            self.process.close()
        except Exception:
            pass
        self.process = None

bridges = {}
pending_localhost_key_setups = {}
active_sessions = {}
socket_session_tokens = {}
socket_client_ips = {}
session_cleanup_task_started = False

def is_valid_access_token(token):
    if not isinstance(token, str):
        return False
    return secrets.compare_digest(token.strip(), ACCESS_TOKEN)

def is_valid_session(session_token):
    if not isinstance(session_token, str):
        return False
    expires_at = active_sessions.get(session_token)
    if not expires_at:
        return False
    if time.time() > expires_at:
        active_sessions.pop(session_token, None)
        close_all_terminal_bridges(session_token)
        return False
    return True

def cleanup_expired_sessions():
    now = time.time()
    expired_tokens = [
        session_token
        for session_token, expires_at in list(active_sessions.items())
        if now > expires_at
    ]
    for session_token in expired_tokens:
        active_sessions.pop(session_token, None)
        close_all_terminal_bridges(session_token)
        for sid, sid_session_token in list(socket_session_tokens.items()):
            if sid_session_token == session_token:
                socket_session_tokens.pop(sid, None)
                socket_client_ips.pop(sid, None)

def session_cleanup_loop():
    while True:
        socketio.sleep(SESSION_CLEANUP_INTERVAL_SECONDS)
        cleanup_expired_sessions()

def ensure_session_cleanup_task():
    global session_cleanup_task_started
    if session_cleanup_task_started:
        return
    session_cleanup_task_started = True
    socketio.start_background_task(target=session_cleanup_loop)

def get_request_session_token():
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not is_valid_session(session_token):
        return None
    return session_token

def has_control_chars(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)

def add_common_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

def close_bridge(bridge):
    if not bridge:
        return
    bridge.closing = True
    bridge.close()

def get_bridge(session_token, terminal_id):
    return bridges.get(session_token, {}).get(terminal_id)

def set_bridge(session_token, terminal_id, bridge):
    bridges.setdefault(session_token, {})[terminal_id] = bridge

def pop_bridge(session_token, terminal_id):
    terminals = bridges.get(session_token)
    if not terminals:
        return None
    bridge = terminals.pop(terminal_id, None)
    if not terminals:
        bridges.pop(session_token, None)
    return bridge

def unregister_terminal_bridge(session_token, terminal_id, bridge):
    if get_bridge(session_token, terminal_id) is not bridge:
        return
    pop_bridge(session_token, terminal_id)
    close_bridge(bridge)

def close_terminal_bridge(session_token, terminal_id):
    close_bridge(pop_bridge(session_token, terminal_id))

def close_all_terminal_bridges(session_token):
    terminals = bridges.pop(session_token, {})
    for bridge in list(terminals.values()):
        close_bridge(bridge)

def detach_session_bridges(session_token, sid):
    for bridge in bridges.get(session_token, {}).values():
        bridge.detach(sid)

def get_session_sids(session_token):
    return [
        sid
        for sid, sid_session_token in socket_session_tokens.items()
        if sid_session_token == session_token
    ]

def build_terminal_list(session_token):
    terminals = []
    for terminal_id, bridge in sorted(bridges.get(session_token, {}).items()):
        terminal_info = {
            'terminal_id': terminal_id,
            'connection_type': bridge.connection_type,
            'terminal_kind': bridge.terminal_kind,
            'terminal_label': bridge.terminal_label,
            'term': SSH_TERM,
            'connected': True,
            'buffered_events': len(bridge.replay_buffer),
        }
        terminals.append(terminal_info)
    return terminals

def is_valid_terminal_id(terminal_id):
    if not isinstance(terminal_id, str):
        return False
    if not terminal_id or len(terminal_id) > MAX_TERMINAL_ID_LENGTH:
        return False
    return bool(TERMINAL_ID_PATTERN.fullmatch(terminal_id))

def validate_terminal_id_payload(data, default=None):
    if not isinstance(data, dict):
        return None
    terminal_id = data.get('terminal_id', default)
    if not is_valid_terminal_id(terminal_id):
        return None
    return terminal_id

def emit_connection_error(sid, message, error_code=None, action_type=None, action_message=None,
                          action_question=None, action_id=None, terminal_id=TERMINAL_ID_MAIN):
    socketio.emit(
        'ssh_output',
        {
            'message_type': 'connection_error',
            'terminal_id': terminal_id,
            'message': message,
            'error_code': error_code,
            'action_type': action_type,
            'action_message': action_message,
            'action_question': action_question,
            'action_id': action_id,
        },
        room=sid,
    )

def validate_start_ssh_payload(data):
    if not isinstance(data, dict):
        return None, 'Invalid connection payload.'

    connection_type = normalize_connection_type(data.get('connection_type', DEFAULT_CONNECTION_TYPE))
    if not connection_type:
        return None, 'Connection type must be ssh or local_shell.'
    if FORCE_CONNECTION_TYPE and connection_type != FORCE_CONNECTION_TYPE:
        return None, f'Connection type is locked to {FORCE_CONNECTION_TYPE}.'

    terminal_id = validate_terminal_id_payload(data, default=TERMINAL_ID_MAIN)
    if not terminal_id:
        return None, 'Invalid terminal id.'

    if connection_type == CONNECTION_TYPE_LOCAL_SHELL:
        return {
            'connection_type': connection_type,
            'terminal_id': terminal_id,
        }, None

    host = data.get('host', SSH_HOST)
    if not isinstance(host, str):
        return None, 'Host must be a string.'
    host = host.strip()
    if not host or len(host) > MAX_HOST_LENGTH:
        return None, 'Host is empty or too long.'
    if has_control_chars(host):
        return None, 'Host contains invalid control characters.'

    try:
        port = int(data.get('port', SSH_PORT))
    except (TypeError, ValueError):
        return None, 'Port must be a number.'
    if port < 1 or port > 65535:
        return None, 'Port must be between 1 and 65535.'

    user = data.get('username', SSH_USER)
    if not isinstance(user, str):
        return None, 'Username must be a string.'
    user = user.strip()
    if not user or len(user) > MAX_USERNAME_LENGTH:
        return None, 'Username is empty or too long.'
    if has_control_chars(user):
        return None, 'Username contains invalid control characters.'

    password = data.get('password') or ''
    if not isinstance(password, str):
        return None, 'Password must be a string.'
    if len(password.encode('utf-8', errors='ignore')) > MAX_PASSWORD_BYTES:
        return None, 'Password is too long.'

    return {
        'connection_type': connection_type,
        'terminal_id': terminal_id,
        'host': host,
        'port': port,
        'username': user,
        'password': password,
    }, None

def parse_terminal_size(data):
    if not isinstance(data, dict):
        return None
    try:
        cols = int(data.get('cols'))
        rows = int(data.get('rows'))
    except (TypeError, ValueError):
        return None
    if cols < MIN_TERMINAL_COLS or cols > MAX_TERMINAL_COLS:
        return None
    if rows < MIN_TERMINAL_ROWS or rows > MAX_TERMINAL_ROWS:
        return None
    return cols, rows

def build_session_response():
    ensure_session_cleanup_task()
    session_token = secrets.token_urlsafe(32)
    active_sessions[session_token] = time.time() + SESSION_COOKIE_MAX_AGE
    response = redirect('/')
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite='Strict',
    )
    return add_common_headers(response)

def get_pending_localhost_key_setup(sid, action_id):
    pending_setup = pending_localhost_key_setups.get(sid)
    if not pending_setup:
        return None, 'localhost_key_setup_no_pending_action'
    if time.time() > pending_setup['expires_at']:
        pending_localhost_key_setups.pop(sid, None)
        return None, 'localhost_key_setup_expired'
    if not isinstance(action_id, str) or not secrets.compare_digest(action_id, pending_setup['action_id']):
        return None, 'localhost_key_setup_no_pending_action'
    return pending_setup, None

@app.route('/')
def index():
    token = request.args.get('token')
    if is_valid_access_token(token):
        return build_session_response()

    if not is_valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return abort(403, description="Invalid or missing access token.")

    response = make_response(render_template(
        'index.html',
        ssh_term=SSH_TERM,
        terminal_policy=build_terminal_policy(),
    ))
    return add_common_headers(response)

@socketio.on('connect')
def on_connect():
    ensure_session_cleanup_task()
    cleanup_expired_sessions()
    session_token = get_request_session_token()
    client_ip = get_request_client_ip()
    if not session_token:
        print(f"[!] Unauthorized WebSocket attempt: {request.sid} from {client_ip}")
        return False 
    socket_session_tokens[request.sid] = session_token
    socket_client_ips[request.sid] = client_ip
    print(f"[+] Client connected: {request.sid} from {client_ip}")

@socketio.on('list_terminals')
def on_list_terminals():
    cleanup_expired_sessions()
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    socketio.emit(
        'terminal_list',
        {
            'terminals': build_terminal_list(session_token),
        },
        room=request.sid,
    )

@socketio.on('replay_terminal')
def on_replay_terminal(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    if bridge:
        bridge.attach(request.sid)
        bridge.replay_to(request.sid)


@socketio.on('start_ssh')
def on_start_ssh(data):
    cleanup_expired_sessions()
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    payload, validation_error = validate_start_ssh_payload(data)
    if validation_error:
        emit_connection_error(request.sid, validation_error, error_code='invalid_start_ssh_payload')
        return

    pending_localhost_key_setups.pop(request.sid, None)
    terminal_id = payload['terminal_id']
    replacing_existing = get_bridge(session_token, terminal_id) is not None
    if not replacing_existing and len(bridges.get(session_token, {})) >= MAX_TERMINALS_PER_CLIENT:
        emit_connection_error(
            request.sid,
            'Terminal limit reached.',
            error_code='terminal_limit_reached',
            terminal_id=terminal_id,
        )
        return

    connection_type = payload['connection_type']
    cols = 80
    rows = 24
    if connection_type == CONNECTION_TYPE_LOCAL_SHELL:
        bridge = LocalShellBridge(session_token, terminal_id)
        bridge.attach(request.sid)
        success, result = bridge.connect(cols=cols, rows=rows)
    else:
        host = payload['host']
        port = payload['port']
        user = payload['username']
        password = payload['password']
        bridge = SSHBridge(session_token, terminal_id)
        bridge.attach(request.sid)
        success, result = bridge.connect(host, port, user, password, cols=cols, rows=rows)

    if success:
        close_terminal_bridge(session_token, terminal_id)
        set_bridge(session_token, terminal_id, bridge)
        connected_payload = {'message_type': 'ssh_connected'}
        connected_payload.update(bridge.metadata(cols=cols, rows=rows))
        bridge.emit_output(connected_payload)
        socketio.start_background_task(target=bridge.read_loop)
        return

    if connection_type == CONNECTION_TYPE_LOCAL_SHELL:
        message = 'Connection failed.'
        error_code = None
        if isinstance(result, dict):
            message = result.get('message', message)
            error_code = result.get('error_code')
        elif result:
            message = str(result)

        close_bridge(bridge)
        emit_connection_error(
            request.sid,
            message,
            error_code=error_code,
            terminal_id=terminal_id,
        )
        return

    host = payload['host']
    port = payload['port']
    user = payload['username']
    password = payload['password']
    message = 'Connection failed.'
    error_code = None
    action_type = None
    action_message = None
    action_question = None
    if isinstance(result, dict):
        message = result.get('message', message)
        error_code = result.get('error_code')
        action_type = result.get('action_type')
        action_message = result.get('action_message')
        action_question = result.get('action_question')
    elif result:
        message = str(result)

    action_id = None
    if action_type == 'offer_localhost_key_setup':
        missing_entries = bridge._get_missing_local_public_keys()
        if missing_entries:
            action_id = secrets.token_urlsafe(16)
            pending_localhost_key_setups[request.sid] = {
                'action_id': action_id,
                'host': host,
                'port': port,
                'username': user,
                'terminal_id': terminal_id,
                'key_entry': missing_entries[0],
                'expires_at': time.time() + LOCALHOST_KEY_SETUP_TTL_SECONDS,
            }
        else:
            action_type = None
            action_message = None
            action_question = None

    close_bridge(bridge)
    emit_connection_error(
        request.sid,
        message,
        error_code=error_code,
        action_type=action_type,
        action_message=action_message,
        action_question=action_question,
        action_id=action_id,
        terminal_id=terminal_id,
    )

@socketio.on('setup_localhost_key_access')
def on_setup_localhost_key_access(data):
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    data = data if isinstance(data, dict) else {}
    action_id = data.get('action_id')
    pending_setup, pending_error_code = get_pending_localhost_key_setup(request.sid, action_id)
    bridge = SSHBridge(session_token, pending_setup.get('terminal_id', TERMINAL_ID_MAIN) if pending_setup else TERMINAL_ID_MAIN)

    if not pending_setup:
        result = {
            'status': 'failed',
            'message': 'No pending localhost key setup request is available.',
            'error_code': pending_error_code,
        }
    elif not bridge._can_offer_local_key_setup(pending_setup['username']):
        pending_localhost_key_setups.pop(request.sid, None)
        result = {
            'status': 'failed',
            'message': 'Automatic localhost key setup is only available for the current local user.',
            'error_code': 'localhost_key_setup_unavailable',
        }
    else:
        pending_localhost_key_setups.pop(request.sid, None)
        _, result = bridge._append_public_key_entry_to_authorized_keys(pending_setup['key_entry'])

    socketio.emit(
        'ssh_output',
        {
            'message_type': 'setup_result',
            'terminal_id': bridge.terminal_id,
            'message': result['message'],
            'setup_status': result['status'],
            'error_code': result.get('error_code'),
        },
        room=request.sid,
    )

@socketio.on('ssh_input')
def on_ssh_input(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    if not bridge:
        return
    bridge.attach(request.sid)
    ssh_input = data.get('data')
    if not isinstance(ssh_input, str):
        return
    if len(ssh_input.encode('utf-8', errors='ignore')) > MAX_SSH_INPUT_BYTES:
        return
    bridge.write(ssh_input)

@socketio.on('resize')
def on_resize(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    size = parse_terminal_size(data)
    if bridge and size:
        cols, rows = size
        bridge.resize(cols, rows)

@socketio.on('close_terminal')
def on_close_terminal(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    if bridge:
        bridge.emit_output({
            'message_type': 'ssh_closed',
            'message': 'Terminal session closed.',
        })
    close_terminal_bridge(session_token, terminal_id)

@socketio.on('close_all_terminals')
def on_close_all_terminals():
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    session_sids = get_session_sids(session_token)
    close_all_terminal_bridges(session_token)
    for sid in session_sids:
        socketio.emit(
            'terminal_list',
            {
                'terminals': [],
            },
            room=sid,
        )

@socketio.on('disconnect')
def on_disconnect():
    pending_localhost_key_setups.pop(request.sid, None)
    session_token = socket_session_tokens.pop(request.sid, None)
    client_ip = socket_client_ips.pop(request.sid, 'unknown')
    if session_token:
        detach_session_bridges(session_token, request.sid)
    print(f"[-] Client disconnected: {request.sid} from {client_ip}")

def is_wsl():
    if not sys.platform.startswith('linux'):
        return False

    if os.getenv('WSL_DISTRO_NAME'):
        return True

    try:
        with open('/proc/version', 'r', encoding='utf-8') as version_file:
            return 'microsoft' in version_file.read().lower()
    except OSError:
        return False

def get_primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_wsl_ip():
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, check=False)
        ips = result.stdout.strip().split()
        if ips:
            return ips[0]
    except Exception:
        pass

    return get_primary_ip()

def get_bind_host():
    if DEFAULT_BIND_HOST:
        return DEFAULT_BIND_HOST

    if is_wsl():
        return get_wsl_ip()

    return "127.0.0.1"

def get_access_host(bind_host):
    if bind_host in {"0.0.0.0", "::"}:
        return get_primary_ip()

    return bind_host

def is_loopback_bind(bind_host):
    return bind_host in {'127.0.0.1', 'localhost', '::1'}

def is_local_shell_enabled():
    return DEFAULT_CONNECTION_TYPE == CONNECTION_TYPE_LOCAL_SHELL or FORCE_CONNECTION_TYPE == CONNECTION_TYPE_LOCAL_SHELL

def get_runtime_name():
    if is_wsl():
        return "WSL"
    if sys.platform == 'darwin':
        return "macOS"
    if sys.platform.startswith('win'):
        return "Windows"
    return "Linux"

if __name__ == '__main__':
    print("[*] Python imports completed; resolving bind host...", flush=True)
    bind_host = get_bind_host()
    access_host = get_access_host(bind_host)
    port = DEFAULT_PORT
    print("\n" + "="*60)
    print(f"WebSSH Server Starting...")
    print(f"Runtime: {get_runtime_name()}")
    print(f"Async Mode: {ASYNC_MODE}")
    print(f"Default Connection: {DEFAULT_CONNECTION_TYPE}")
    if FORCE_CONNECTION_TYPE:
        print(f"Forced Connection: {FORCE_CONNECTION_TYPE}")
    print(f"Access URL: http://{access_host}:{port}/?token={ACCESS_TOKEN}")
    print(f"Listening on: {bind_host}:{port}")
    if is_local_shell_enabled() and not is_loopback_bind(bind_host) and os.getenv('WEBSSH_ALLOW_REMOTE_LOCAL_SHELL') != '1':
        print("[!] WARNING: Local Shell is enabled while listening on a non-loopback address.")
        print("[!] Set WEBSSH_ALLOW_REMOTE_LOCAL_SHELL=1 to acknowledge this deployment mode.")
    if sys.platform == 'darwin':
        print("Tip: Enable Remote Login in macOS if you want localhost SSH access.")
    print("="*60 + "\n")
    
    sys.stdout.flush()

    run_kwargs = {'host': bind_host, 'port': port, 'log_output': False}
    if ASYNC_MODE == 'threading':
        run_kwargs['allow_unsafe_werkzeug'] = True

    socketio.run(app, **run_kwargs)
