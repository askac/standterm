"""WSL-only TCP transport through Windows; SSH remains in the Core process."""

import base64
import errno
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import threading
import time


HELPER_EXIT_TIMEOUT = 1
READY = b'STANDTERM_TCP_READY\n'
# Endpoint values arrive as JSON data, never as PowerShell source or arguments.
# The helper opens only an outbound socket and exits when either pipe closes.
RELAY_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$inputStream = [Console]::OpenStandardInput()
$outputStream = [Console]::OpenStandardOutput()
$client = $null
try {
    $reader = New-Object IO.StreamReader($inputStream, [Text.Encoding]::UTF8, $false, 1024, $true)
    $line = $reader.ReadLine()
    if ($null -eq $line -or $line.Length -gt 4096) { throw 'Invalid request' }
    $request = $line | ConvertFrom-Json
    if ($request.version -ne 1 -or $request.host -isnot [string] -or !$request.host.Length -or
        $request.host.Length -gt 255 -or $request.port -lt 1 -or $request.port -gt 65535 -or
        $request.timeout_ms -lt 1 -or $request.timeout_ms -gt 60000) { throw 'Invalid request' }
    $reader.Dispose()
    $hello = [Text.Encoding]::ASCII.GetBytes("STANDTERM_TCP_HELPER $PID`n")
    $outputStream.Write($hello, 0, $hello.Length)
    $outputStream.Flush()
    $client = New-Object Net.Sockets.TcpClient
    $pending = $client.ConnectAsync([string]$request.host, [int]$request.port)
    if (!$pending.Wait([int]$request.timeout_ms)) { throw 'Connection timed out' }
    $client.NoDelay = $true
    $stream = $client.GetStream()
    $ready = [Text.Encoding]::ASCII.GetBytes("STANDTERM_TCP_READY`n")
    $outputStream.Write($ready, 0, $ready.Length)
    $outputStream.Flush()
    $up = $inputStream.CopyToAsync($stream)
    $down = $stream.CopyToAsync($outputStream)
    [Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]@($up, $down)) | Out-Null
} catch {
    exit 1
} finally {
    if ($null -ne $client) { $client.Close() }
}
'''


def windows_network_executable():
    if sys.platform != 'linux':
        return None
    try:
        if 'microsoft' not in Path('/proc/sys/kernel/osrelease').read_text().lower():
            return None
    except OSError:
        return None
    if not os.environ.get('WSL_INTEROP'):
        return None
    return shutil.which('powershell.exe')


class WindowsNetworkSocket:
    def __init__(self):
        self.process = None
        self.helper_pid = None
        self.timeout = None
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self):
        return self._closed

    def connect(self, address, timeout):
        executable = windows_network_executable()
        if not executable:
            raise OSError('Windows network access requires WSL interoperability and Windows PowerShell on PATH.')
        host, port = address
        if (not isinstance(host, str) or not 1 <= len(host) <= 255
                or any(ord(char) < 32 or ord(char) == 127 for char in host)
                or type(port) is not int or not 1 <= port <= 65535 or not 0 < timeout <= 60):
            raise ValueError('Invalid Windows network endpoint.')
        script = base64.b64encode(RELAY_SCRIPT.encode('utf-16-le')).decode('ascii')
        with self._lock:
            if self._closed:
                raise OSError('Windows network connection canceled.')
            if self.process is not None:
                raise OSError('Windows network socket is already connected.')
            self.process = subprocess.Popen(
                [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        deadline = time.monotonic() + timeout
        request = (json.dumps({'version': 1, 'host': host, 'port': port,
                               'timeout_ms': int(timeout * 1000)}) + '\n').encode('utf-8')
        try:
            while request:
                self.settimeout(max(0, deadline - time.monotonic()))
                request = request[self.send(request):]
            # Do not send SSH bytes until the helper's bounded request reader is done.
            hello = self._read_line(deadline)
            prefix, _, pid = hello.rstrip(b'\n').partition(b' ')
            if prefix != b'STANDTERM_TCP_HELPER' or not pid.isdigit() or not 0 < int(pid) <= 2147483647:
                raise OSError('Invalid Windows network helper identity.')
            self.helper_pid = int(pid)
            if self._read_line(deadline) != READY:
                raise OSError('Invalid Windows network helper response.')
            self.settimeout(timeout)
        except BaseException:
            self.close()
            raise

    def _read_line(self, deadline):
        response = b''
        while len(response) < 128:
            self.settimeout(max(0, deadline - time.monotonic()))
            part = self.recv(1)
            if not part:
                raise OSError('Windows could not open the network connection. Check Windows reachability and PowerShell policy.')
            response += part
            if part == b'\n':
                return response
        raise OSError('Invalid Windows network helper response.')

    def settimeout(self, timeout):
        self.timeout = timeout

    def _pipe(self, sending):
        if self._closed or self.process is None:
            raise OSError(errno.EBADF, 'Windows network socket is closed.')
        return self.process.stdin if sending else self.process.stdout

    def send(self, data):
        pipe = self._pipe(True)
        try:
            if not select.select([], [pipe], [], self.timeout)[1]:
                raise socket.timeout('Windows network write timed out.')
            return os.write(pipe.fileno(), data)
        except ValueError as exc:
            raise OSError(errno.EBADF, 'Windows network socket is closed.') from exc

    def recv(self, size):
        if not size or self._closed:
            return b''
        pipe = self._pipe(False)
        try:
            if not select.select([pipe], [], [], self.timeout)[0]:
                raise socket.timeout('Windows network read timed out.')
            return os.read(pipe.fileno(), size)
        except (OSError, ValueError):
            if self._closed:
                return b''
            raise

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self.process
        if process is None:
            return
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        try:
            process.wait(timeout=HELPER_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=HELPER_EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=HELPER_EXIT_TIMEOUT)
