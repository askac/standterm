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
    # Newer tmux also detects truecolor from COLORTERM; remove every RGB hint in the negative control.
    prefix = 'env -u TERMINFO -u TERMINFO_DIRS -u COLORTERM ' if baseline else ''
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


def check_tmux_images(browser, url, directory):
    for protocol in ['sixel', 'iterm2', 'all']:
        context, page = fixture.new_page(browser, url)
        socket_path = directory / ('image-' + protocol)
        command = ['env', '-u', 'TMUX', 'tmux', '-S', str(socket_path), '-f', '/dev/null',
                   'new-session', '-s', 'images', '/bin/sh']
        try:
            send(page, shlex.join(command) + '\r')
            deadline = time.monotonic() + 10
            dimensions = []
            while time.monotonic() < deadline:
                result = subprocess.run(['tmux', '-S', str(socket_path), 'display-message', '-p',
                                         '#{client_cell_width}|#{client_cell_height}|#{sixel_support}'],
                                        capture_output=True, text=True)
                dimensions = result.stdout.strip().split('|')
                if len(dimensions) == 3 and all(value.isdigit() and int(value) > 0 for value in dimensions):
                    break
                page.wait_for_timeout(100)
            fixture.check(len(dimensions) == 3 and all(value.isdigit() and int(value) > 0 for value in dimensions),
                          f'{protocol}: tmux needs SIXEL support and nonzero cell pixels: {dimensions}')
            fixture.check('sixel' in client_info(socket_path).split('|')[-1].split(','),
                          f'{protocol}: tmux did not detect SIXEL support')
            result = subprocess.run(['tmux', '-S', str(socket_path), 'display-message', '-p',
                                     '#{window_cell_width}|#{window_cell_height}'],
                                    capture_output=True, text=True, check=True)
            window_dimensions = [int(value) for value in result.stdout.strip().split('|')]
            # tmux scales native SIXEL from its window cells to each client's cells.
            native_pixels = 240 * 96 * int(dimensions[0]) * int(dimensions[1])
            native_pixels /= window_dimensions[0] * window_dimensions[1]
            expected_pixels = native_pixels if protocol == 'sixel' else 240 * 96
            if protocol == 'all':
                expected_pixels += native_pixels
            if protocol != 'sixel':
                subprocess.run(['tmux', '-S', str(socket_path), 'set-option', '-g', 'allow-passthrough', 'on'],
                               check=True)
            send(page, "printf '\\033[2J\\033[HIMAGE_READY\\n'\r")
            page.wait_for_function("""() => window.terminalTest.getActiveTerminalBufferCellsForTest(0)
                ?.slice(0, 11).map(cell => cell.chars).join('') === 'IMAGE_READY'""")
            send(page, "printf '\\033[2J\\033[H'; " + shlex.join(
                ['bash', str(fixture.ROOT / 'scripts' / 'image-check.sh'), '--protocol', protocol]) + '\r')
            name = 'SIXEL' if protocol == 'sixel' else 'iTerm2 PNG'
            page.wait_for_function("""name => Array.from({length: 40}, (_, row) =>
                window.terminalTest.getActiveTerminalBufferCellsForTest(row)
                    ?.map(cell => cell.chars).join('') || '').join('\\n').includes('TEXT AFTER ' + name)
            """, arg=name)
            page.wait_for_function("""expectedPixels => {
                const canvas = document.querySelector('.terminal-pane.active .xterm-image-layer');
                if (!canvas) return false;
                const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
                const colors = [0, 0, 0];
                let opaque = 0;
                for (let offset = 0; offset < pixels.length; offset += 4) {
                    if (pixels[offset + 3] > 240) opaque++;
                    for (let channel = 0; channel < 3; channel++) {
                        if (pixels[offset + channel] > 240 && pixels[offset + (channel + 1) % 3] < 10
                            && pixels[offset + (channel + 2) % 3] < 10 && pixels[offset + 3] > 240)
                            colors[channel]++;
                    }
                }
                return colors.every(count => count > expectedPixels / 20) && opaque > expectedPixels * 0.9;
            }""", arg=expected_pixels)
            state = page.evaluate('window.terminalTest.getTerminalImagesForTest()[0]')
            fixture.check(state['usage'] > 0 and state['mirrorUsage'] > 0, f'{protocol}: {state}')
            fixture.check(state['cursor'] == state['mirrorCursor'], f'{protocol}: mirror cursor mismatch: {state}')
            layout = page.evaluate("""name => {
                const rows = window.terminalTest.getTerminalTabsState().tabs.find(tab => tab.active).rows;
                const textRow = Array.from({length: rows}, (_, row) =>
                    window.terminalTest.getActiveTerminalBufferCellsForTest(row)
                        .map(cell => cell.chars).join('')).findIndex(line => line.includes('TEXT AFTER ' + name));
                const canvas = document.querySelector('.terminal-pane.active .xterm-image-layer');
                const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
                let bottom = 0;
                for (let offset = pixels.length - 4; offset >= 0; offset -= 4) {
                    if (pixels[offset + 3]) {
                        bottom = Math.floor(offset / 4 / canvas.width) + 1;
                        break;
                    }
                }
                return {textTop: textRow * canvas.height / rows, imageBottom: bottom};
            }""", name)
            fixture.check(layout['textTop'] >= layout['imageBottom'], f'{protocol}: text overlaps image: {layout}')
            print(f'tmux {protocol}: cell pixels={dimensions[:2]}; RGB image and Agent mirror rendered: ok', flush=True)
        finally:
            subprocess.run(['tmux', '-S', str(socket_path), 'kill-server'], capture_output=True)
            fixture.close_context(context)


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
                    check_tmux_images(browser, url, Path(directory))
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
