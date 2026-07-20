import codecs
import os
import select
import sys
from pathlib import Path

from .base import BackendSettingSchema, BackendStartFieldSchema, TerminalBackendPlugin, TerminalBridge
from runtime_logging import log_message

try:
    from ptyprocess import PtyProcess
except Exception:
    PtyProcess = None

try:
    from winpty import PtyProcess as WinPtyProcess
except Exception:
    WinPtyProcess = None


def decode_local_shell_output(data, decoder=None, *, final=False):
    if isinstance(data, bytes):
        if decoder is not None:
            return decoder.decode(data, final=final)
        return data.decode('utf-8', errors='replace')
    if isinstance(data, str):
        return data
    return str(data)


class LocalShellBridge(TerminalBridge):
    connection_type = 'local_shell'

    def __init__(
        self,
        owner_session,
        terminal_id='main',
        shell_config=None,
        *,
        ssh_term,
        get_default_local_shell_config,
        runtime=None,
    ):
        super().__init__(owner_session, terminal_id, runtime=runtime)
        self.process = None
        self._ssh_term = ssh_term
        shell_config = shell_config or get_default_local_shell_config()[0]
        self.shell = shell_config['shell_display']
        self.shell_command = shell_config['shell_command']
        self.terminal_kind = shell_config['terminal_kind']
        self.terminal_label = shell_config['terminal_label']
        self._output_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    def connect(self, cols=80, rows=24):
        if sys.platform.startswith('win'):
            return self._connect_windows(cols, rows)
        if PtyProcess is None:
            return False, {
                'message': 'Local Shell requires ptyprocess. Re-run the launcher with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = dict(os.environ)
            env['TERM'] = self._ssh_term
            cwd = str(Path.home())
            self.process = PtyProcess.spawn(
                self.shell_command,
                cwd=cwd,
                env=env,
                dimensions=(rows, cols),
            )
            log_message(f"[+] Local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            log_message(f"[!] Local shell start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'local_shell_start_failed'}

    def _connect_windows(self, cols, rows):
        if WinPtyProcess is None:
            return False, {
                'message': 'Local Shell on Windows requires pywinpty. Re-run run.bat with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = dict(os.environ)
            env['TERM'] = self._ssh_term
            cwd = str(Path.home())
            self.process = self._spawn_windows_process(cols, rows, cwd, env)
            self.resize(cols, rows)
            log_message(f"[+] Windows local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            log_message(f"[!] Windows local shell start error: {exc}")
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
        log_message(f"[*] Starting local shell read loop for {self.sid}")
        while True:
            self.runtime.sleep(0.01)
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
                    decoded = decode_local_shell_output(data, self._output_decoder)
                    if not decoded:
                        continue
                    self.emit_output({
                        'message_type': 'terminal',
                        'data': decoded,
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
                log_message(f"[!] Local shell read error: {exc}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell closed due to a read error.',
                    'error_code': 'local_shell_read_error',
                })
                break

        log_message(f"[*] Local shell read loop terminated for {self.sid}")
        self.runtime.unregister_bridge(self.owner_session, self.terminal_id, self)

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
            log_message(f"[!] Windows local shell read error: {exc}")
            self.emit_output({
                'message_type': 'ssh_closed',
                'message': 'Local shell closed due to a read error.',
                'error_code': 'local_shell_read_error',
            })
            return False

    def write(self, data):
        if self.process:
            try:
                if not sys.platform.startswith('win') and isinstance(data, str):
                    data = data.encode('utf-8')
                self.process.write(data)
            except Exception as exc:
                log_message(f"[!] Local shell write error: {exc}")

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
                log_message(f"[!] Local shell resize error: {exc}")

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


class LocalShellBackendPlugin(TerminalBackendPlugin):
    connection_type = 'local_shell'
    label = 'Local Shell'

    def __init__(
        self,
        *,
        bridge_cls,
        is_allowed_for_client,
        get_local_shell_config,
        bridge_kwargs,
        is_wsl,
        get_wsl_local_shell_options,
        default_shell_kind,
        low_risk_settings_capability,
        high_risk_settings_capability,
    ):
        self._bridge_cls = bridge_cls
        self._is_allowed_for_client = is_allowed_for_client
        self._get_local_shell_config = get_local_shell_config
        self._bridge_kwargs = bridge_kwargs
        self._is_wsl = is_wsl
        self._get_wsl_local_shell_options = get_wsl_local_shell_options
        self._default_shell_kind = default_shell_kind
        self._low_risk_settings_capability = low_risk_settings_capability
        self._high_risk_settings_capability = high_risk_settings_capability

    def get_settings_schema(self):
        allowed_kinds = [item['kind'] for item in self._get_wsl_local_shell_options()] if self._is_wsl() else []
        return [
            BackendSettingSchema(
                setting_key='local_shell.default_kind',
                label='Default shell kind',
                value_type='enum',
                risk_level='low',
                required_capability=self._low_risk_settings_capability,
                default_value=self._default_shell_kind,
                allowed_values=tuple(allowed_kinds),
                restart_required=False,
                readonly_when_remote=True,
                mutable=bool(allowed_kinds),
            ),
            BackendSettingSchema(
                setting_key='local_shell.remote_access',
                label='Remote Local Shell access',
                value_type='boolean',
                risk_level='high',
                required_capability=self._high_risk_settings_capability,
                default_value=False,
                restart_required=True,
                apply_scope='restart',
                readonly_when_remote=True,
            ),
        ]

    def _get_default_shell_kind(self, context=None):
        settings_snapshot = context.settings_snapshot if context else None
        if isinstance(settings_snapshot, dict):
            value = settings_snapshot.get('local_shell.default_kind')
            if value is not None:
                normalized, error = self.validate_setting_update(
                    'local_shell.default_kind',
                    value,
                    current_value=self._default_shell_kind,
                )
                if not error:
                    return normalized
        return self._default_shell_kind

    def build_policy_option(self, context=None, browser_authorized=False):
        client_ip = context.client_ip if context else 'unknown'
        browser_authorized = context.browser_authorized if context else browser_authorized
        default_shell_kind = self._get_default_shell_kind(context=context)
        allowed = self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        option = {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
        }
        if self._is_wsl():
            option['shell_options'] = self._get_wsl_local_shell_options()
            option['default_shell_kind'] = default_shell_kind
        return option

    def get_start_form_schema(self, context=None):
        if not self._is_wsl():
            return []
        shell_options = self._get_wsl_local_shell_options()
        return [
            BackendStartFieldSchema(
                name='local_shell_kind',
                label='Shell',
                value_type='enum',
                input_type='select',
                default_value=self._get_default_shell_kind(context=context),
                required=False,
                options=tuple(
                    {
                        'value': item['kind'],
                        'label': item.get('label') or item['kind'],
                    }
                    for item in shell_options
                ),
            ),
        ]

    def validate_setting_update(self, setting_key, value, current_value=None):
        if setting_key != 'local_shell.default_kind':
            return super().validate_setting_update(setting_key, value, current_value=current_value)
        if not self._is_wsl():
            return None, {
                'error_code': 'settings_not_mutable',
                'message': 'Local Shell default kind is only mutable on WSL.',
            }
        allowed_kinds = [item['kind'] for item in self._get_wsl_local_shell_options()]
        normalized = value.strip().lower() if isinstance(value, str) else ''
        if normalized not in allowed_kinds:
            return None, {
                'error_code': 'settings_invalid_value',
                'message': 'Local Shell default kind must be bash, cmd, or powershell.',
            }
        return normalized, None

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False, context=None):
        if not self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'Local Shell is not available for this client.',
                'error_code': 'local_shell_unavailable_for_client',
            }
        requested_kind = data.get('local_shell_kind')
        if not isinstance(requested_kind, str) or not requested_kind.strip():
            requested_kind = self._get_default_shell_kind(context=context)
            if not self._is_wsl():
                requested_kind = None
        shell_config, shell_error = self._get_local_shell_config(requested_kind)
        if shell_error:
            return None, shell_error
        return {'local_shell_config': shell_config}, None

    def create_bridge(self, session_token, terminal_id, payload):
        return self._bridge_cls(
            session_token,
            terminal_id,
            shell_config=payload.get('local_shell_config'),
            **self._bridge_kwargs,
        )

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols=cols, rows=rows)
