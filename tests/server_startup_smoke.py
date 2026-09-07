"""Launcher conflict handling, persistence, and real HTTP/WebSocket checks."""

from contextlib import contextmanager
import errno
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server_startup as startup


class StartupTests(unittest.TestCase):
    def test_services_parser_protocols_comments_aliases_and_invalid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'services'
            path.write_text('# comment\nssh 22/tcp alias # note\ncustom\t55000/TCP\talias\n'
                            'udp-only 55001/udp\nduplicate 55000/tcp\ninvalid -1/tcp\nzero 0/tcp\n'
                            'overflow 65536/tcp\nnot-a-port bad/tcp\nrange 55002-55004/tcp\n'
                            'missing\nnon-ascii １２/tcp\n' + 'huge ' + '9' * 5000 + '/tcp\n', encoding='utf-8')
            self.assertEqual(startup.service_tcp_ports(path), {22, 55000})
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(startup.service_tcp_ports(Path(directory) / 'absent'), set())

    def test_services_path_follows_the_backend_platform(self):
        with patch.object(startup.sys, 'platform', 'win32'), patch.dict(os.environ, SystemRoot='custom-windows'):
            self.assertEqual(startup.services_path(), Path('custom-windows') / 'System32' / 'drivers' / 'etc' / 'services')
        for platform in ('linux', 'darwin'):
            with patch.object(startup.sys, 'platform', platform):
                self.assertEqual(startup.services_path(), Path('/etc/services'))

    def test_candidates_exclude_builtin_services_and_current_port_before_binding(self):
        with patch.object(startup, 'service_tcp_ports', return_value={55000, 55001}), \
                patch.object(startup, 'PORT_SEARCH_LIMIT', 20000):
            ports = startup.automatic_port_candidates(exclude=(55002,))
        self.assertEqual(len(ports), len(set(ports)))
        self.assertTrue(all(49152 <= port <= 65535 for port in ports))
        self.assertTrue({5000, 6000, 7000, 62078, 55000, 55001, 55002}.isdisjoint(ports))
        self.assertIn(49152, ports)
        self.assertIn(65535, ports)
        with patch.object(startup, 'service_tcp_ports', return_value=set(range(49152, 65536))):
            self.assertEqual(startup.automatic_port_candidates(), [])

    def test_automatic_bind_retries_only_bind_errors_and_holds_selected_listener(self):
        attempts, closed = [], []

        @contextmanager
        def bind(_app, _sio, _host, port, _ssl):
            attempts.append(port)
            if port == 55000:
                raise OSError(errno.EADDRINUSE, 'busy')
            if port == 55001:
                raise OSError(errno.EACCES, 'excluded by OS')
            try:
                yield port, lambda: None
            finally:
                closed.append(port)

        with patch.object(startup, 'automatic_port_candidates', return_value=[55000, 55001, 55002, 55003]), \
                patch.object(startup, '_bound_server', bind):
            with self.assertRaises(OSError):
                with startup.bound_server(None, None, 'localhost', 0, None) as (port, _serve):
                    self.assertEqual(port, 55002)
                    self.assertEqual(closed, [])
                    raise OSError(errno.EADDRINUSE, 'not a bind failure')
        self.assertEqual(attempts, [55000, 55001, 55002])
        self.assertEqual(closed, [55002])

    def test_explicit_port_bypasses_automatic_filter_and_exhaustion_never_saves(self):
        @contextmanager
        def bind(_app, _sio, _host, port, _ssl):
            yield port, lambda: None

        with patch.object(startup, 'automatic_port_candidates') as candidates, patch.object(startup, '_bound_server', bind):
            with startup.bound_server(None, None, 'localhost', 5000, None) as (port, _serve):
                self.assertEqual(port, 5000)
            candidates.assert_not_called()
        with patch.object(startup, 'automatic_port_candidates', return_value=[]), patch.object(startup, 'save_port') as save:
            with self.assertRaises(RuntimeError):
                with startup.launch_server(None, None, 'localhost', 0, None, print, settings_path=Path('unused.json')):
                    self.fail('Exhausted candidate list continued')
            save.assert_not_called()

    def test_settings_precedence_validation_and_allowlisted_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'launcher-settings.json'
            report = []
            launcher = {'STANDTERM_LAUNCHER': '1'}
            self.assertEqual(startup.load_port(5000, launcher, report.append, path), 0)
            startup.save_port(8765, path)
            self.assertEqual(json.loads(path.read_text()), {'version': 1, 'port': 8765})
            self.assertEqual(startup.load_port(5000, launcher, report.append, path), 8765)
            self.assertEqual(startup.load_port(5000, {}, report.append, path), 5000)
            self.assertEqual(startup.load_port(5000, dict(launcher, STANDTERM_PORT='8766'), report.append, path), 8766)
            for value in ('bad', '0', '-1', '65536', ''):
                with self.assertRaises(ValueError):
                    startup.load_port(5000, {'STANDTERM_PORT': value}, report.append, path)
            for data in ('{', '[]', '{"version": 1, "port": true}', '{"version": 2, "port": 80}'):
                path.write_text(data)
                self.assertEqual(startup.load_port(5000, launcher, report.append, path), 0)
            self.assertEqual(len(report), 4)
            with patch.object(startup.os, 'replace', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    startup.save_port(8765, path)
            self.assertEqual(list(Path(directory).glob('.launcher-settings-*')), [])

    def test_confirmation_requires_console_and_explicit_yes(self):
        with patch.object(startup.sys, 'stdin', io.StringIO('yes\n')), patch('builtins.input') as reader:
            self.assertFalse(startup.confirm('Continue?'))
            reader.assert_not_called()
        with patch.object(startup.sys, 'stdin') as console:
            console.isatty.return_value = True
            for answer, expected in [('y', True), ('YES', True), ('', False), ('n', False)]:
                with patch('builtins.input', return_value=answer):
                    self.assertEqual(startup.confirm('Continue?'), expected)
            with patch('builtins.input', side_effect=EOFError):
                self.assertFalse(startup.confirm('Continue?'))

    def test_first_launch_binds_before_saving_and_reuses_port(self):
        from flask import Flask
        from flask_socketio import SocketIO

        app = Flask(__name__)
        sio = SocketIO(app, async_mode='threading')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with patch.object(startup, 'confirm') as confirm:
                with startup.launch_server(app, sio, '127.0.0.1', 0, None, lambda _message: None,
                                           settings_path=path) as (port, _serve):
                    self.assertGreaterEqual(port, startup.AUTOMATIC_PORT_MIN)
                    self.assertNotIn(port, startup.FIXED_TCP_PORTS)
                    self.assertNotIn(port, startup.service_tcp_ports())
                    self.assertEqual(json.loads(path.read_text()), {'version': 1, 'port': port})
                    with socket.create_connection(('127.0.0.1', port), timeout=2):
                        pass
                confirm.assert_not_called()
            loaded = startup.load_port(5000, {'STANDTERM_LAUNCHER': '1'}, print, path)
            self.assertEqual(loaded, port)
            with patch.object(startup, 'save_port') as save:
                with startup.launch_server(app, sio, '127.0.0.1', loaded, None, print, settings_path=path) as (reused, _serve):
                    self.assertEqual(reused, port)
                save.assert_not_called()

    def test_first_launch_save_failure_still_serves_and_bind_failure_never_saves(self):
        @contextmanager
        def bound(*_args):
            yield 45678, lambda: None

        reports = []
        with patch.object(startup, 'bound_server', bound), \
                patch.object(startup, 'save_port', side_effect=OSError('read-only')):
            with startup.launch_server(None, None, 'localhost', 0, None, reports.append,
                                       settings_path=Path('unused.json')) as (port, _serve):
                self.assertEqual(port, 45678)
        self.assertTrue(reports)
        with patch.object(startup, 'bound_server', side_effect=OSError(errno.EACCES, 'denied')), \
                patch.object(startup, 'save_port') as save:
            with self.assertRaises(OSError):
                with startup.launch_server(None, None, 'localhost', 0, None, print, settings_path=Path('unused.json')):
                    self.fail('Failed bind continued')
            save.assert_not_called()

    def test_real_bind_conflict_is_typed_and_releases_listener(self):
        from flask import Flask
        from flask_socketio import SocketIO

        app = Flask(__name__)
        sio = SocketIO(app, async_mode='threading')
        with startup.bound_server(app, sio, '127.0.0.1', 0, None) as (port, _serve):
            with self.assertRaises(OSError) as caught:
                with startup.bound_server(app, sio, '127.0.0.1', port, None):
                    self.fail('Second listener acquired the occupied port')
            self.assertTrue(startup.address_in_use(caught.exception))
            candidate = startup.suggested_port('127.0.0.1', port)
            self.assertNotEqual(candidate, port)
            self.assertGreaterEqual(candidate, startup.AUTOMATIC_PORT_MIN)
            with patch.object(startup.sys, 'stdin', io.StringIO()), self.assertRaises(RuntimeError):
                with startup.launch_server(app, sio, '127.0.0.1', port, None, lambda _message: None):
                    self.fail('Non-interactive conflict reached startup notification')
        with startup.bound_server(app, sio, '127.0.0.1', port, None):
            pass

    def test_retry_race_save_and_no_save(self):
        attempts = []
        closed = []

        @contextmanager
        def factory(_app, _sio, _host, port, _ssl):
            attempts.append(port)
            if len(attempts) < 3:
                raise OSError(errno.EADDRINUSE, 'unrelated display text')
            try:
                yield port, lambda: None
            finally:
                closed.append(port)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            for remember in (False, True):
                attempts.clear()
                with patch.object(startup, 'bound_server', factory), \
                        patch.object(startup, 'suggested_port', side_effect=[5001, 5002]), \
                        patch.object(startup, 'confirm', side_effect=[True, True, remember]):
                    with startup.launch_server(None, None, 'localhost', 5000, None, lambda _message: None,
                                               settings_path=path) as (port, _serve):
                        self.assertEqual(port, 5002)
                self.assertEqual(attempts, [5000, 5001, 5002])
                self.assertEqual(path.exists(), remember)
            self.assertEqual(closed, [5002, 5002])
            self.assertEqual(json.loads(path.read_text())['port'], 5002)

    def test_other_errors_do_not_offer_a_port(self):
        with patch.object(startup, 'bound_server', side_effect=OSError(errno.EACCES, 'Address already in use')), \
                patch.object(startup, 'confirm') as confirm:
            with self.assertRaises(OSError):
                with startup.launch_server(None, None, 'localhost', 5000, None, lambda _message: None):
                    self.fail('Permission failure was ignored')
            confirm.assert_not_called()

    def test_core_notification_order_and_saved_port_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, STANDTERM_AGENT_RUNTIME_DIR=directory,
                       STANDTERM_SESSION_RECOVERY_STORE=str(Path(directory) / 'credentials.json'),
                       STANDTERM_DISABLE_AGENTINFO_CURRENT='1', STANDTERM_ACCESS_UI='off',
                       STANDTERM_ASYNC_MODE='threading', STANDTERM_DISABLE_AUTO_HTTPS='1',
                       STANDTERM_HTTPS='0', STANDTERM_HOST='127.0.0.1')
            result = subprocess.run([sys.executable, __file__, '--core-check', directory],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_decline_and_exhausted_candidates_do_not_change_settings(self):
        for candidate in (5001, None):
            with patch.object(startup, 'bound_server', side_effect=OSError(errno.EADDRINUSE, 'busy')), \
                    patch.object(startup, 'suggested_port', return_value=candidate), \
                    patch.object(startup, 'confirm', return_value=False), \
                    patch.object(startup, 'save_port') as save:
                with self.assertRaises(RuntimeError):
                    with startup.launch_server(None, None, 'localhost', 5000, None, lambda _message: None,
                                               settings_path=Path('unused.json')):
                        self.fail('Unapproved startup continued')
                save.assert_not_called()

    def test_real_http_and_websocket(self):
        import concurrent.futures
        import ssl
        import urllib.request
        from simple_websocket import Client

        for mode in ('threading', 'eventlet'):
            for tls in ('plain', 'tls'):
                with self.subTest(mode=mode, tls=tls):
                    child = subprocess.Popen([sys.executable, '-u', __file__, '--probe', mode, tls],
                                             cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    client = None
                    context = ssl._create_unverified_context()
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                         urllib.request.HTTPSHandler(context=context))
                    try:
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            future = pool.submit(child.stdout.readline)
                            try:
                                frame = json.loads(future.result(timeout=30))
                            except Exception:
                                child.kill()
                                raise AssertionError('Probe server did not publish its bound port') from None
                        scheme = 'https' if tls == 'tls' else 'http'
                        origin = f'{scheme}://127.0.0.1:{frame["port"]}'
                        with opener.open(origin, timeout=5) as response:
                            self.assertEqual(json.load(response), {'ready': True})
                        websocket_url = origin.replace('http', 'ws', 1) + '/socket.io/?EIO=4&transport=websocket'
                        client = Client.connect(websocket_url, ssl_context=context)
                        self.assertTrue(client.receive(timeout=5).startswith('0'))
                        client.send('40')
                        self.assertTrue(client.receive(timeout=5).startswith('40'))
                        client.send('421["echo",{"value":"roundtrip"}]')
                        self.assertEqual(client.receive(timeout=5), '431[{"value":"roundtrip"}]')
                    finally:
                        if client is not None:
                            client.close()
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)
                        child.stdout.close()


def probe_server(mode, tls):
    if mode == 'eventlet':
        import eventlet
        eventlet.monkey_patch()
    from flask import Flask
    from flask_socketio import SocketIO
    from werkzeug.serving import make_ssl_devcert

    app = Flask(__name__)
    sio = SocketIO(app, async_mode=mode)
    app.add_url_rule('/', view_func=lambda: {'ready': True})
    sio.on_event('echo', lambda data: data)
    with tempfile.TemporaryDirectory() as directory:
        context = make_ssl_devcert(str(Path(directory) / 'cert'), host='localhost') if tls == 'tls' else None
        with startup.bound_server(app, sio, '127.0.0.1', 0, context) as (port, serve):
            print(json.dumps({'port': port}), flush=True)
            serve()


def check_core(directory):
    sys.argv = [str(ROOT / 'app.py')]
    import app as core

    path = Path(directory) / 'launcher-settings.json'
    original_bound_server = startup.bound_server
    opened = []
    advertised = []

    @contextmanager
    def no_serve(*args):
        with original_bound_server(*args) as (port, _serve):
            yield port, lambda: None

    def browser_open(_url, **_kwargs):
        # Browser notification happens only while our actual listener is held.
        with socket.create_connection(('127.0.0.1', core.DEFAULT_PORT), timeout=2):
            opened.append(core.DEFAULT_PORT)

    with patch.object(startup, 'LAUNCHER_SETTINGS', path), \
            patch.object(startup, 'bound_server', no_serve), \
            patch.object(startup, 'open_browser', side_effect=browser_open), \
            patch.object(core, 'log_message'), \
            patch.object(core, 'start_console_copy_shortcuts'), \
            patch.object(core, 'start_access_window'), \
            patch.object(core, 'start_windows_proxy_bypass'), \
            patch.object(core, 'write_external_agentinfo_files', side_effect=lambda **_kw: advertised.append(core.DEFAULT_PORT)), \
            patch.dict(os.environ, STANDTERM_LAUNCHER='1', STANDTERM_OPEN_BROWSER='1'):
        os.environ.pop('STANDTERM_PORT', None)
        with patch.object(startup, 'confirm') as confirm:
            assert core.main() == 0
            first = core.DEFAULT_PORT
            assert first > 0
            assert json.loads(path.read_text())['port'] == first
            assert core.main() == 0
            assert core.DEFAULT_PORT == first
            assert opened == advertised == [first, first]
            confirm.assert_not_called()
        opened.clear()
        advertised.clear()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            original_port = occupied.getsockname()[1]
            os.environ['STANDTERM_PORT'] = str(original_port)
            with patch.object(startup, 'confirm', side_effect=[True, True]):
                assert core.main() == 0
            selected = core.DEFAULT_PORT
            assert selected != original_port
            assert json.loads(path.read_text())['port'] == selected
            assert opened == advertised == [selected]
            # Redirected stdin must not publish credentials or open a browser.
            with patch.object(startup.sys, 'stdin', io.StringIO()):
                assert core.main() == 1
            assert opened == advertised == [selected]
        del os.environ['STANDTERM_PORT']
        assert core.main() == 0
        assert opened == advertised == [selected, selected]


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--probe':
        probe_server(*sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == '--core-check':
        check_core(sys.argv[2])
    else:
        unittest.main()
