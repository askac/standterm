"""Exercise real SSH PTYs and tmux with the shipped xterm in Chromium."""

from pathlib import Path
import queue
import shlex
import subprocess
import threading
import time
from types import MethodType, SimpleNamespace

import agent_browser_smoke as browser_fixture
from ssh_jump_smoke import SSHJumpTests, server
from terminal_backends.base import TerminalBridge
from terminal_capabilities import build_capability_response


def main():
    fixture = SSHJumpTests()
    fixture.setUp()
    try:
        host = fixture.stack.enter_context(server())
        bridge = fixture.bridge([host])
        bridge._ssh_term = 'xterm-256color'
        outputs = queue.Queue()
        bridge.runtime = SimpleNamespace(
            emit_socket=lambda event, payload, **kwargs: outputs.put(payload),
            append_transcript=lambda *args: None, update_headless_mirror=None,
            unregister_bridge=lambda *args: None, sleep=time.sleep,
            max_replay_events=256, max_replay_bytes=65536,
        )
        bridge.emit_output = MethodType(TerminalBridge.emit_output, bridge)
        bridge.attach('test-sid')
        route = fixture.route([host])
        fixture.trust(route, [host])
        success, error = fixture.connect(bridge, route)
        assert success, error
        reader = threading.Thread(target=bridge.read_loop, daemon=True)
        reader.start()
        with browser_fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_script_tag(path=str(browser_fixture.ROOT / 'static/js/xterm.js'))
            page.add_script_tag(path=str(browser_fixture.ROOT / 'static/js/standterm-capabilities.js'))

            def query(payload):
                reply = build_capability_response(payload['kind'], payload.get('names'))
                if reply:
                    bridge.write_capability_response(payload['capability_epoch'], payload['output_seq'],
                                                     payload['query_index'], reply,
                                                     query_identity=(payload['kind'], payload.get('names')))

            page.expose_function('capabilityQuery', query)
            page.expose_function('terminalReply', bridge.write)
            page.evaluate("""() => {
                window.term = new Terminal({ allowProposedApi: true, cols: 80, rows: 24 });
                window.addon = new StandTermCapabilities(query => window.capabilityQuery(query));
                term.loadAddon(addon);
                term.onData(data => window.terminalReply(data));
            }""")

            def pump():
                while not outputs.empty():
                    payload = outputs.get_nowait()
                    if payload.get('message_type') == 'terminal':
                        page.evaluate('p => new Promise(resolve => addon.write(p.data, p, resolve))', payload)
                page.wait_for_timeout(50)

            for explicit in (False, True):
                socket_path = fixture.directory / ('explicit.sock' if explicit else 'plain.sock')
                command = ['tmux', '-S', str(socket_path), '-f', '/dev/null']
                if explicit:
                    command += ['-T', 'RGB']
                command += ['new-session', '-s', 'probe', '/bin/sh']
                try:
                    bridge.write(shlex.join(command) + '\r')
                    deadline = time.monotonic() + 10
                    info = ''
                    while time.monotonic() < deadline:
                        pump()
                        info = subprocess.run(['tmux', '-S', str(socket_path), 'list-clients', '-F',
                                               '#{client_termname}|#{client_termtype}|#{client_termfeatures}'],
                                              capture_output=True, text=True).stdout.strip()
                        if 'StandTerm(' in info:
                            break
                    assert 'StandTerm(' in info, info
                    assert ('RGB' in info.split('|')[-1].split(',')) == explicit, info
                    bridge.write("printf '\\033[2J\\033[H\\033[38;2;12;34;56mRGB_PROBE\\033[0m\\n'\r")
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        pump()
                        cell = page.evaluate("""() => {
                            const c = term.buffer.active.getLine(0).getCell(0);
                            return { text: c.getChars(), fg: c.getFgColor(), rgb: !!c.isFgRGB() };
                        }""")
                        if cell['text'] == 'R':
                            break
                    assert cell['text'] == 'R', cell
                    assert (cell['rgb'] and cell['fg'] == 0x0c2238) == explicit, cell
                    print(f'SSH {"explicit" if explicit else "plain"}: {info}; cell={cell}', flush=True)
                finally:
                    subprocess.run(['tmux', '-S', str(socket_path), 'kill-server'], capture_output=True)
                    for _ in range(5):
                        pump()
            browser.close()
        bridge.close()
        reader.join(timeout=3)
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
