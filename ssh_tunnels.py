"""User-configured TCP tunnels; authentication belongs to the calling adapter."""
import ipaddress
import secrets
import socket
import threading
import time

from ssh_forwarding import (
    FORWARD_BIND_HOST, FORWARD_CONNECT_TIMEOUT, FORWARD_POLL_SECONDS,
    forwarding_for, is_loopback_address, relay_tcp,
)


USER_TUNNEL_SETUP_TIMEOUT = 45
USER_TUNNEL_MAX_CONNECTIONS = 8
USER_TUNNEL_MAX_ACTIVE = 8
USER_TUNNEL_MAX_RECORDS = 64


def parse_tunnel_spec(data):
    if not isinstance(data, dict) or not isinstance(data.get('direction'), str) or data['direction'] not in {'local', 'remote'}:
        raise ValueError('Choose a tunnel direction.')
    ports = {}
    for field, minimum in (('listen_port', 0), ('target_port', 1)):
        value = data.get(field)
        if type(value) is not int or not minimum <= value <= 65535:
            raise ValueError('Enter valid port numbers. Listening port 0 chooses an available port.')
        ports[field] = value
    host = data.get('target_host')
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError('Enter a target hostname or IP address.')
    if any(character.isspace() or ord(character) < 32 for character in host) or any(c in host for c in '/\\@\x7f'):
        raise ValueError('Enter a hostname or IP address without a URL scheme or path.')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            raise ValueError('The target hostname is invalid.') from None
        if len(host) > 253 or ':' in host or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                              or not all(c.isalnum() or c in '-_' for c in label)
                              for label in host.rstrip('.').split('.')):
            raise ValueError('The target hostname is invalid.')
    name = data.get('name', '')
    if not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('Tunnel names must be at most 80 characters without control characters.')
    return {'direction': data['direction'], 'target_host': host, 'name': name, **ports}


class UserTunnel:
    @staticmethod
    def connection_id(transport):
        return forwarding_for(transport).id

    def __init__(self, transport, spec, *, authorized, changed):
        self.id = 'ssht_' + secrets.token_urlsafe(18)
        self.transport = transport
        self.forwarding = forwarding_for(transport)
        self.spec = dict(spec)
        self.authorized = authorized
        self.changed = changed
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.setup_done = threading.Event()
        self.status = 'starting'
        self.error = None
        self.port = None
        self.listener = None
        self.channels = set()
        self.bytes_sent = 0
        self.bytes_received = 0
        self.revision = 0
        self.created_at = time.monotonic()

    def snapshot(self):
        with self.lock:
            self.revision += 1
            return {'tunnel_id': self.id, **self.spec, 'status': self.status,
                    'revision': self.revision,
                    'bound_port': self.port, 'bind_host': FORWARD_BIND_HOST,
                    'connections': len(self.channels), 'bytes_sent': self.bytes_sent,
                    'bytes_received': self.bytes_received, 'error': self.error,
                    'cleanup_pending': self.closed.is_set() and not self.setup_done.is_set()}

    def available(self):
        return not self.closed.is_set() and self.transport.is_active() and self.authorized()

    def start(self):
        if self.closed.is_set():
            self.setup_done.set()
            return
        try:
            threading.Thread(target=self._setup, daemon=True).start()
        except Exception:
            self.setup_done.set()
            self.stop('The tunnel setup worker could not be started.')
            return
        if not self.setup_done.wait(USER_TUNNEL_SETUP_TIMEOUT):
            self.stop('Tunnel setup timed out. A pending remote request must finish before another can start.')

    def _setup(self):
        try:
            if not self.available():
                raise RuntimeError('The SSH connection or owning viewer is no longer available.')
            if self.spec['direction'] == 'local':
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                with self.lock:
                    self.listener = listener
                    if self.closed.is_set():
                        listener.close()
                        return
                listener.bind((FORWARD_BIND_HOST, self.spec['listen_port']))
                listener.listen(USER_TUNNEL_MAX_CONNECTIONS)
                listener.settimeout(FORWARD_POLL_SECONDS)
                self.port = listener.getsockname()[1]
            else:
                self.port = self.forwarding.request_remote(
                    self.spec['listen_port'], self, self._accept_remote, lambda: not self.available())
            with self.lock:
                if not self.available():
                    raise RuntimeError('Tunnel setup was cancelled.')
                self.status = 'listening'
            self.changed()
            threading.Thread(target=self._monitor, daemon=True).start()
        except Exception as exc:
            self.stop(str(exc))
        finally:
            self.setup_done.set()
            if self.closed.is_set() and self.port and self.spec['direction'] == 'remote':
                self.forwarding.cancel_remote(self.port, self)
            self.changed()

    def _monitor(self):
        while self.available():
            if self.spec['direction'] == 'remote':
                self.closed.wait(FORWARD_POLL_SECONDS)
                continue
            try:
                channel, origin = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._accept(channel, origin)
        self.stop()

    def _accept_remote(self, channel, origin, destination):
        if destination != (FORWARD_BIND_HOST, self.port) or not is_loopback_address(origin[0]):
            channel.close()
            return
        self._accept(channel, origin)

    def _accept(self, channel, origin):
        with self.lock:
            if (not self.available() or len(self.channels) >= USER_TUNNEL_MAX_CONNECTIONS
                    or not self.forwarding.connections.acquire(blocking=False)):
                channel.close()
                return
            self.channels.add(channel)
        try:
            threading.Thread(target=self._connect, args=(channel, origin), daemon=True).start()
        except Exception:
            with self.lock:
                self.channels.discard(channel)
                self.error = 'A forwarding worker could not be started.'
            self.forwarding.connections.release()
            channel.close()
            self.changed()

    def _connect(self, channel, origin):
        target = None
        try:
            address = (self.spec['target_host'], self.spec['target_port'])
            if self.spec['direction'] == 'local':
                target = self.transport.open_channel('direct-tcpip', address, origin, timeout=FORWARD_CONNECT_TIMEOUT)
            else:
                target = socket.create_connection(address, FORWARD_CONNECT_TIMEOUT)
            if not self.available():
                return
            with self.lock:
                self.error = None
            self.changed()
            relay_tcp(channel, target, lambda: not self.available(), self._progress)
        except Exception:
            with self.lock:
                if not self.closed.is_set():
                    self.error = 'A connection to the target failed. Check its address, service and SSH forwarding policy.'
            self.changed()
        finally:
            channel.close()
            if target:
                target.close()
            with self.lock:
                self.channels.discard(channel)
            self.forwarding.connections.release()
            self.changed()

    def _progress(self, direction, size):
        with self.lock:
            if direction == 'sent':
                self.bytes_sent += size
            else:
                self.bytes_received += size

    def stop(self, error=None):
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            self.status = 'failed' if error else 'stopped'
            self.error = error
            listener, channels = self.listener, list(self.channels)
        if listener:
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            listener.close()
        for channel in channels:
            channel.close()
        if self.port and self.spec['direction'] == 'remote':
            threading.Thread(target=self.forwarding.cancel_remote, args=(self.port, self), daemon=True).start()
        self.changed()
