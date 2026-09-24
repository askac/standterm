"""Verify Telnet form policy, bilingual copy and the Windows-origin payload."""

import sys
import socket
import threading
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_browser_smoke as fixture


def test_form(browser, url, locale):
    context, page = fixture.new_page(browser, url, ui_language=locale)
    try:
        page.click('#new-tab-btn')
        page.evaluate("""() => {
            const policy = window.terminalTest.getTerminalPolicy();
            const telnet = policy.connection_options.find(option => option.connection_type === 'telnet');
            telnet.allowed = true;
            telnet.start_fields = telnet.start_fields.filter(field => field.name !== 'network_origin');
            window.terminalTest.applyTerminalPolicy(policy);
            window.terminalTest.setConnectionTypeForTest('telnet');
        }""")
        assert page.locator('#telnet-fields').is_visible()
        assert page.locator('#telnet-network-origin-field').is_hidden()
        assert page.locator('#telnet-network-origin').is_disabled()
        assert page.locator('#telnet-encoding').input_value() == 'utf-8'
        assert page.locator('label[for="telnet-encoding"]').inner_text() == (
            '文字編碼' if locale == 'zh-TW' else 'Encoding')
        page.fill('#telnet-host', 'device.test')
        page.fill('#telnet-port', '2323')
        page.select_option('#telnet-encoding', 'big5')
        form = page.evaluate('() => window.terminalTest.getConnectionFormDataForTest()')
        assert {key: form[key] for key in ('connection_type', 'host', 'port', 'encoding')} == {
            'connection_type': 'telnet', 'host': 'device.test', 'port': '2323', 'encoding': 'big5'}
        assert 'network_origin' not in form
        page.evaluate("""() => {
            const policy = window.terminalTest.getTerminalPolicy();
            const telnet = policy.connection_options.find(option => option.connection_type === 'telnet');
            telnet.start_fields.push({name:'network_origin',value_type:'string',input_type:'select',
                options:[{value:'core'},{value:'windows'}],default_value:'core'});
            window.terminalTest.applyTerminalPolicy(policy);
            window.terminalTest.setConnectionTypeForTest('telnet');
        }""")
        details = page.locator('#telnet-network-origin-field')
        assert details.is_visible()
        assert not details.evaluate('(element) => element.open')
        details.locator('summary').click()
        page.check('#telnet-network-origin')
        form = page.evaluate('() => window.terminalTest.getConnectionFormDataForTest()')
        assert form['network_origin'] == 'windows'
        page.evaluate("""() => {
            const policy = window.terminalTest.getTerminalPolicy();
            const telnet = policy.connection_options.find(option => option.connection_type === 'telnet');
            telnet.start_fields = telnet.start_fields.filter(field => field.name !== 'network_origin');
            window.terminalTest.applyTerminalPolicy(policy);
            window.terminalTest.setConnectionTypeForTest('telnet');
        }""")
        assert details.is_hidden()
        assert 'network_origin' not in page.evaluate('() => window.terminalTest.getConnectionFormDataForTest()')
    finally:
        fixture.close_context(context)


def test_live_connection(browser, url):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    received = []
    done = threading.Event()
    input_received = threading.Event()

    def device():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(30)
                conn.recv(1024)
                conn.sendall(b'DEVICE READY\r\n')
                while not done.is_set():
                    chunk = conn.recv(1024)
                    received.append(chunk)
                    if b'id' in b''.join(received):
                        input_received.set()
                        conn.sendall(b'ID OK\r\n')
                        break
                done.wait(3)
        finally:
            listener.close()

    thread = threading.Thread(target=device, daemon=True)
    thread.start()
    context, page = fixture.new_page(browser, url)
    try:
        page.click('#new-tab-btn')
        page.evaluate("() => window.terminalTest.setConnectionTypeForTest('telnet')")
        page.fill('#telnet-host', '127.0.0.1')
        page.fill('#telnet-port', str(listener.getsockname()[1]))
        page.click('#connectBtn')
        try:
            page.wait_for_function("() => window.terminalTest.getActiveAgentState()?.connected === true", timeout=8000)
        except Exception:
            print(page.evaluate("""() => ({active:window.terminalTest.getActiveAgentState(),
                starts:window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh'),
                error:document.getElementById('errorBox').textContent,
                tabs:window.terminalTest.getTerminalTabsState()})"""), flush=True)
            raise
        page.locator('.terminal-pane.active .xterm-screen').click()
        page.keyboard.type('id')
        page.keyboard.press('Enter')
        assert input_received.wait(3), received
        assert b'id' in b''.join(received)
    finally:
        done.set()
        fixture.close_context(context)
        thread.join(timeout=3)
    assert not thread.is_alive()


def main():
    original_popen = fixture.subprocess.Popen

    def launch(args, **kwargs):
        args = ['--default-connection' if value == '--force-connection' else value for value in args]
        return original_popen(args, **kwargs)

    with patch.object(fixture.subprocess, 'Popen', side_effect=launch):
        process, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for locale in ('en', 'zh-TW'):
                    test_form(browser, url, locale)
                test_live_connection(browser, url)
            finally:
                browser.close()
    finally:
        fixture.stop_server(process)
    print('Telnet browser smoke passed.')


if __name__ == '__main__':
    main()
