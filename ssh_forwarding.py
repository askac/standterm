"""Shared SSH forwarding primitives for user tunnels and the Agent preset."""
import ipaddress
import secrets
import socket
import threading


FORWARD_BIND_HOST = '127.0.0.1'
FORWARD_CONNECT_TIMEOUT = 10
FORWARD_POLL_SECONDS = 1
FORWARD_BUFFER_BYTES = 65536
FORWARD_MAX_CONNECTIONS = 32
_contexts_lock = threading.Lock()


def is_loopback_address(address):
    try:
        value = ipaddress.ip_address(address)
        return value.is_loopback or bool(getattr(value, 'ipv4_mapped', None) and value.ipv4_mapped.is_loopback)
    except ValueError:
        return False


class SSHForwarding:
    def __init__(self, transport):
        self.transport = transport
        self.id = secrets.token_urlsafe(12)
        self.connections = threading.BoundedSemaphore(FORWARD_MAX_CONNECTIONS)
        self.lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.routes = {}

    def request_remote(self, port, owner, accept, cancelled):
        # Paramiko has one global request response slot and one TCP handler.
        # A timed-out caller must not release this lock for its still-running I/O.
        if not self.request_lock.acquire(blocking=False):
            raise RuntimeError('Another remote tunnel request is still pending. Retry after it finishes.')
        try:
            if cancelled():
                raise RuntimeError('Tunnel setup was cancelled.')
            allocated = self.transport.request_port_forward(FORWARD_BIND_HOST, port, handler=self._dispatch)
            if not 1 <= allocated <= 65535:
                raise RuntimeError('The SSH server returned an invalid listening port.')
            with self.lock:
                if allocated in self.routes:
                    raise RuntimeError('The SSH server reused an active tunnel port.')
                self.routes[allocated] = (owner, accept)
            if cancelled():
                self.cancel_remote(allocated, owner)
            return allocated
        finally:
            self.request_lock.release()

    def cancel_remote(self, port, owner):
        with self.lock:
            route = self.routes.get(port)
            if not route or route[0] is not owner:
                return
            del self.routes[port]
        if self.transport.is_active():
            # cancel_port_forward clears Paramiko's handler for every listener.
            self.transport.global_request('cancel-tcpip-forward', (FORWARD_BIND_HOST, port), wait=False)

    def _dispatch(self, channel, origin, destination):
        with self.lock:
            route = self.routes.get(destination[1])
        if not route:
            channel.close()
            return
        # Callbacks only admit and start a worker; never connect or relay here.
        try:
            route[1](channel, origin, destination)
        except Exception:
            channel.close()


def forwarding_for(transport):
    with _contexts_lock:
        context = getattr(transport, '_standterm_forwarding', None)
        if context is None:
            context = SSHForwarding(transport)
            transport._standterm_forwarding = context
        return context


def relay_tcp(left, right, stopped, progress=None):
    """Drain both TCP directions, including responses after a write half-close."""
    failed = threading.Event()
    left.settimeout(FORWARD_POLL_SECONDS)
    right.settimeout(FORWARD_POLL_SECONDS)

    def pump(source, destination, direction):
        try:
            while not stopped() and not failed.is_set():
                try:
                    data = source.recv(FORWARD_BUFFER_BYTES)
                except socket.timeout:
                    continue
                if not data:
                    if hasattr(destination, 'shutdown_write'):
                        destination.shutdown_write()
                    else:
                        destination.shutdown(socket.SHUT_WR)
                    return
                pending = memoryview(data)
                while pending and not stopped() and not failed.is_set():
                    try:
                        sent = destination.send(pending)
                    except socket.timeout:
                        continue
                    if not sent:
                        raise EOFError('The forwarding destination closed.')
                    pending = pending[sent:]
                    if progress:
                        progress(direction, sent)
        except (OSError, EOFError):
            failed.set()
            source.close()
            destination.close()

    reverse = threading.Thread(target=pump, args=(right, left, 'received'), daemon=True)
    try:
        reverse.start()
        pump(left, right, 'sent')
        while reverse.is_alive():
            if stopped() or failed.is_set():
                left.close()
                right.close()
            reverse.join(FORWARD_POLL_SECONDS)
    finally:
        left.close()
        right.close()
