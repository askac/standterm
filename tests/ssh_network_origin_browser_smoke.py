"""Verify one-connection network selection, policy availability and bound SSH retries."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_browser_smoke as fixture
import ssh_routes_browser_smoke as routes


def set_available(page, available):
    page.evaluate('''available => {
        const policy = window.terminalTest.getTerminalPolicy();
        const ssh = policy.connection_options.find(item => item.connection_type === 'ssh');
        ssh.start_fields = ssh.start_fields.filter(field => field.name !== 'network_origin');
        if (available) ssh.start_fields.push({name: 'network_origin', input_type: 'select',
            options: [{value: 'core'}, {value: 'windows'}], default_value: 'core'});
        window.terminalTest.applyTerminalPolicy(policy);
        window.terminalTest.setConnectionTypeForTest('ssh');
    }''', available)


def test_selection_and_route_scope(browser, url, locale):
    context, page = fixture.new_page(browser, url, ui_language=locale)
    try:
        page.click('#new-tab-btn')
        routes.show_ssh(page)
        set_available(page, True)
        assert page.locator('#ssh-network-origin-field').is_visible()
        assert page.input_value('#ssh-network-origin') == 'core'
        assert page.locator('label[for="ssh-network-origin"]').inner_text() == (
            'SSH 網路來源（本次連線）' if locale == 'zh-TW' else 'SSH network source (this connection)')
        page.fill('#host', 'target.test'); page.fill('#username', 'operator')
        direct = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert 'network_origin' not in direct
        page.select_option('#ssh-network-origin', 'windows')
        direct = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert direct['network_origin'] == 'windows'
        page.evaluate('''() => window.terminalTest.setSshSessionState({version:2, revision:0,
            profiles:[{id:'route-a',name:'Route A',startNodeId:'jump'}], history:[], nodes:[
                {id:'jump',endpoint:{host:'jump.test',port:'22',username:'u'},
                    authentication:{method:'password'},hostKeyAlias:'',nextNodeId:'target'},
                {id:'target',endpoint:{host:'target.test',port:'22',username:'u'},
                    authentication:{method:'password'},hostKeyAlias:'',nextNodeId:null}
            ]})''')
        routes.select(page, 'route-a')
        route = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert route['network_origin'] == 'windows'
        assert [node['host'] for node in route['route']] == ['jump.test', 'target.test']
        assert all('network_origin' not in node for node in route['route'])
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert all('network_origin' not in entry for entry in state['profiles'] + state['nodes'])
        set_available(page, False)
        assert page.locator('#ssh-network-origin-field').is_hidden()
        assert page.locator('#ssh-network-origin').is_disabled()
        assert page.input_value('#ssh-network-origin') == 'core'
        route = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert 'network_origin' not in route
    finally:
        fixture.close_context(context)


def test_trust_retry_keeps_the_selected_network(browser, url):
    context, page = routes.new_page(browser, url)
    try:
        routes.show_ssh(page)
        set_available(page, True)
        page.fill('#host', 'target.test'); page.fill('#username', 'operator')
        page.select_option('#ssh-network-origin', 'windows')
        page.evaluate('''() => {
            window.terminalTest.captureSshStartsForTest();
            window.terminalTest.clearEmitted();
            document.getElementById('connectBtn').click();
        }''')
        page.wait_for_function("() => window.terminalTest.getEmitted().some(item => item.event === 'start_ssh')")
        start = page.evaluate("() => window.terminalTest.getEmitted().find(item => item.event === 'start_ssh').args[0]")
        assert start['network_origin'] == 'windows'
        failure = {'terminal_id': start['terminal_id'], 'message_type': 'connection_error',
                   'attempt_id': start['attempt_id'], 'action_type': 'confirm_ssh_host_key',
                   'action_id': 'trust-origin', 'message': 'Host key is unknown',
                   'action_message': 'Fingerprint fixture', 'action_question': 'Trust this key?'}
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', failure)
        page.click('#actionYesBtn')
        response = {'terminal_id': start['terminal_id'], 'message_type': 'host_key_result',
                    'attempt_id': start['attempt_id'], 'action_type': 'confirm_ssh_host_key',
                    'action_id': 'trust-origin', 'operation': 'confirm', 'status': 'success'}
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', response)
        page.wait_for_function("() => window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh').length === 2")
        starts = page.evaluate("() => window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh').map(item => item.args[0])")
        assert [item['network_origin'] for item in starts] == ['windows', 'windows']
        assert starts[0]['route'] == starts[1]['route']
        page.evaluate('data => window.terminalTest.handleSshOutput(data)',
                      {**failure, 'attempt_id': starts[1]['attempt_id'], 'action_id': 'trust-second'})
        page.click('#actionYesBtn')
        # The synthetic retry hides the form; still exercise its change handler
        # before delivering the deliberately delayed trust result.
        page.select_option('#ssh-network-origin', 'core', force=True)
        page.evaluate('data => window.terminalTest.handleSshOutput(data)',
                      {**response, 'attempt_id': starts[1]['attempt_id'], 'action_id': 'trust-second'})
        page.wait_for_timeout(200)
        assert page.evaluate("() => window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh').length") == 2
    finally:
        fixture.close_context(context)


def main():
    process, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for locale in ['en', 'zh-TW']:
                    test_selection_and_route_scope(browser, url, locale)
                test_trust_retry_keeps_the_selected_network(browser, url)
            finally:
                browser.close()
    finally:
        fixture.stop_server(process)
    print('SSH network browser smoke passed: bilingual selector, route scope, unavailable capability and trust retry.')


if __name__ == '__main__':
    main()
