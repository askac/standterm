import json
import select
import subprocess

from .base import TerminalBackendPlugin, TerminalBridge


class UARTBridge(TerminalBridge):
    connection_type = 'uart'
    terminal_kind = 'uart'

    def __init__(
        self,
        owner_session,
        terminal_id,
        port_info,
        baud_rate,
        *,
        is_wsl,
        is_windows_com_device,
        get_serial_modules,
        find_windows_python_with_pyserial,
        windows_serial_helper,
        time_func,
    ):
        super().__init__(owner_session, terminal_id)
        self.serial = None
        self.device = port_info['device']
        self.baud_rate = baud_rate
        self.terminal_label = f'UART {port_info.get("label") or self.device}'
        self._is_wsl = is_wsl
        self._is_windows_com_device = is_windows_com_device
        self._get_serial_modules = get_serial_modules
        self._find_windows_python_with_pyserial = find_windows_python_with_pyserial
        self._windows_serial_helper = windows_serial_helper
        self._time_func = time_func

    def connect(self, cols=80, rows=24):
        if self._is_wsl() and self._is_windows_com_device(self.device):
            return self._connect_wsl_windows_com()

        try:
            serial_lib, _ = self._get_serial_modules()
        except Exception:
            return False, {
                'message': 'UART requires pyserial. Re-run the launcher with --force to install dependencies.',
                'error_code': 'uart_dependency_missing',
            }

        try:
            self.serial = serial_lib.Serial(
                port=self.device,
                baudrate=self.baud_rate,
                timeout=0,
                write_timeout=1,
            )
            print(f"[+] UART opened for {self.sid}: {self.device} @ {self.baud_rate}")
            return True, None
        except serial_lib.SerialException as exc:
            print(f"[!] UART open error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_open_failed'}
        except PermissionError as exc:
            print(f"[!] UART permission error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_permission_denied'}
        except Exception as exc:
            print(f"[!] UART start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_open_failed'}

    def _connect_wsl_windows_com(self):
        helper_python, helper_error = self._find_windows_python_with_pyserial()
        if not helper_python:
            return False, {
                'message': helper_error or 'WSL Windows COM access requires Windows Python with pyserial installed.',
                'error_code': 'uart_windows_python_unavailable',
            }

        try:
            self.serial = subprocess.Popen(
                [
                    helper_python,
                    '-u',
                    '-c',
                    self._windows_serial_helper,
                    self.device,
                    str(self.baud_rate),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as exc:
            print(f"[!] Windows UART helper start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_helper_start_failed'}

        status = self._read_helper_status(timeout_seconds=5)
        if status.get('event') == 'ready':
            print(f"[+] Windows UART helper opened for {self.sid}: {self.device} @ {self.baud_rate}")
            return True, None

        message = status.get('message') or 'Windows UART helper did not become ready.'
        self._close_process(self.serial)
        self.serial = None
        return False, {'message': message, 'error_code': 'uart_open_failed'}

    def _read_helper_status(self, timeout_seconds):
        if not self.serial or not self.serial.stderr:
            return {'event': 'error', 'message': 'Windows UART helper stderr is unavailable.'}

        deadline = self._time_func() + timeout_seconds
        while self._time_func() < deadline:
            timeout = max(0, deadline - self._time_func())
            try:
                readable, _, _ = select.select([self.serial.stderr], [], [], timeout)
            except Exception as exc:
                return {'event': 'error', 'message': str(exc)}
            if not readable:
                continue
            line = self.serial.stderr.readline()
            if not line:
                break
            try:
                data = json.loads(line.decode('utf-8', errors='replace'))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get('event') in {'ready', 'error'}:
                return data

        if self.serial and self.serial.poll() is not None:
            return {'event': 'error', 'message': 'Windows UART helper exited before opening the port.'}
        return {'event': 'error', 'message': 'Timed out while opening Windows UART port.'}

    def read_loop(self):
        print(f"[*] Starting UART read loop for {self.sid}")
        while True:
            self.runtime.sleep(0.01)
            if not self.serial:
                break

            try:
                if isinstance(self.serial, subprocess.Popen):
                    data = self._read_windows_helper_once()
                else:
                    waiting = self.serial.in_waiting
                    data = self.serial.read(waiting or 1)
                if data:
                    self.emit_output({
                        'message_type': 'terminal',
                        'data': data.decode('utf-8', errors='replace'),
                    })
                elif isinstance(self.serial, subprocess.Popen) and self.serial.poll() is not None:
                    self.emit_output({
                        'message_type': 'ssh_closed',
                        'message': 'UART helper exited.',
                        'error_code': 'uart_helper_exited',
                    })
                    break
            except Exception as exc:
                if self.closing:
                    break
                print(f"[!] UART read error: {exc}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'UART connection closed due to a read error.',
                    'error_code': 'uart_read_error',
                })
                break

        print(f"[*] UART read loop terminated for {self.sid}")
        self.runtime.unregister_bridge(self.owner_session, self.terminal_id, self)

    def _read_windows_helper_once(self):
        if not self.serial or not self.serial.stdout:
            return b''
        readable, _, _ = select.select([self.serial.stdout], [], [], 0)
        if not readable:
            return b''
        return self.serial.stdout.read(4096)

    def write(self, data):
        if not self.serial:
            return
        try:
            encoded = data.encode('utf-8', errors='replace')
            if isinstance(self.serial, subprocess.Popen):
                if self.serial.stdin:
                    self.serial.stdin.write(encoded)
                    self.serial.stdin.flush()
            else:
                self.serial.write(encoded)
        except Exception as exc:
            print(f"[!] UART write error: {exc}")

    def resize(self, cols, rows):
        return

    def close(self):
        if not self.serial:
            return
        try:
            if isinstance(self.serial, subprocess.Popen):
                self._close_process(self.serial)
            else:
                self.serial.close()
        except Exception:
            pass
        self.serial = None

    def _close_process(self, process):
        if self.runtime.close_process:
            self.runtime.close_process(process)
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


class UARTBackendPlugin(TerminalBackendPlugin):
    connection_type = 'uart'
    label = 'UART'

    def __init__(
        self,
        *,
        bridge_cls,
        is_allowed_for_client,
        detect_serial_ports,
        get_detected_serial_port,
        default_baud_rate,
        min_baud_rate,
        max_baud_rate,
        baud_rates,
        bridge_kwargs,
    ):
        self._bridge_cls = bridge_cls
        self._is_allowed_for_client = is_allowed_for_client
        self._detect_serial_ports = detect_serial_ports
        self._get_detected_serial_port = get_detected_serial_port
        self._default_baud_rate = default_baud_rate
        self._min_baud_rate = min_baud_rate
        self._max_baud_rate = max_baud_rate
        self._baud_rates = baud_rates
        self._bridge_kwargs = bridge_kwargs

    def build_policy_option(self, context=None, browser_authorized=False):
        client_ip = context.client_ip if context else 'unknown'
        browser_authorized = context.browser_authorized if context else browser_authorized
        allowed = self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        return {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
            'available_ports': self._detect_serial_ports() if allowed else [],
            'default_baud_rate': self._default_baud_rate,
            'baud_rates': self._baud_rates,
        }

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        if not self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'UART is not available for this client.',
                'error_code': 'uart_unavailable_for_client',
            }

        port_info = self._get_detected_serial_port(data.get('serial_port'))
        if not port_info:
            return None, {
                'message': 'Select an available UART port.',
                'error_code': 'uart_port_unavailable',
            }

        try:
            baud_rate = int(data.get('baud_rate', self._default_baud_rate))
        except (TypeError, ValueError):
            return None, {
                'message': 'UART baud rate must be a number.',
                'error_code': 'uart_invalid_baud_rate',
            }
        if baud_rate < self._min_baud_rate or baud_rate > self._max_baud_rate:
            return None, {
                'message': 'UART baud rate is outside the supported range.',
                'error_code': 'uart_invalid_baud_rate',
            }

        return {
            'serial_port': port_info['device'],
            'serial_port_info': port_info,
            'baud_rate': baud_rate,
        }, None

    def create_bridge(self, session_token, terminal_id, payload):
        return self._bridge_cls(
            session_token,
            terminal_id,
            payload['serial_port_info'],
            payload['baud_rate'],
            **self._bridge_kwargs,
        )

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols=cols, rows=rows)
