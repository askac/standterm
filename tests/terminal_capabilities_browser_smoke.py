"""Verify real tmux output through an isolated Core and the shipped browser terminal."""

import os
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap

import agent_browser_smoke as fixture


def send(page, text):
    page.evaluate("text => window.terminalTest.emitSocket('ssh_input', {terminal_id: 'main', data: text})", text)


def client_info(socket_path):
    result = subprocess.run(['tmux', '-S', str(socket_path), 'list-clients', '-F',
                             '#{client_termname}|#{client_termtype}|#{client_termfeatures}'],
                            capture_output=True, text=True)
    return result.stdout.strip()


def run_case(page, directory, name, *, baseline=False, explicit=False, existing=False):
    socket_path = directory / name
    command = ['tmux', '-S', str(socket_path), '-f', '/dev/null']
    if explicit:
        command += ['-T', 'RGB']
    if existing:
        environment = dict(os.environ, TERM='xterm-256color')
        environment.pop('TERMINFO', None)
        environment.pop('TERMINFO_DIRS', None)
        subprocess.run(command + ['new-session', '-d', '-s', 'probe', '/bin/sh'],
                       env=environment, check=True)
    command += ['attach-session', '-t', 'probe'] if existing else ['new-session', '-s', 'probe', '/bin/sh']
    prefix = 'env -u TERMINFO -u TERMINFO_DIRS ' if baseline else ''
    try:
        send(page, prefix + shlex.join(command) + '\r')
        deadline = time.monotonic() + 10
        info = ''
        while time.monotonic() < deadline:
            info = client_info(socket_path)
            if 'StandTerm(' in info:
                break
            page.wait_for_timeout(100)
        fixture.check('StandTerm(' in info, f'{name}: missing real XTVERSION reply: {info}')
        expect_rgb = not baseline or explicit
        fixture.check(('RGB' in info.split('|')[-1].split(',')) == expect_rgb, f'{name}: {info}')
        send(page, "printf '\\033[2J\\033[H\\033[38;2;12;34;56mRGB_PROBE\\033[0m\\n'\r")
        page.wait_for_function("() => window.terminalTest.getActiveTerminalBufferCellsForTest(0)?.slice(0, 9).map(c => c.chars).join('') === 'RGB_PROBE'")
        cell = page.evaluate('() => window.terminalTest.getActiveTerminalBufferCellsForTest(0, false, true)[0]')
        fixture.check((cell['fgRgb'] and cell['fg'] == 0x0c2238) == expect_rgb,
                      f'{name}: RGB preservation mismatch: {cell}')
        print(f'{name}: {info}; rendered cell={cell}', flush=True)
    finally:
        subprocess.run(['tmux', '-S', str(socket_path), 'kill-server'], capture_output=True)
        page.wait_for_timeout(250)


def check_query_replay(context, page, directory):
    script = directory / 'query_probe.py'
    result = directory / 'query_result.json'
    ready = directory / 'query_ready'
    finish = directory / 'query_finish'
    again = directory / 'query_again'
    script.write_text(textwrap.dedent(r'''
        import json, os, select, sys, termios, time, tty
        from pathlib import Path
        fd = os.open('/dev/tty', os.O_RDWR)
        previous = termios.tcgetattr(fd)
        replies = b''
        repeated = False
        try:
            tty.setraw(fd)
            os.write(fd, b'\x1b[>q\x1bP+q524742\x1b\\')
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if select.select([fd], [], [], 0.05)[0]:
                    replies += os.read(fd, 4096)
                if replies.count(b'\x1b\\') >= 2:
                    Path(sys.argv[2]).touch()
                if Path(sys.argv[4]).exists() and not repeated:
                    os.write(fd, b'\x1b[>q\x1bP+q524742\x1b\\')
                    repeated = True
                if Path(sys.argv[3]).exists():
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, previous)
            os.close(fd)
            Path(sys.argv[1]).write_text(json.dumps(replies.decode('ascii')))
    '''))
    send(page, shlex.join([sys.executable, str(script), str(result), str(ready), str(finish), str(again)]) + '\r')
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    fixture.check(ready.exists(), 'live terminal did not answer capability queries')
    second = context.new_page()
    try:
        second.goto(page.url, wait_until='domcontentloaded')
        second.wait_for_function('() => window.terminalTest?.getActiveAgentState()?.connected')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => window.terminalTest?.getActiveAgentState()?.connected')
        again.touch()
        page.wait_for_timeout(500)
        finish.touch()
        deadline = time.monotonic() + 3
        while not result.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        replies = json.loads(result.read_text())
        # Existing xterm DA/color replies use a separate path; count the new protocol replies only.
        fixture.check(replies.count('\x1bP>|StandTerm(') == 2, 'version replies duplicated or missing')
        fixture.check(replies.count('\x1bP1+r524742=38\x1b\\') == 2, 'RGB replies duplicated or missing')
        print('Core capability path: two live query rounds answered once each; refresh/replay silent: ok', flush=True)
    finally:
        finish.touch()
        second.close()


def main():
    if not shutil.which('tmux') or not shutil.which('tic'):
        raise RuntimeError('This smoke requires tmux and ncurses tools.')
    server = None
    try:
        server, url = fixture.start_server()
        with tempfile.TemporaryDirectory(prefix='standterm-tmux-browser-') as directory:
            with fixture.load_playwright()[0]() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context, page = fixture.new_page(browser, url)
                try:
                    run_case(page, Path(directory), 'baseline', baseline=True)
                    run_case(page, Path(directory), 'automatic')
                    run_case(page, Path(directory), 'existing', existing=True)
                    run_case(page, Path(directory), 'explicit', baseline=True, explicit=True)
                    check_query_replay(context, page, Path(directory))
                    fixture.test_terminal_payload_text_is_not_control(browser, url)
                    fixture.test_unicode_provider_keeps_emoji_text_in_separate_cells(browser, url)
                finally:
                    context.close()
                    browser.close()
    finally:
        if server:
            fixture.stop_server(server)


if __name__ == '__main__':
    main()
