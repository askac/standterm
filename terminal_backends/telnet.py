"""Telnet terminal backend for device consoles."""

import codecs
import socket
import threading

from .base import BackendStartFieldSchema, TerminalBackendPlugin, TerminalBridge
from .windows_network import WindowsNetworkSocket, windows_network_executable


IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
BINARY, ECHO, SGA, TTYPE, NAWS = 0, 1, 3, 24, 31
TELNET_ENCODINGS = {'utf-8': 'utf-8', 'big5': 'cp950', 'latin-1': 'latin-1'}
CONNECT_TIMEOUT = 10


class TelnetProtocol:
    """Separate Telnet commands from data across arbitrary TCP reads."""

    def __init__(self, cols=80, rows=24):
        self.state = 'data'
        self.command = None
        self.option = None
        self.subnegotiation = bytearray()
        self.local_options = set()
        self.remote_options = set()
        self.cols, self.rows = cols, rows
        self.pending_cr = False

    def initial_commands(self):
        return bytes((IAC, WILL, NAWS, IAC, WILL, TTYPE,
                      IAC, WILL, BINARY, IAC, DO, BINARY,
                      IAC, WILL, SGA, IAC, DO, SGA))

    def window_size(self, cols, rows):
        self.cols, self.rows = max(1, min(cols, 65535)), max(1, min(rows, 65535))
        if NAWS not in self.local_options:
            return b''
        dimensions = self.cols.to_bytes(2, 'big') + self.rows.to_bytes(2, 'big')
        return bytes((IAC, SB, NAWS)) + dimensions.replace(b'\xff', b'\xff\xff') + bytes((IAC, SE))

    def encode_input(self, data):
        if BINARY not in self.local_options:
            data = data.replace(b'\r\n', b'\n').replace(b'\r', b'\n').replace(b'\n', b'\r\n')
        return data.replace(b'\xff', b'\xff\xff')

    def _negotiate(self, command, option):
        if command in (DO, DONT):
            enabled = command == DO and option in (BINARY, SGA, NAWS, TTYPE)
            current = option in self.local_options
            if enabled:
                self.local_options.add(option)
            else:
                self.local_options.discard(option)
            response = bytes((IAC, WILL if enabled else WONT, option)) if enabled != current or not enabled else b''
            if enabled and option == NAWS and not current:
                response += self.window_size(self.cols, self.rows)
            return response
        enabled = command == WILL and option in (BINARY, ECHO, SGA)
        current = option in self.remote_options
        if enabled:
            self.remote_options.add(option)
        else:
            self.remote_options.discard(option)
        return bytes((IAC, DO if enabled else DONT, option)) if enabled != current or not enabled else b''

    def feed(self, chunk):
        output, replies = bytearray(), bytearray()
        for byte in chunk:
            if self.state == 'data':
                if byte == IAC:
                    self.state = 'command'
                else:
                    if self.pending_cr:
                        if byte != 0:
                            output.append(13)
                        self.pending_cr = False
                        if byte == 0:
                            continue
                    if byte == 13 and BINARY not in self.remote_options:
                        self.pending_cr = True
                    else:
                        output.append(byte)
            elif self.state == 'command':
                self.state = 'data'
                if byte == IAC:
                    output.append(IAC)
                elif byte in (DO, DONT, WILL, WONT):
                    self.command = byte
                    self.state = 'option'
                elif byte == SB:
                    self.subnegotiation.clear()
                    self.state = 'subnegotiation'
            elif self.state == 'option':
                replies.extend(self._negotiate(self.command, byte))
                self.state = 'data'
            elif self.state == 'subnegotiation':
                if byte == IAC:
                    self.state = 'subnegotiation_iac'
                elif len(self.subnegotiation) < 64:
                    self.subnegotiation.append(byte)
            else:
                if byte == SE:
                    if self.subnegotiation == bytes((TTYPE, 1)) and TTYPE in self.local_options:
                        replies.extend(bytes((IAC, SB, TTYPE, 0)) + b'XTERM-256COLOR' + bytes((IAC, SE)))
                    self.state = 'data'
                else:
                    if len(self.subnegotiation) < 64:
                        self.subnegotiation.append(byte)
                    self.state = 'subnegotiation'
        return bytes(output), bytes(replies)


class TelnetBridge(TerminalBridge):
    connection_type = 'telnet'
    terminal_kind = 'telnet'

    def __init__(self, owner_session, terminal_id, host, port, encoding, network_origin, runtime=None):
        super().__init__(owner_session, terminal_id, runtime=runtime)
        self.host, self.port = host, port
        self.encoding = TELNET_ENCODINGS[encoding]
        self.network_origin = network_origin
        self.terminal_label = f'Telnet {host}:{port}'
        self.protocol = TelnetProtocol()
        self.decoder = codecs.getincrementaldecoder(self.encoding)(errors='replace')
        self.socket = None
        self.socket_lock = threading.RLock()

    def connect(self, cols=80, rows=24):
        self.protocol.window_size(cols, rows)
        try:
            if self.network_origin == 'windows':
                sock = WindowsNetworkSocket()
                self.socket = sock
                sock.settimeout(CONNECT_TIMEOUT)
                sock.connect((self.host, self.port))
            else:
                sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
                self.socket = sock
            sock.settimeout(1)
            self._send(self.protocol.initial_commands())
            return True, None
        except (OSError, ValueError) as exc:
            self.close()
            return False, {'message': str(exc), 'error_code': 'telnet_connect_failed'}

    def _send(self, data):
        with self.socket_lock:
            while data and self.socket and not self.closing:
                sent = self.socket.send(data)
                if sent <= 0:
                    raise OSError('Telnet connection closed while sending.')
                data = data[sent:]

    def read_loop(self):
        close_message = 'Telnet connection closed.'
        try:
            while not self.closing and self.socket:
                try:
                    chunk = self.socket.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                output, reply = self.protocol.feed(chunk)
                if reply:
                    self._send(reply)
                if output:
                    text = self.decoder.decode(output)
                    if text:
                        self.emit_output({'message_type': 'terminal', 'data': text})
            remainder = self.decoder.decode(b'', final=True)
            if remainder and not self.closing:
                self.emit_output({'message_type': 'terminal', 'data': remainder})
        except OSError as exc:
            close_message = str(exc)
        finally:
            if not self.closing:
                self.emit_output({'message_type': 'ssh_closed', 'message': close_message})
            self.close()
            self.runtime.unregister_bridge(self.owner_session, self.terminal_id, self)

    def write(self, data):
        try:
            self._send(self.protocol.encode_input(data.encode(self.encoding, errors='replace')))
        except OSError:
            self.close()

    def resize(self, cols, rows):
        command = self.protocol.window_size(cols, rows)
        if command:
            try:
                self._send(command)
            except OSError:
                self.close()

    def close(self):
        self.closing = True
        sock, self.socket = self.socket, None
        if sock:
            sock.close()


class TelnetBackendPlugin(TerminalBackendPlugin):
    connection_type = 'telnet'
    label = 'Telnet'

    def __init__(self, is_allowed_for_client):
        self._is_allowed_for_client = is_allowed_for_client

    def build_policy_option(self, context=None, browser_authorized=False):
        allowed = self._is_allowed_for_client(context.client_ip, browser_authorized=context.browser_authorized)
        return {'connection_type': self.connection_type, 'label': self.label, 'allowed': allowed,
                'authorization_available': not allowed, 'browser_authorized': context.browser_authorized}

    def get_start_form_schema(self, context=None):
        fields = [
            BackendStartFieldSchema(name='host', label='Host', value_type='string', input_type='text', required=True, max_length=255),
            BackendStartFieldSchema(name='port', label='Port', value_type='integer', input_type='text', required=True,
                                    default_value=23, min_value=1, max_value=65535),
            BackendStartFieldSchema(name='encoding', label='Encoding', value_type='string', input_type='select',
                                    default_value='utf-8', options=tuple({'value': name, 'label': name.upper()}
                                                                       for name in TELNET_ENCODINGS)),
        ]
        if windows_network_executable():
            fields.append(BackendStartFieldSchema(name='network_origin', label='Connect from', value_type='string',
                                                  input_type='select', default_value='core',
                                                  options=({'value': 'core', 'label': 'Core (WSL)'},
                                                           {'value': 'windows', 'label': 'Windows'})))
        return fields

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False, context=None):
        if not self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {'message': 'Telnet is not available for this client.', 'error_code': 'telnet_unauthorized'}
        host, port = data.get('host'), data.get('port', 23)
        if not isinstance(host, str) or not host or len(host) > 255 or any(ord(c) <= 32 or ord(c) == 127 for c in host):
            return None, {'message': 'Enter a valid Telnet host.', 'error_code': 'telnet_invalid_host'}
        try:
            port = int(port) if not isinstance(port, bool) else 0
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            return None, {'message': 'Enter a valid Telnet port.', 'error_code': 'telnet_invalid_port'}
        encoding = data.get('encoding', 'utf-8')
        if not isinstance(encoding, str) or encoding not in TELNET_ENCODINGS:
            return None, {'message': 'Unsupported Telnet encoding.', 'error_code': 'telnet_invalid_encoding'}
        origin = data.get('network_origin', 'core')
        if origin not in ('core', 'windows'):
            return None, {'message': 'Invalid Telnet network origin.', 'error_code': 'telnet_invalid_network_origin'}
        if origin == 'windows' and not windows_network_executable():
            return None, {'message': 'Windows network access is unavailable.', 'error_code': 'telnet_windows_network_unavailable'}
        return {'host': host, 'port': port, 'encoding': encoding, 'network_origin': origin}, None

    def create_bridge(self, session_token, terminal_id, payload):
        return TelnetBridge(session_token, terminal_id, payload['host'], payload['port'], payload['encoding'],
                            payload['network_origin'])

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols, rows)
