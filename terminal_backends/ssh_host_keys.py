import base64
import hashlib
import os
import re
import secrets
import stat
import tempfile
import threading
from pathlib import Path


HOST_KEY_ACTION_TYPES = frozenset({'confirm_ssh_host_key', 'forget_ssh_host_key'})
HOST_KEYS_LOCK = threading.RLock()


def host_key_name(host, port):
    if not host or any(ch.isspace() or ch in ',|*?!#' for ch in host) or host.startswith('@'):
        raise ValueError('SSH host contains invalid known_hosts characters.')
    return host if int(port) == 22 else f'[{host}]:{int(port)}'


def fingerprint(key):
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode('ascii').rstrip('=')
    return f'{key.get_name()} SHA256:{digest}'


class HostKeyConfirmationRequired(Exception):
    def __init__(self, key):
        super().__init__('SSH host key requires confirmation.')
        self.key = key


class ConfirmHostKeyPolicy:
    def missing_host_key(self, client, hostname, key):
        raise HostKeyConfirmationRequired(key)


class SSHHostKeyStore:
    def __init__(self, paramiko_module, path=None):
        self.paramiko = paramiko_module
        self.path = Path(path) if path is not None else Path.home() / '.ssh' / 'known_hosts'

    def _read(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b''

    def _matches(self, name, target):
        if name == target:
            return True
        if name.startswith('|1|'):
            try:
                return secrets.compare_digest(self.paramiko.HostKeys.hash_host(target, name), name)
            except (ValueError, AssertionError, TypeError) as exc:
                raise ValueError('The known_hosts file contains an invalid hashed host.') from exc
        if any(ch in name for ch in '*?!'):
            pattern = re.escape(name.lstrip('!')).replace(r'\*', '.*').replace(r'\?', '.')
            return re.fullmatch(pattern, target) is not None
        return False

    def _target_lines(self, content, target):
        for line in content.decode('utf-8', errors='surrogateescape').splitlines(keepends=True):
            stripped = line.lstrip()
            if not stripped.strip() or stripped.startswith('#'):
                yield line, None, None
                continue
            fields = stripped.split()
            marked = fields[0].startswith('@')
            if marked and len(fields) < 2:
                raise ValueError('The known_hosts file contains an invalid marked entry.')
            names = fields[1 if marked else 0].split(',')
            matched = [name for name in names if self._matches(name, target)]
            if not matched:
                yield line, None, None
                continue
            if marked:
                raise ValueError('Manage marked SSH host keys outside StandTerm.')
            if any(any(ch in name for ch in '*?!') for name in names):
                raise ValueError('Manage SSH host key patterns outside StandTerm.')
            if len(fields) < 3:
                raise ValueError('The saved SSH host key is invalid. Check known_hosts.')
            try:
                entry = self.paramiko.hostkeys.HostKeyEntry.from_line(' '.join(fields[:3]))
            except Exception as exc:
                raise ValueError('The saved SSH host key is invalid. Check known_hosts.') from exc
            if entry is None or entry.key is None:
                raise ValueError('The saved SSH host key type is unsupported.')
            yield line, [name for name in names if name not in matched], entry.key

    def snapshot(self, host, port):
        target = host_key_name(host, port)
        with HOST_KEYS_LOCK:
            content = self._read()
            keys = [key for _, _, key in self._target_lines(content, target) if key is not None]
            return {
                'host_key_name': target,
                'revision': hashlib.sha256(content).hexdigest(),
                'keys': keys,
            }

    def update(self, snapshot, key=None):
        with HOST_KEYS_LOCK:
            if self.path.is_symlink():
                raise ValueError('Manage symlinked known_hosts files outside StandTerm.')
            content = self._read()
            if hashlib.sha256(content).hexdigest() != snapshot['revision']:
                raise ValueError('SSH host records changed. Try again and review the current fingerprint.')
            output = []
            for line, remaining, old_key in self._target_lines(content, snapshot['host_key_name']):
                if old_key is None:
                    output.append(line)
                elif remaining:
                    output.append(re.sub(r'^(\s*)\S+', lambda match: match[1] + ','.join(remaining), line, count=1))
            if key is not None:
                if output and not output[-1].endswith(('\n', '\r')):
                    output.append('\n')
                output.append(f"{snapshot['host_key_name']} {key.get_name()} {key.get_base64()}\n")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = stat.S_IMODE(self.path.stat().st_mode) if self.path.exists() else 0o600
            fd, temporary = tempfile.mkstemp(prefix='.standterm-known-hosts-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(''.join(output).encode('utf-8', errors='surrogateescape'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, mode)
                if self.path.is_symlink() or self._read() != content:
                    raise ValueError('SSH host records changed. Try again and review the current fingerprint.')
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
