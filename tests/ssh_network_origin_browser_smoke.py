"""Verify saved first-hop networks, cross-environment portability and bound SSH retries."""

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
        assert page.locator('#ssh-network-origin').is_hidden()
        assert not page.is_checked('#ssh-network-origin')
        assert page.locator('#ssh-network-origin-field summary').inner_text() == (
            '進階' if locale == 'zh-TW' else 'Advanced')
        collapsed_height = page.locator('#controls').bounding_box()['height']
        page.locator('#ssh-network-origin-field summary').click()
        assert page.locator('#ssh-network-origin').is_visible()
        assert page.locator('#controls').bounding_box()['height'] > collapsed_height
        assert page.locator('label[for="ssh-network-origin"]').inner_text() == (
            '使用 Windows 網路（試用）' if locale == 'zh-TW' else 'Use Windows networking (preview)')
        page.fill('#host', 'target.test'); page.fill('#username', 'operator')
        direct = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert 'network_origin' not in direct
        page.check('#ssh-network-origin')
        page.locator('#ssh-network-origin-field summary').click()
        assert page.locator('#ssh-network-origin').is_hidden()
        assert page.is_checked('#ssh-network-origin')
        direct = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert direct['network_origin'] == 'windows'
        page.evaluate('''() => window.terminalTest.setSshSessionState({version:2, revision:0,
            profiles:[{id:'route-a',name:'Route A',startNodeId:'jump',networkOrigin:'windows'}], history:[], nodes:[
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
        assert state['profiles'][0]['networkOrigin'] == 'windows'
        assert all('networkOrigin' not in node for node in state['nodes'])
        set_available(page, False)
        assert page.locator('#ssh-network-origin-field').is_hidden()
        assert page.is_checked('#ssh-network-origin')
        assert page.locator('#ssh-network-origin').is_disabled()
        route = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert 'network_origin' not in route
        set_available(page, True)
        page.locator('#ssh-network-origin-field').evaluate('(element) => { element.open = true; }')
        page.check('#ssh-network-origin')
        page.uncheck('#ssh-network-origin')
        route = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert 'network_origin' not in route
        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        page.click('#ssh-profile-list [data-profile-id="route-a"]')
        page.wait_for_function("() => !document.getElementById('ssh-profile-edit-route').disabled")
        page.click('#ssh-profile-edit-route')
        network = page.locator('#ssh-route-editor .ssh-network-settings')
        assert network.is_visible()
        assert not network.evaluate('(element) => element.open')
        network.locator('summary').click()
        assert network.locator('input').is_checked()
        network.locator('input').uncheck()
        page.locator('#ssh-route-editor button.primary').click()
        page.locator('#ssh-route-editor').wait_for(state='detached')
        updated = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert updated['profiles'][0]['networkOrigin'] == 'core'
        assert updated['nodes'] == state['nodes']
    finally:
        fixture.close_context(context)


def test_saved_profiles_and_portable_export(browser, url):
    context, page = routes.new_page(browser, url)
    try:
        routes.show_ssh(page)
        set_available(page, True)
        page.fill('#host', 'saved.test')
        page.fill('#username', 'operator')
        page.locator('#ssh-network-origin-field summary').click()
        page.check('#ssh-network-origin')
        page.check('#ssh-save-session')
        page.evaluate('() => window.terminalTest.prepareSshConnectionForTest(true)')
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        profile_id = state['profiles'][0]['id']
        assert state['profiles'][0]['networkOrigin'] == 'windows'
        # Load from IndexedDB again, then simulate a Core without Windows support.
        page.reload()
        page.wait_for_function('() => window.terminalTest && window.terminalTest.getSocketState().connected')
        page.click('#new-tab-btn')
        routes.show_ssh(page)
        set_available(page, False)
        routes.select(page, profile_id, direct=True)
        assert page.is_checked('#ssh-network-origin')
        assert page.locator('#ssh-network-origin-field').is_hidden()
        assert 'network_origin' not in page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        # A name-only edit must preserve the unsupported setting.
        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        page.click(f'#ssh-profile-list [data-profile-id="{profile_id}"]')
        page.wait_for_function("() => !document.getElementById('ssh-profile-save').disabled")
        network = page.locator('#ssh-profile-node .ssh-network-settings')
        assert network.is_hidden()
        assert network.locator('input').is_checked()
        assert network.locator('input').is_disabled()
        page.fill('#ssh-profile-name', 'Portable Windows profile')
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent.startsWith('Saved Portable Windows profile.')")
        page.click('#settings-close')
        envelope = page.evaluate('() => window.terminalTest.createBrowserSettingsEnvelopeForTest()')
        page.evaluate('() => window.terminalTest.setSshSessionState({profiles: [], history: []})')
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_navigation(wait_until='domcontentloaded'):
            page.evaluate('value => window.terminalTest.importBrowserSettingsEnvelopeForTest(value)', envelope)
        page.wait_for_function('() => window.terminalTest && window.terminalTest.getSocketState().connected')
        portable = page.evaluate("""async () => window.terminalTest.decodeBrowserSettingsEnvelopeForTest(
            await window.terminalTest.createBrowserSettingsEnvelopeForTest())""")
        page.click('#new-tab-btn')
        routes.show_ssh(page)
        set_available(page, False)
        assert portable['ssh']['profiles'][0]['networkOrigin'] == 'windows'
        imported = page.evaluate('() => window.terminalTest.getSshSessionState()')
        profile_id = imported['profiles'][0]['id']
        routes.select(page, profile_id, direct=True)
        assert 'network_origin' not in page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        page.check('#ssh-save-session')
        page.evaluate('() => window.terminalTest.prepareSshConnectionForTest(true)')
        assert page.evaluate('() => window.terminalTest.getSshSessionState()')['profiles'][0]['networkOrigin'] == 'windows'
        set_available(page, True)
        start = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert start['network_origin'] == 'windows'
        # Change the saved direct profile through Settings, then reload it.
        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        page.click(f'#ssh-profile-list [data-profile-id="{profile_id}"]')
        page.wait_for_function("() => !document.getElementById('ssh-profile-save').disabled")
        network = page.locator('#ssh-profile-node .ssh-network-settings')
        network.locator('summary').click()
        network.locator('input').uncheck()
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent.startsWith('Saved Portable Windows profile.')")
        page.click('#settings-close')
        routes.select(page, profile_id, direct=True)
        assert not page.is_checked('#ssh-network-origin')
        assert 'network_origin' not in page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
    finally:
        fixture.close_context(context)


def test_history_distinguishes_actual_networks(browser, url):
    context, page = routes.new_page(browser, url)
    try:
        for origin in ['windows', 'core']:
            if origin == 'core':
                page.click('#new-tab-btn')
            routes.show_ssh(page)
            set_available(page, True)
            page.fill('#host', 'same.test')
            page.fill('#username', 'operator')
            page.check('#ssh-save-history')
            page.locator('#ssh-network-origin-field').evaluate('(element) => { element.open = true; }')
            page.locator('#ssh-network-origin').set_checked(origin == 'windows')
            page.evaluate("""() => {
                window.terminalTest.captureSshStartsForTest();
                window.terminalTest.clearEmitted();
                document.getElementById('connectBtn').click();
            }""")
            page.wait_for_function("() => window.terminalTest.getEmitted().some(item => item.event === 'start_ssh')")
            start = page.evaluate("() => window.terminalTest.getEmitted().find(item => item.event === 'start_ssh').args[0]")
            page.evaluate('data => window.terminalTest.handleSshOutput(data)', {
                'terminal_id': start['terminal_id'], 'attempt_id': start['attempt_id'],
                'message_type': 'ssh_connected', 'connection_type': 'ssh',
                'ssh_target': {'host': 'same.test', 'port': '22', 'username': 'operator', 'network_origin': origin}})
            page.wait_for_function("async count => (await window.terminalTest.getSshSessionState()).history.length === count",
                                   arg=1 if origin == 'windows' else 2)
        history = page.evaluate('() => window.terminalTest.getSshSessionState()')['history']
        assert [entry['networkOrigin'] for entry in history] == ['core', 'windows']
        assert history[0]['host'] == history[1]['host']
    finally:
        fixture.close_context(context)


def test_trust_retry_keeps_the_selected_network(browser, url):
    context, page = routes.new_page(browser, url)
    try:
        routes.show_ssh(page)
        set_available(page, True)
        page.fill('#host', 'target.test'); page.fill('#username', 'operator')
        page.locator('#ssh-network-origin-field summary').click()
        page.check('#ssh-network-origin')
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
        page.evaluate('''() => {
            const input = document.getElementById('ssh-network-origin');
            input.checked = false;
            input.dispatchEvent(new Event('change', {bubbles: true}));
        }''')
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
                test_saved_profiles_and_portable_export(browser, url)
                test_history_distinguishes_actual_networks(browser, url)
                test_trust_retry_keeps_the_selected_network(browser, url)
            finally:
                browser.close()
    finally:
        fixture.stop_server(process)
    print('SSH network browser smoke passed: saved profiles, route editing, portable export, capability fallback and trust retry.')


if __name__ == '__main__':
    main()
