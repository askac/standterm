import base64
import getpass
import os
from pathlib import Path

from .base import BackendAction, BackendSettingSchema, BackendStartFieldSchema, TerminalBackendPlugin, TerminalBridge


class SSHBridge(TerminalBridge):
    connection_type = 'ssh'
    terminal_kind = 'ssh'
    terminal_label = 'SSH'

    def __init__(
        self,
        owner_session,
        terminal_id='main',
        *,
        get_paramiko,
        ssh_term,
        local_public_key_types,
    ):
        super().__init__(owner_session, terminal_id)
        self._get_paramiko = get_paramiko
        self._ssh_term = ssh_term
        self._local_public_key_types = local_public_key_types
        self.ssh = None
        self._reset_ssh_client()
        self.channel = None

    def _reset_ssh_client(self, trust_unknown_host=False):
        paramiko_module = self._get_paramiko()
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

    def _parse_public_key_line(self, line):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            return None

        parts = stripped.split()
        for index in range(len(parts) - 1):
            key_type = parts[index]
            key_body = parts[index + 1]
            if key_type not in self._local_public_key_types:
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

    def prepare_backend_action(self, action_type, payload, expires_at, message=None, question=None):
        if action_type != 'offer_localhost_key_setup':
            return None
        missing_entries = self._get_missing_local_public_keys()
        if not missing_entries:
            return None
        return BackendAction(
            action_type=action_type,
            terminal_id=payload['terminal_id'],
            metadata={
                'host': payload['host'],
                'port': payload['port'],
                'username': payload['username'],
                'key_entry': missing_entries[0],
            },
            expires_at=expires_at,
            message=message,
            question=question,
        )

    @classmethod
    def execute_backend_action(cls, action, **bridge_kwargs):
        if action.action_type != 'offer_localhost_key_setup':
            return {
                'status': 'failed',
                'message': 'Unsupported SSH backend action.',
                'error_code': 'backend_action_unsupported',
            }
        metadata = action.metadata if isinstance(action.metadata, dict) else {}
        username = metadata.get('username')
        key_entry = metadata.get('key_entry')
        bridge = cls(None, action.terminal_id, **bridge_kwargs)
        if not isinstance(key_entry, dict):
            return {
                'status': 'failed',
                'message': 'Localhost key setup action is invalid.',
                'error_code': 'localhost_key_setup_invalid_action',
            }
        if not bridge._can_offer_local_key_setup(username):
            return {
                'status': 'failed',
                'message': 'Automatic localhost key setup is only available for the current local user.',
                'error_code': 'localhost_key_setup_unavailable',
            }
        _, result = bridge._append_public_key_entry_to_authorized_keys(key_entry)
        return result

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

    def _load_private_key(self, key_path, passphrase=None):
        paramiko_module = self._get_paramiko()
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
        paramiko_module = self._get_paramiko()
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
        paramiko_module = self._get_paramiko()
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

            self.channel = self.ssh.invoke_shell(term=self._ssh_term, width=cols, height=rows)
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
            self.runtime.sleep(0.01)
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
        self.runtime.unregister_bridge(self.owner_session, self.terminal_id, self)

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


class SSHBackendPlugin(TerminalBackendPlugin):
    connection_type = 'ssh'
    label = 'SSH'

    def __init__(
        self,
        *,
        bridge_cls,
        default_host,
        default_port,
        default_user,
        max_host_length,
        max_username_length,
        max_password_bytes,
        has_control_chars,
        allowed_action_types,
        backend_action_store,
        bridge_kwargs,
        low_risk_settings_capability,
        high_risk_settings_capability,
        key_setup_ttl_seconds,
        token_urlsafe,
        time_func,
    ):
        self._bridge_cls = bridge_cls
        self._default_host = default_host
        self._default_port = default_port
        self._default_user = default_user
        self._max_host_length = max_host_length
        self._max_username_length = max_username_length
        self._max_password_bytes = max_password_bytes
        self._has_control_chars = has_control_chars
        self._allowed_action_types = allowed_action_types
        self._backend_action_store = backend_action_store
        self._bridge_kwargs = bridge_kwargs
        self._low_risk_settings_capability = low_risk_settings_capability
        self._high_risk_settings_capability = high_risk_settings_capability
        self._key_setup_ttl_seconds = key_setup_ttl_seconds
        self._token_urlsafe = token_urlsafe
        self._time_func = time_func

    def get_settings_schema(self):
        return [
            BackendSettingSchema(
                setting_key='ssh.default_host',
                label='Default host',
                value_type='string',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_host,
                restart_required=True,
                apply_scope='restart',
                readonly_when_remote=True,
            ),
            BackendSettingSchema(
                setting_key='ssh.default_port',
                label='Default port',
                value_type='integer',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_port,
                min_value=1,
                max_value=65535,
                restart_required=True,
                apply_scope='restart',
                readonly_when_remote=True,
            ),
            BackendSettingSchema(
                setting_key='ssh.default_user',
                label='Default user',
                value_type='string',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_user,
                restart_required=True,
                apply_scope='restart',
                readonly_when_remote=True,
            ),
            BackendSettingSchema(
                setting_key='ssh.localhost_key_setup_action',
                label='Localhost key setup action',
                value_type='boolean',
                risk_level='medium',
                required_capability=self._high_risk_settings_capability,
                default_value='offer_localhost_key_setup' in self._allowed_action_types,
                restart_required=True,
                apply_scope='restart',
                readonly_when_remote=True,
            ),
        ]

    def get_start_form_schema(self, context=None):
        return [
            BackendStartFieldSchema(
                name='host',
                label='Host',
                value_type='string',
                input_type='text',
                default_value=self._default_host,
                required=True,
                max_length=self._max_host_length,
            ),
            BackendStartFieldSchema(
                name='port',
                label='Port',
                value_type='integer',
                input_type='text',
                default_value=self._default_port,
                required=True,
                min_value=1,
                max_value=65535,
            ),
            BackendStartFieldSchema(
                name='username',
                label='Username',
                value_type='string',
                input_type='text',
                default_value=self._default_user,
                required=True,
                max_length=self._max_username_length,
            ),
            BackendStartFieldSchema(
                name='password',
                label='Password',
                value_type='string',
                input_type='password',
                required=False,
                secret=True,
                max_bytes=self._max_password_bytes,
            ),
        ]

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False, context=None):
        host = data.get('host', self._default_host)
        if not isinstance(host, str):
            return None, 'Host must be a string.'
        host = host.strip()
        if not host or len(host) > self._max_host_length:
            return None, 'Host is empty or too long.'
        if self._has_control_chars(host):
            return None, 'Host contains invalid control characters.'

        try:
            port = int(data.get('port', self._default_port))
        except (TypeError, ValueError):
            return None, 'Port must be a number.'
        if port < 1 or port > 65535:
            return None, 'Port must be between 1 and 65535.'

        user = data.get('username', self._default_user)
        if not isinstance(user, str):
            return None, 'Username must be a string.'
        user = user.strip()
        if not user or len(user) > self._max_username_length:
            return None, 'Username is empty or too long.'
        if self._has_control_chars(user):
            return None, 'Username contains invalid control characters.'

        password = data.get('password') or ''
        if not isinstance(password, str):
            return None, 'Password must be a string.'
        if len(password.encode('utf-8', errors='ignore')) > self._max_password_bytes:
            return None, 'Password is too long.'

        return {
            'host': host,
            'port': port,
            'username': user,
            'password': password,
        }, None

    def create_bridge(self, session_token, terminal_id, payload):
        return self._bridge_cls(session_token, terminal_id, **self._bridge_kwargs)

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(
            payload['host'],
            payload['port'],
            payload['username'],
            payload['password'],
            cols=cols,
            rows=rows,
        )

    def prepare_connection_failure(self, sid, bridge, payload, result):
        failure = super().prepare_connection_failure(sid, bridge, payload, result)
        action_type = None
        action_message = None
        action_question = None

        if isinstance(result, dict):
            action_type = result.get('action_type')
            action_message = result.get('action_message')
            action_question = result.get('action_question')
        if action_type not in self._allowed_action_types:
            action_type = None
            action_message = None
            action_question = None

        action_id = None
        if action_type == 'offer_localhost_key_setup':
            action = bridge.prepare_backend_action(
                action_type,
                payload,
                expires_at=self._time_func() + self._key_setup_ttl_seconds,
                message=action_message,
                question=action_question,
            )
            if action:
                action_id = self._token_urlsafe(16)
                self._backend_action_store.set(sid, action_id, action)
            else:
                action_type = None
                action_message = None
                action_question = None

        failure.update({
            'action_type': action_type,
            'action_message': action_message,
            'action_question': action_question,
            'action_id': action_id,
        })
        return failure

    def execute_backend_action(self, action):
        if action.action_type != 'offer_localhost_key_setup':
            return super().execute_backend_action(action)
        return self._bridge_cls.execute_backend_action(action, **self._bridge_kwargs)
