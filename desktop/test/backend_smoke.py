"""Exercise the desktop control pipe without printing credential material."""

import concurrent.futures
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix='standterm-desktop-smoke-') as temporary:
        env = dict(os.environ,
                   STANDTERM_AGENT_RUNTIME_DIR=str(Path(temporary) / 'runtime'),
                   STANDTERM_SESSION_RECOVERY_STORE=str(Path(temporary) / 'credentials.json'))
        proc = subprocess.Popen(
            [sys.executable, '-u', str(ROOT / 'desktop' / 'backend.py')],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(proc.stdout.readline)
                try:
                    line = future.result(timeout=60)
                except concurrent.futures.TimeoutError:
                    proc.kill()
                    raise AssertionError('Desktop startup timed out') from None
            frame = json.loads(line)
            assert frame['type'] == 'standterm_desktop_ready'
            assert frame['version'] == 1
            assert re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', frame['core_version'])
            assert frame['core_bundle_id'] is None
            assert frame['python_version'] == '.'.join(map(str, sys.version_info[:3]))
            origin = frame['origin']
            assert urllib.parse.urlparse(origin).hostname == '127.0.0.1'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(origin + '/agentinfo', timeout=5) as response:
                agentinfo = json.load(response)
            assert agentinfo['instance_id'] == frame['instance_id']
            assert agentinfo['launch_dir'] == str(ROOT)
            for skill in agentinfo['skills'].values():
                assert skill['available'] is True
                for key in ('path', 'boot_prompt_path', 'install_prompt_path'):
                    assert Path(skill[key]).is_file()
            for helper in agentinfo['scripts'].values():
                assert Path(helper).is_file()
            try:
                opener.open(origin, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError('Unauthenticated desktop access was accepted')
            request = urllib.request.Request(origin + '/launcher/status', headers={
                'X-StandTerm-Launcher-Token': frame['launcher_token'],
            })
            with opener.open(request, timeout=5) as response:
                status = json.load(response)
            assert status['instance_id'] == frame['instance_id']
            assert status['core_version'] == frame['core_version']
            assert status['sessions'] == 1
            request = urllib.request.Request(origin, headers={
                'Cookie': frame['cookie_name'] + '=' + frame['session_token'],
            })
            with opener.open(request, timeout=5) as response:
                assert response.status == 200
                cookie_header = response.headers.get('Set-Cookie', '')
                assert 'HttpOnly' in cookie_header and 'SameSite=Strict' in cookie_header
                html = response.read().decode('utf-8')
                assert frame['session_token'] not in html
                assert 'const useDesktopFloatingWindows = true;' in html
            proc.stdin.close()
            assert proc.wait(timeout=10) == 0
            address = urllib.parse.urlparse(origin)
            with socket.socket() as connection:
                connection.settimeout(2)
                assert connection.connect_ex((address.hostname, address.port)) != 0
            assert not list((Path(temporary) / 'runtime').glob('**/standterm_agentinfo.json'))
            # A remembered port can be rebound, but an occupied one must return
            # a typed conflict without credentials or stopping its current owner.
            with socket.socket() as occupied:
                if os.name == 'nt':
                    occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                occupied.bind(('127.0.0.1', 0))
                occupied.listen()
                busy_port = occupied.getsockname()[1]
                conflict = subprocess.run([sys.executable, '-u', str(ROOT / 'desktop' / 'backend.py'),
                    '--port', str(busy_port)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60)
                assert conflict.returncode == 0
                failure = json.loads(conflict.stdout)
                assert failure['type'] == 'standterm_desktop_bind_error'
                assert failure['code'] == 'address_in_use'
                assert failure['port'] == busy_port
                assert set(failure) == {'type', 'version', 'code', 'port', 'suggested_port'}
                with socket.create_connection(('127.0.0.1', busy_port), timeout=2):
                    pass
            proc = subprocess.Popen([sys.executable, '-u', str(ROOT / 'desktop' / 'backend.py'),
                '--port', str(busy_port)], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(proc.stdout.readline)
                try:
                    reused = json.loads(future.result(timeout=60))
                except concurrent.futures.TimeoutError:
                    proc.kill()
                    raise AssertionError('Fixed-port desktop startup timed out') from None
            assert reused['type'] == 'standterm_desktop_ready'
            assert urllib.parse.urlparse(reused['origin']).port == busy_port
            proc.stdin.close()
            assert proc.wait(timeout=10) == 0
            print('Desktop backend smoke: private handoff, authentication and EOF cleanup passed.')
        finally:
            if proc.poll() is None:
                proc.stdin.close()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == '__main__':
    main()
