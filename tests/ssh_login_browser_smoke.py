"""Drive per-site login cards through real SSH transports in Chromium."""
import contextlib
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ssh_routes_browser_smoke as routes_fixture
from ssh_login_smoke import password_server

fixture = routes_fixture.fixture


def test_real_login_cards_preserve_completed_hops(browser, url):
    with contextlib.ExitStack() as stack:
        servers = [stack.enter_context(password_server(f'login-secret-{i}')) for i in range(3)]
        context, page = fixture.new_page(browser, url)
        try:
            page.set_viewport_size({'width':1280, 'height':900})
            page.add_style_tag(content='#policy-debug-panel { display: none !important; }')
            page.evaluate("""ports => window.terminalTest.setSshSessionState({version:2,revision:0,history:[],
                profiles:[{id:'login-entry',name:'Three-site login',startNodeId:'login-0'}],
                nodes:ports.map((port,i) => ({id:`login-${i}`,endpoint:{host:'127.0.0.1',port:String(port),username:`user-${i}`},
                    authentication:{method:'password'},hostKeyAlias:`browser-login-${i}`,nextNodeId:i<2?`login-${i+1}`:null}))})""",
                [server['port'] for server in servers])
            routes_fixture.show_ssh(page)
            routes_fixture.select(page, 'login-entry')
            page.click('#connectBtn')
            panel = page.locator('#ssh-login-panel')
            try:
                page.wait_for_selector('.ssh-login-card[data-phase="host_key"]', timeout=10000)
            except Exception:
                print('Login panel:', panel.inner_text(), flush=True)
                print('Connection message:', page.locator('#ssh-session-message').inner_text(), flush=True)
                raise
            assert not page.locator('#connection-form').is_visible()
            for i in range(3):
                card = panel.locator(f'.ssh-login-card[data-node-id="login-{i}"]')
                card.locator('button', has_text='Trust and continue').wait_for()
                assert servers[i]['attempts'] == []
                card.get_by_role('button', name='Trust and continue', exact=True).click()
                password = card.locator('input[type=password]')
                password.wait_for()
                page.wait_for_function('(id) => document.activeElement?.closest(".ssh-login-card")?.dataset.nodeId === id', arg=f'login-{i}')
                if i == 1:
                    assert panel.locator('.ssh-login-card[data-node-id="login-0"]').get_attribute('data-phase') == 'authenticated'
                    assert not panel.locator('.ssh-login-card[data-node-id="login-0"] .ssh-login-card-body').is_visible()
                    assert panel.locator('.ssh-login-card[data-node-id="login-2"]').get_attribute('data-phase') == 'waiting'
                    page.locator('#controls').screenshot(path='/tmp/standterm-ssh-login-cards.png')
                    password.fill('wrong-password')
                    card.get_by_role('button', name='Log in', exact=True).click()
                    card.get_by_text('Password was rejected. Try again.', exact=True).wait_for()
                    assert len(servers[0]['attempts']) == 1
                password.fill(f'login-secret-{i}')
                card.get_by_role('button', name='Log in', exact=True).click()
            page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
            assert not panel.is_visible()
            assert [len(server['attempts']) for server in servers] == [1, 2, 1]
            assert [len(server['transports']) for server in servers] == [1, 1, 1]
            diagnostic = page.evaluate('() => JSON.stringify(window.terminalTest.getEmitted())')
            assert 'login-secret-' not in diagnostic and 'wrong-password' not in diagnostic
        finally:
            fixture.close_context(context)


def test_background_and_stale_prompts_cannot_steal_focus(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        routes_fixture.show_ssh(page)
        page.fill('#host', 'example.test')
        page.evaluate('() => window.terminalTest.captureSshStartsForTest()')
        page.click('#connectBtn')
        page.wait_for_selector('.ssh-login-card')
        start = page.evaluate('() => window.terminalTest.getEmitted().filter(item => item.event === "start_ssh").at(-1).args[0]')
        prompt = {'message_type':'ssh_login_prompt', 'terminal_id':'main', 'attempt_id':start['attempt_id'],
                  'node_id':start['route'][0]['node_id'], 'hop':1, 'total':1,
                  'kind':'password', 'phase':'password', 'request_id':'first-prompt'}
        page.click('#new-tab-btn')
        page.fill('#host', 'other-tab.test')
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', prompt)
        assert page.locator('#host').evaluate('(field) => field === document.activeElement')
        assert not page.locator('#ssh-login-panel').is_visible()
        page.evaluate('() => window.terminalTest.switchTerminalForTest("main")')
        page.wait_for_function('() => document.activeElement?.type === "password"')
        page.locator('#ssh-login-panel input').fill('private-draft')
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', {**prompt, 'attempt_id':'older-attempt', 'request_id':'older'})
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', {'message_type':'connection_error', 'terminal_id':'main', 'message':'uncorrelated'})
        assert page.locator('#ssh-login-panel input').input_value() == 'private-draft'
        page.locator('.ssh-login-actions').get_by_role('button', name='Cancel connection', exact=True).click()
        assert not page.locator('#ssh-login-panel').is_visible()
        page.click('#connectBtn')
        page.wait_for_selector('.ssh-login-card')
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', prompt)
        assert page.locator('#ssh-login-panel input').count() == 0
    finally:
        fixture.close_context(context)


def test_local_key_setup_retries_keep_cards_and_original_attempt_binding(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        routes_fixture.show_ssh(page)
        page.evaluate('() => window.terminalTest.captureSshStartsForTest()')
        page.click('#connectBtn')
        page.wait_for_selector('.ssh-login-card')
        def starts():
            return page.evaluate('() => window.terminalTest.getEmitted().filter(item => item.event === "start_ssh" && item.args[0].connection_type === "ssh").map(item => item.args[0])')
        first = starts()[-1]
        failure = {'message_type':'connection_error', 'terminal_id':'main', 'attempt_id':first['attempt_id'],
                   'action_type':'offer_localhost_key_setup', 'action_id':'setup-a',
                   'action_message':'Authorize local key', 'action_question':'Set up local key?', 'message':'Local key not authorized'}
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', failure)
        page.click('#actionYesBtn')
        result = {'message_type':'setup_result', 'terminal_id':'main', 'attempt_id':first['attempt_id'],
                  'action_id':'setup-a', 'setup_status':'success'}
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', result)
        second = starts()[-1]
        assert second['attempt_id'] != first['attempt_id']
        assert second['route'] == first['route']
        assert page.locator('#ssh-login-panel').is_visible() and not page.locator('#connection-form').is_visible()
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', result)
        assert len(starts()) == 2
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', {**failure, 'attempt_id':second['attempt_id'], 'action_id':'setup-b'})
        page.click('#actionYesBtn')
        page.click('#new-tab-btn')
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', {**result, 'attempt_id':second['attempt_id'], 'action_id':'setup-b'})
        assert len(starts()) == 2, 'A background setup result started another connection'
        page.evaluate('() => window.terminalTest.switchTerminalForTest("main")')
        page.locator('.ssh-login-actions').get_by_role('button', name='Back to connection settings', exact=True).click()
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', {**result, 'attempt_id':second['attempt_id'], 'action_id':'setup-b'})
        assert len(starts()) == 2
    finally:
        fixture.close_context(context)


def main():
    # Isolate known_hosts without changing the user's account or production config.
    bootstrap = """
import runpy, sys
from terminal_backends.ssh import SSHBridge
known_hosts_path = sys.argv[1]
original = SSHBridge.__init__
def initialize(self, *args, **kwargs):
    kwargs['known_hosts_path'] = known_hosts_path
    original(self, *args, **kwargs)
SSHBridge.__init__ = initialize
sys.argv = ['app.py'] + sys.argv[2:]
runpy.run_path('app.py', run_name='__main__')
"""
    with tempfile.TemporaryDirectory(prefix='standterm-login-browser-') as directory:
        original_popen = fixture.subprocess.Popen
        def launch(args, **kwargs):
            if len(args) > 1 and args[1] == 'app.py':
                options = ['--default-connection' if value == '--force-connection' else value for value in args[2:]]
                args = [args[0], '-c', bootstrap, str(Path(directory) / 'known_hosts'), *options]
            return original_popen(args, **kwargs)
        with patch.object(fixture.subprocess, 'Popen', side_effect=launch):
            proc, url = fixture.start_server()
        try:
            with fixture.load_playwright()[0]() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for test in (test_real_login_cards_preserve_completed_hops, test_background_and_stale_prompts_cannot_steal_focus,
                                 test_local_key_setup_retries_keep_cards_and_original_attempt_binding):
                        test(browser, url)
                        print(test.__name__ + ': ok', flush=True)
                finally:
                    browser.close()
        finally:
            fixture.stop_server(proc)


if __name__ == '__main__':
    main()
