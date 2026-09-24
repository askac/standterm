"""Run on WSL with Windows PowerShell and Node available; no installed service is changed."""

import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_backends.windows_network import WindowsNetworkSocket, windows_network_executable


NODE = shutil.which('node.exe')
SERVER = '''
const net = require('net'), sockets = new Set();
const server = net.createServer(c => {
  sockets.add(c); c.on('error', () => {}); c.on('close', () => sockets.delete(c));
  if (process.argv[1] === 'sink') c.pause(); else c.pipe(c);
});
server.listen(0, '127.0.0.1', () => console.log(JSON.stringify({port: server.address().port})));
process.stdin.on('data', () => { for (const c of sockets) c.end(); });
process.stdin.on('end', () => { for (const c of sockets) c.destroy(); server.close(); });
'''


@unittest.skipUnless(windows_network_executable() and NODE, 'Requires WSL interop, Windows PowerShell and Windows Node')
class WindowsNetworkTests(unittest.TestCase):
    def server(self, mode='echo'):
        process = subprocess.Popen([NODE, '-e', SERVER, mode], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def close():
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()
        self.addCleanup(close)
        return process, json.loads(process.stdout.readline())['port']

    def transport(self, port):
        sock = WindowsNetworkSocket()
        self.addCleanup(sock.close)
        sock.connect(('127.0.0.1', port), 15)
        self.assertIsNotNone(sock.helper_pid)
        return sock

    def assert_helper_gone(self, sock):
        sock.close()
        self.assertIsNotNone(sock.process.poll())
        # Check the actual Windows process, not just its WSL interop PID.
        result = subprocess.run([windows_network_executable(), '-NoProfile', '-NonInteractive', '-Command',
                                 f'if (Get-Process -Id {sock.helper_pid} -ErrorAction SilentlyContinue) {{ exit 1 }}'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        self.assertEqual(result.returncode, 0)

    def test_binary_full_duplex_timeout_remote_eof_and_windows_process_exit(self):
        server, port = self.server()
        sock = self.transport(port)
        sock.settimeout(0.1)
        with self.assertRaises(socket.timeout):
            sock.recv(1)
        sock.settimeout(10)
        data = bytes(range(256)) * 16384
        errors = []
        def send():
            try:
                remaining = memoryview(data)
                while remaining:
                    remaining = remaining[sock.send(remaining):]
            except Exception as error:
                errors.append(error)
        writer = threading.Thread(target=send)
        writer.start()
        received = bytearray()
        while len(received) < len(data):
            chunk = sock.recv(65536)
            self.assertTrue(chunk)
            received.extend(chunk)
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(received, data)
        server.stdin.write(b'close\n'); server.stdin.flush()
        self.assertEqual(sock.recv(1), b'')
        self.assert_helper_gone(sock)

    def test_backpressure_is_bounded_and_close_stops_the_windows_peer(self):
        _, port = self.server('sink')
        sock = self.transport(port)
        sock.settimeout(0.2)
        block = b'x' * 65536
        sent = 0
        with self.assertRaises(socket.timeout):
            while sent < 64 * 1024 * 1024:
                sent += sock.send(block)
        self.assertGreater(sent, 0)
        self.assertLess(sent, 64 * 1024 * 1024)
        self.assert_helper_gone(sock)

    def test_cancel_during_connect_does_not_leave_a_windows_process(self):
        sock = WindowsNetworkSocket()
        self.addCleanup(sock.close)
        errors = []
        def connect():
            try:
                sock.connect(('192.0.2.1', 22), 15)
            except OSError as error:
                errors.append(error)
        worker = threading.Thread(target=connect)
        worker.start()
        deadline = time.monotonic() + 10
        while sock.helper_pid is None and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(sock.helper_pid)
        self.assert_helper_gone(sock)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)

    def test_parent_crash_closes_an_established_windows_connection(self):
        _, port = self.server()
        code = '''import os, sys
from terminal_backends.windows_network import WindowsNetworkSocket
s = WindowsNetworkSocket(); s.connect(('127.0.0.1', int(sys.argv[1])), 15)
print(s.helper_pid, flush=True)
os._exit(0)
'''
        worker = subprocess.Popen([sys.executable, '-c', code, str(port)], stdout=subprocess.PIPE)
        pid = int(worker.stdout.readline())
        worker.wait(timeout=5); worker.stdout.close()
        result = subprocess.run([windows_network_executable(), '-NoProfile', '-NonInteractive', '-Command',
                                 f'if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 1 }}'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        self.assertEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
