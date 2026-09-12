#!/usr/bin/env python3
"""Provisioning checks run by Core over an authenticated SSH exec channel."""
import ipaddress
import hashlib
import importlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def freebsd_listener_addresses(port):
    result = subprocess.run(
        ['/usr/bin/netstat', '--libxo', 'json', '-an', '-p', 'tcp'],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError('Cannot inspect the SSH listener with netstat JSON.')
    try:
        entries = json.loads(result.stdout)['statistics']['socket']
        if not isinstance(entries, list):
            raise ValueError
        addresses = []
        for entry in entries:
            if entry['protocol'] not in {'tcp4', 'tcp6', 'tcp46', 'toe4', 'toe6', 'toe46'}:
                raise ValueError
            if not isinstance(entry['tcp-state'], str):
                raise ValueError
            if entry['tcp-state'] != 'LISTEN':
                continue
            local = entry['local']
            number = local['port']
            if not isinstance(number, str) or not number.isascii() or not number.isdecimal():
                raise ValueError
            if not 1 <= int(number) <= 65535 or not isinstance(local['address'], str):
                raise ValueError
            if int(number) == port:
                addresses.append(local['address'])
        return addresses
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('Cannot verify the SSH listener: invalid netstat JSON.') from None


def listener_addresses(port):
    addresses = []
    if sys.platform.startswith('freebsd'):
        return freebsd_listener_addresses(port)
    if os.path.isfile('/proc/net/tcp'):
        for name, family in (('tcp', socket.AF_INET), ('tcp6', socket.AF_INET6)):
            path = '/proc/net/' + name
            if name == 'tcp6' and not os.path.exists(path):
                continue
            with open(path, encoding='ascii') as handle:
                for line in list(handle)[1:]:
                    fields = line.split()
                    address, number = fields[1].split(':')
                    if fields[3] != '0A' or int(number, 16) != port:
                        continue
                    packed = b''.join(struct.pack('=I', int(address[i:i + 8], 16))
                                      for i in range(0, len(address), 8))
                    addresses.append(socket.inet_ntop(family, packed))
    elif shutil.which('lsof'):
        result = subprocess.run(
            [shutil.which('lsof'), '-nP', '-a', '-iTCP:' + str(port), '-sTCP:LISTEN', '-Fn'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError('Cannot inspect the SSH listener with lsof.')
        for line in result.stdout.splitlines():
            if line.startswith('n'):
                addresses.append(line[1:].rsplit(':', 1)[0].strip('[]'))
    else:
        raise RuntimeError('Cannot verify the remote loopback listener: no supported inspection tool is available.')
    return addresses


def verify(port, instance_id):
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    for relative, digest in manifest.items():
        path = root / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Agent helper bundle verification failed.')
    for name in ('cli', 'input', 'jsonl', 'repl', 'scp', 'shcmd', 'type', 'rsfile', 'mcp'):
        importlib.import_module('agent_' + name)
    addresses = listener_addresses(port)
    if not addresses:
        raise RuntimeError('Cannot verify the remote SSH listener.')
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise RuntimeError('SSH listener is not restricted to loopback.') from None
        if not parsed.is_loopback:
            raise RuntimeError('SSH listener is not restricted to loopback; check GatewayPorts.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open('http://127.0.0.1:%d/agentinfo' % port, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        payload = json.loads(response.read(65536))
    if payload.get('instance_id') != instance_id:
        raise RuntimeError('The SSH tunnel did not reach this StandTerm instance.')
    return {'verified': True}


def main():
    if sys.version_info < (3, 9):
        raise RuntimeError('Agent tunnel requires Python 3.9 or newer.')
    if sys.argv[1:] == ['prepare']:
        root = tempfile.mkdtemp(prefix='standterm-agent-')
        os.chmod(root, 0o700)
        result = {'root': root, 'python_path': sys.executable}
    elif len(sys.argv) == 4 and sys.argv[1] == 'verify':
        result = verify(int(sys.argv[2]), sys.argv[3])
    else:
        raise RuntimeError('Invalid provisioning operation.')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)
