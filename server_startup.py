"""Local launcher settings and bind-before-notify server startup."""

from contextlib import contextmanager, ExitStack
import errno
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import warnings


LAUNCHER_SETTINGS = Path(__file__).resolve().parent / 'tools' / 'launcher-settings.json'
PORT_SEARCH_LIMIT = 20
# IANA Dynamic/Private range; no registry assignments exist in this range.
# https://www.iana.org/assignments/service-names-port-numbers/
AUTOMATIC_PORT_MIN = 49152
AUTOMATIC_PORT_MAX = 65535
# Fixed-use exceptions from Apple documentation, reviewed 2026-09-07.
# https://support.apple.com/en-us/103229
# Do not exclude the documented dynamic range as though it were fixed-use.
FIXED_TCP_PORTS = {5000: 'AirPlay', 6000: 'AirPlay', 7000: 'AirPlay',
                   62078: 'Device pairing, sync and backup'}


def services_path():
    if sys.platform == 'win32':
        return Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'drivers' / 'etc' / 'services'
    return Path('/etc/services')


def service_tcp_ports(path=None):
    path = path if path is not None else services_path()
    ports = set()
    try:
        with path.open(encoding='utf-8', errors='replace') as source:
            for line in source:
                fields = line.split('#', 1)[0].split()
                if len(fields) < 2:
                    continue
                number, separator, protocol = fields[1].partition('/')
                if separator and protocol.lower() == 'tcp' and len(number) <= 5 and number.isascii() and number.isdecimal():
                    port = int(number)
                    if valid_port(port):
                        ports.add(port)
    except OSError:
        warnings.warn(f'Cannot read {path}; automatic ports use the built-in exclusions only.', RuntimeWarning)
    return ports


def automatic_port_candidates(exclude=()):
    blocked = set(FIXED_TCP_PORTS) | service_tcp_ports() | set(exclude)
    candidates = [port for port in range(AUTOMATIC_PORT_MIN, AUTOMATIC_PORT_MAX + 1) if port not in blocked]
    return random.SystemRandom().sample(candidates, min(PORT_SEARCH_LIMIT, len(candidates)))


def valid_port(value):
    return type(value) is int and 1 <= value <= 65535


def load_port(default, environ, report, path=LAUNCHER_SETTINGS):
    if 'STANDTERM_PORT' in environ:
        try:
            port = int(environ['STANDTERM_PORT'])
        except ValueError:
            raise ValueError('STANDTERM_PORT must be an integer from 1 to 65535.') from None
        if not valid_port(port):
            raise ValueError('STANDTERM_PORT must be an integer from 1 to 65535.')
        return port
    if environ.get('STANDTERM_LAUNCHER') != '1':
        return default
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1 or not valid_port(data.get('port')):
            raise ValueError('Unsupported launcher settings.')
        return data['port']
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        report(f'[!] Could not read launcher settings; selecting an automatic port: {exc}')
        return 0


def save_port(port, path=LAUNCHER_SETTINGS):
    if not valid_port(port):
        raise ValueError('Invalid launcher port.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.launcher-settings-', delete=False) as output:
            temp_path = Path(output.name)
            json.dump({'version': 1, 'port': port}, output, indent=2)
            output.write('\n')
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def address_in_use(error):
    return error.errno == errno.EADDRINUSE or getattr(error, 'winerror', None) == 10048


def suggested_port(host, port):
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    for candidate in automatic_port_candidates(exclude=(port,)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                if os.name == 'nt':
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, candidate))
                return candidate
        except OSError as exc:
            if address_in_use(exc) or exc.errno == errno.EACCES:
                continue
            raise
    return None


def confirm(message):
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    try:
        return input(message + ' [y/N] ').strip().lower() in {'y', 'yes'}
    except EOFError:
        return False


class BindFailure(Exception):
    """Keep Werkzeug from converting a typed bind error into SystemExit."""

    def __init__(self, error):
        self.error = error
        super().__init__(str(error))


@contextmanager
def bound_server(app, socketio, host, port, ssl_context):
    if port != 0:
        # Existing saved ports and explicit overrides remain operator choices.
        with _bound_server(app, socketio, host, port, ssl_context) as bound:
            yield bound
        return
    for candidate in automatic_port_candidates():
        with ExitStack() as attempt:
            try:
                bound = attempt.enter_context(_bound_server(app, socketio, host, candidate, ssl_context))
            except OSError as exc:
                if address_in_use(exc) or exc.errno == errno.EACCES:
                    continue
                raise
            # Keep the actual listener held; do not probe, close, then rebind.
            # Exceptions from the caller are not bind errors and must not retry.
            yield bound
            return
    raise RuntimeError('No automatic port could be bound. Set STANDTERM_PORT to select one explicitly.')


@contextmanager
def _bound_server(app, socketio, host, port, ssl_context):
    mode = socketio.server.eio.async_mode
    if mode == 'threading':
        from werkzeug.serving import ThreadedWSGIServer

        class LauncherServer(ThreadedWSGIServer):
            allow_reuse_address = os.name != 'nt'

            def server_bind(self):
                try:
                    if os.name == 'nt':
                        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    super().server_bind()
                except OSError as exc:
                    raise BindFailure(exc) from exc

        try:
            server = LauncherServer(host, port, app, ssl_context=ssl_context)
        except BindFailure as exc:
            raise exc.error from None
        try:
            yield server.server_port, server.serve_forever
        finally:
            server.server_close()
    elif mode == 'eventlet':
        import eventlet
        import eventlet.wsgi
        from eventlet.green import socket as green_socket

        address = green_socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        listener = green_socket.socket(address[0], socket.SOCK_STREAM)
        try:
            if os.name == 'nt':
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address[4])
            listener.listen(128)
            if ssl_context is not None:
                if isinstance(ssl_context, tuple):
                    listener = eventlet.wrap_ssl(listener, certfile=ssl_context[0],
                                                 keyfile=ssl_context[1], server_side=True)
                else:
                    listener = ssl_context.wrap_socket(listener, server_side=True)
            yield listener.getsockname()[1], lambda: eventlet.wsgi.server(listener, app, log_output=False)
        finally:
            listener.close()
    elif mode == 'gevent':
        from gevent import pywsgi

        options = {'log': None}
        try:
            from geventwebsocket.handler import WebSocketHandler
            options['handler_class'] = WebSocketHandler
        except ImportError:
            pass
        if ssl_context is not None:
            if isinstance(ssl_context, tuple):
                options.update(certfile=ssl_context[0], keyfile=ssl_context[1])
            else:
                options['ssl_context'] = ssl_context
        server = pywsgi.WSGIServer((host, port), app, **options)
        socketio.wsgi_server = server
        try:
            server.init_socket()
            yield server.server_port, server.serve_forever
        finally:
            server.close()
    else:
        raise ValueError(f'Unsupported launcher async mode: {mode}')


@contextmanager
def launch_server(app, socketio, host, port, ssl_context, report, *, settings_path=None):
    original_port = port
    with ExitStack() as stack:
        while True:
            try:
                actual_port, serve = stack.enter_context(bound_server(app, socketio, host, port, ssl_context))
                break
            except OSError as exc:
                if not address_in_use(exc):
                    raise
                report(f'[!] Port {port} is already in use on {host}. No existing service was stopped or reused.')
                candidate = suggested_port(host, port)
                if candidate is None:
                    raise RuntimeError('No automatic port is available. Set STANDTERM_PORT to another port.') from None
                report(f'[*] Port {candidate} appears available. Set STANDTERM_PORT={candidate} to select it explicitly.')
                report('[*] A different port uses separate browser storage; existing settings and SSH keys are not moved.')
                if not confirm(f'Use port {candidate} for this launch?'):
                    raise RuntimeError('Startup cancelled. The configured port was not changed.') from None
                port = candidate
        if settings_path is not None and (original_port == 0 or port != original_port):
            if original_port == 0 or confirm(f'Remember port {actual_port} for future shortcut launches?'):
                try:
                    save_port(actual_port, settings_path)
                    report(f'[*] Saved launcher port in {settings_path}. STANDTERM_PORT still takes precedence.')
                except OSError as exc:
                    report(f'[!] Could not save launcher settings; using this port for this launch only: {exc}')
        yield actual_port, serve


def open_browser(url, *, wsl=False):
    if sys.platform == 'win32':
        os.startfile(url)
        return
    if wsl:
        command = ['cmd.exe', '/c', 'start', '', url]
    elif sys.platform == 'darwin':
        command = ['open', url]
    else:
        command = ['xdg-open', url]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
