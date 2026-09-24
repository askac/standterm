"""Exercise Telnet negotiation, byte handling and a real local TCP console."""

import socket
import threading
import time
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal_backends.base import BackendPolicyContext, TerminalBridgeRuntime
from terminal_backends.telnet import (
    BINARY, DO, IAC, NAWS, SB, SE, TTYPE, WILL,
    TelnetBackendPlugin, TelnetBridge, TelnetProtocol,
)
from terminal_backends import telnet


def test_protocol():
    protocol = TelnetProtocol(80, 24)
    assert bytes((IAC, WILL, BINARY)) in protocol.initial_commands()
    assert protocol.feed(bytes((IAC,)))[0] == b''
    output, reply = protocol.feed(bytes((WILL, BINARY, IAC, DO, BINARY, IAC, DO, NAWS)))
    assert output == b''
    assert BINARY in protocol.remote_options and BINARY in protocol.local_options
    assert bytes((IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE)) in reply
    assert protocol.feed(bytes((IAC, SB, TTYPE, 1, IAC, SE)))[1] == b''
    _, reply = protocol.feed(bytes((IAC, DO, TTYPE, IAC, SB, TTYPE, 1, IAC, SE)))
    assert b'XTERM-256COLOR' in reply
    output, _ = protocol.feed(bytes((0, 127, 128, IAC)))
    assert output == bytes((0, 127, 128))
    assert protocol.feed(bytes((IAC,)))[0] == bytes((255,))
    assert protocol.encode_input(bytes((0, 255, 128))) == bytes((0, 255, 255, 128))
    assert bytes((0, 255, 255)) in protocol.window_size(255, 24)
    protocol.feed(bytes((IAC, DO, 99)))
    assert 99 not in protocol.local_options
    protocol.feed(bytes((IAC, 252, BINARY, IAC, 254, BINARY)))
    assert protocol.encode_input(b'command\r') == b'command\r\n'
    assert protocol.feed(b'line\r')[0] == b'line'
    assert protocol.feed(b'\x00')[0] == b''
    assert protocol.feed(b'next')[0] == b'next'


def test_validation():
    plugin = TelnetBackendPlugin(lambda ip, browser_authorized=False: ip == '127.0.0.1' or browser_authorized)
    context = BackendPolicyContext(client_ip='127.0.0.1')
    with patch.object(telnet, 'windows_network_executable', return_value=None):
        assert 'network_origin' not in {field.name for field in plugin.get_start_form_schema(context)}
        payload, error = plugin.validate_start_payload({'host': 'device.local', 'port': 23}, 'main', '127.0.0.1')
        assert error is None and payload['network_origin'] == 'core'
        for field, value in [('host', ''), ('port', 0), ('port', True), ('encoding', 'unknown'),
                             ('encoding', []), ('network_origin', 'windows')]:
            invalid = {'host': 'device.local', 'port': 23, field: value}
            assert plugin.validate_start_payload(invalid, 'main', '127.0.0.1')[1]
        assert plugin.validate_start_payload({'host': 'device.local'}, 'main', 'remote')[1]
    with patch.object(telnet, 'windows_network_executable', return_value='powershell.exe'):
        assert 'network_origin' in {field.name for field in plugin.get_start_form_schema(context)}
        payload, error = plugin.validate_start_payload({'host': 'device.local', 'network_origin': 'windows'},
                                                       'main', '127.0.0.1')
        assert error is None and payload['network_origin'] == 'windows'


def test_socket():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    responses = []
    ready = threading.Event()

    def device():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(2)
                initial = conn.recv(1024)
                assert bytes((IAC, WILL, NAWS)) in initial
                conn.sendall(bytes((IAC, DO, NAWS, IAC, DO, BINARY, IAC, WILL, BINARY)))
                conn.sendall('中文'.encode('cp950')[:1])
                conn.sendall('中文'.encode('cp950')[1:] + b'\r\n')
                ready.set()
                responses.append(conn.recv(1024))
        finally:
            listener.close()

    thread = threading.Thread(target=device, daemon=True)
    thread.start()
    output = []
    runtime = TerminalBridgeRuntime(
        emit_socket=lambda event, payload, room=None: output.append(payload),
        build_metadata=lambda *args: {}, append_transcript=lambda *args: None,
        unregister_bridge=lambda *args: None, sleep=time.sleep,
        max_replay_events=100, max_replay_bytes=100000,
    )
    bridge = TelnetBridge('session', 'main', '127.0.0.1', listener.getsockname()[1], 'big5', 'core', runtime)
    bridge.attach('browser')
    success, error = bridge.connect(80, 24)
    assert success, error
    reader = threading.Thread(target=bridge.read_loop, daemon=True)
    reader.start()
    assert ready.wait(2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any('中文' in item.get('data', '') for item in output):
        time.sleep(0.01)
    assert any('中文' in item.get('data', '') for item in output), output
    bridge.write('ping\r')
    reader.join(timeout=3)
    thread.join(timeout=3)
    assert any(bytes((IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE)) in response for response in responses), responses
    assert not reader.is_alive() and not thread.is_alive()


def test_windows_transport():
    runtime = TerminalBridgeRuntime(
        emit_socket=lambda *args, **kwargs: None, build_metadata=lambda *args: {},
        append_transcript=lambda *args: None, unregister_bridge=lambda *args: None,
        sleep=time.sleep, max_replay_events=10, max_replay_bytes=1000,
    )
    sock = Mock()
    sock.send.side_effect = lambda data: len(data)
    with patch.object(telnet, 'WindowsNetworkSocket', return_value=sock):
        bridge = TelnetBridge('session', 'main', 'device.local', 23, 'utf-8', 'windows', runtime)
        assert bridge.connect(80, 24) == (True, None)
        sock.connect.assert_called_once_with(('device.local', 23))
        bridge.close()
        sock.close.assert_called_once()


if __name__ == '__main__':
    test_protocol()
    test_validation()
    test_socket()
    test_windows_transport()
    print('Telnet backend smoke passed.')
