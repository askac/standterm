"""Exercise connection modes and saved node credentials with Chromium and SSH."""
import contextlib
import getpass
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ssh_routes_browser_smoke as routes_fixture
from ssh_jump_smoke import server
from terminal_backends.ssh_host_keys import SSHHostKeyStore
import paramiko

fixture = routes_fixture.fixture


def new_preparation_page(browser, url):
    context, page = fixture.new_page(browser, url)
    page.click('#new-tab-btn')
    routes_fixture.show_ssh(page)
    return context, page


def select_route(page, entry='route-a'):
    page.click('#ssh-route-heading')
    page.select_option('#ssh-route-picker', entry)


def seed_route(page, ports=None, alias=''):
    endpoints = [{'host': '127.0.0.1' if ports else f'node-{i}.test', 'port': str(port), 'username': getpass.getuser()}
                 for i, port in enumerate(ports or [22, 22])]
    page.evaluate("""({endpoints, alias}) => window.terminalTest.setSshSessionState({version:2, revision:0,
        profiles:[{id:'route-a',name:'Route A',startNodeId:'node-0'}],history:[],
        nodes:endpoints.map((endpoint,i) => ({id:`node-${i}`, endpoint, authentication:{method:'password'},
            hostKeyAlias:alias ? `${alias}-${i}` : '', nextNodeId:i < endpoints.length-1 ? `node-${i+1}` : null}))})""",
        {'endpoints': endpoints, 'alias': alias})


def key_metadata(page):
    return page.evaluate("""() => new Promise((resolve,reject) => {
        const request = indexedDB.open('standterm-ssh-sessions-v1',2);
        request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction('keys');
            const values = tx.objectStore('keys').getAll();
            tx.oncomplete = () => { db.close(); resolve(values.result.map(key => ({keyId:key.keyId,
                kind:key.kind, owner:key.ownerProfileId, extractable:key.privateKey.extractable}))); };
            tx.onerror = () => reject(tx.error);
        };
    })""")


def finish_editor(page, save=False):
    page.get_by_role('checkbox', name='Save route', exact=True).set_checked(save)
    page.get_by_role('button', name='Done', exact=True).click()
    page.wait_for_selector('#ssh-route-editor', state='detached')


def generate_node_key(page, role):
    if role != 'Direct':
        routes_fixture.open_card(page, role)
    page.get_by_role('checkbox', name=f'{role} Use key', exact=True).check()
    field = page.get_by_role('textbox', name=f'{role} Public key', exact=True)
    page.wait_for_function('(role) => document.querySelector(`[aria-label="${role} Public key"]`).value.startsWith("ssh-ed25519 ")', arg=role)
    return field.input_value()


def test_direct_and_saved_routes_keep_independent_drafts(browser, url, known_hosts):
    context, page = new_preparation_page(browser, url)
    try:
        assert page.locator('#ssh-use-browser-key-label').is_visible()
        page.fill('#host', 'direct.test')
        page.fill('#username', 'direct-user')
        page.fill('#password', 'direct-password')
        page.click('#ssh-route-heading')
        assert page.locator('#connectBtn').is_disabled()
        seed_route(page)
        select_route(page)
        before = page.evaluate('() => window.terminalTest.getSshSessionState()')
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Target')
        page.get_by_role('textbox', name='Target Host', exact=True).fill('changed-target.test')
        finish_editor(page)
        assert page.evaluate('() => window.terminalTest.getSshSessionState()') == before
        assert 'Temporary route' in page.locator('#ssh-route-path').inner_text()
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Target')
        page.get_by_role('textbox', name='Target Host', exact=True).fill('cancelled.test')
        page.get_by_role('button', name='Cancel', exact=True).click()
        route = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert [node['host'] for node in route['route']] == ['node-0.test', 'changed-target.test']
        page.click('#ssh-direct-heading')
        direct = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert direct['host'] == 'direct.test' and direct['password'] == 'direct-password'
        page.click('#ssh-route-heading')
        page.click('#ssh-edit-route')
        page.locator('#ssh-route-editor fieldset').first.get_by_role('button', name='Remove', exact=True).click()
        finish_editor(page, save=True)
        assert '0 jumps' in page.locator('#ssh-route-path').inner_text()
        assert page.evaluate('() => window.terminalTest.getSshSessionState()') == before
        page.evaluate('() => window.terminalTest.captureSshStartsForTest()')
        page.click('#connectBtn')
        page.wait_for_selector('.ssh-login-card')
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert state['profiles'][0]['host'] == 'changed-target.test'
    finally:
        fixture.close_context(context)


def test_new_keys_are_atomic_and_cancel_keeps_no_credentials(browser, url, known_hosts):
    context, page = new_preparation_page(browser, url)
    try:
        seed_route(page)
        select_route(page)
        before = page.evaluate('() => window.terminalTest.getSshSessionState()')
        page.click('#ssh-edit-route')
        first = generate_node_key(page, 'Jump 1')
        assert page.get_by_role('button', name='Copy public key', exact=True).first.is_enabled()
        assert key_metadata(page) == []
        page.get_by_role('button', name='Cancel', exact=True).click()
        assert page.evaluate('() => window.terminalTest.getSshSessionState()') == before
        page.click('#ssh-edit-route')
        retained = generate_node_key(page, 'Jump 1')
        assert retained != first
        finish_editor(page, save=True)
        assert key_metadata(page) == []
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Jump 1')
        assert page.get_by_role('textbox', name='Jump 1 Public key', exact=True).input_value() == retained
        generate_node_key(page, 'Target')
        page.get_by_role('button', name='Cancel', exact=True).click()
        prepared = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert prepared['route'][0]['use_browser_key'] and not prepared['route'][1].get('use_browser_key')
        page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            state.profiles[0].name = 'Changed elsewhere';
            await window.terminalTest.setSshSessionState(state);
        }""")
        page.evaluate('() => window.terminalTest.captureSshStartsForTest()')
        page.click('#connectBtn')
        page.get_by_text('SSH settings changed in another window. Reload before connecting.', exact=True).wait_for()
        assert key_metadata(page) == []
        assert not page.evaluate('() => window.terminalTest.getEmitted().some(item => item.event === "start_ssh" && item.args[0].connection_type === "ssh")')
    finally:
        fixture.close_context(context)


def test_saved_node_keys_survive_reload_and_authenticate_each_hop(browser, url, known_hosts):
    with contextlib.ExitStack() as stack:
        servers = [stack.enter_context(server()) for _ in range(3)]
        context, page = new_preparation_page(browser, url)
        try:
            seed_route(page, [item['port'] for item in servers], 'node-key-test')
            select_route(page)
            page.click('#ssh-edit-route')
            public_keys = [generate_node_key(page, role) for role in ['Jump 1', 'Jump 2', 'Target']]
            for item, key in zip(servers, public_keys):
                (item['root'] / 'authorized_keys').write_text(key + '\n')
            # Reordering and toggling do not rotate the temporary credentials.
            page.locator('#ssh-route-editor fieldset').first.get_by_role('button', name='Move down', exact=True).click()
            page.locator('#ssh-route-editor fieldset').nth(1).get_by_role('button', name='Move up', exact=True).click()
            routes_fixture.open_card(page, 'Target')
            page.get_by_role('checkbox', name='Target Use key', exact=True).uncheck()
            assert page.get_by_role('textbox', name='Target Public key', exact=True).input_value() == public_keys[-1]
            page.get_by_role('checkbox', name='Target Use key', exact=True).check()
            finish_editor(page, save=True)
            assert key_metadata(page) == []
            page.uncheck('#ssh-save-history')
            page.click('#connectBtn')
            page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).wait_for()
            metadata = key_metadata(page)
            assert len(metadata) == 3 and all(item['owner'] is None and not item['extractable'] for item in metadata)
            for _ in servers:
                page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).click()
            page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
            assert len(page.evaluate('async () => (await window.terminalTest.getSshSessionState()).profiles')) == 1
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('() => window.terminalTest?.getSocketState().connected')
            page.wait_for_function('() => window.terminalTest.getActiveAgentState()?.connected === true')
            page.click('#new-tab-btn')
            routes_fixture.show_ssh(page)
            select_route(page)
            assert key_metadata(page) == metadata
            page.click('#connectBtn')
            page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
            signatures = page.evaluate('() => window.terminalTest.getEmitted().filter(item => item.event === "ssh_browser_sign_response").map(item => item.args[0])')
            assert len(signatures) == 3 and all(item['status'] == 'ok' and item.get('credential_id') for item in signatures)
            assert len({item['credential_id'] for item in signatures}) == 3
        finally:
            fixture.close_context(context)


def test_direct_keys_are_temporary_until_connect_saves_the_profile(browser, url, known_hosts):
    with server() as remote:
        context, page = new_preparation_page(browser, url)
        try:
            page.evaluate("""port => window.terminalTest.setSshSessionState({profiles:[{id:'direct-profile',name:'Direct profile',
                host:'127.0.0.1',port:String(port),username:'aska'}],history:[]})""", remote['port'])
            page.evaluate('''async () => {
                const state = await window.terminalTest.getSshSessionState();
                state.nodes[0].hostKeyAlias = 'direct-save-test';
                await window.terminalTest.setSshSessionState(state);
            }''')
            routes_fixture.select(page, 'direct-profile', direct=True)
            key = generate_node_key(page, 'Direct')
            assert page.locator('#password').is_disabled()
            page.uncheck('#ssh-use-browser-key')
            assert page.locator('#password').is_enabled()
            assert page.get_by_role('button', name='Copy public key', exact=True).is_enabled()
            assert page.get_by_role('textbox', name='Direct Public key', exact=True).input_value() == key
            context.grant_permissions(['clipboard-read', 'clipboard-write'])
            page.get_by_role('button', name='Copy public key', exact=True).click()
            assert page.evaluate('() => navigator.clipboard.readText()') == key
            page.check('#ssh-use-browser-key')
            assert key_metadata(page) == []
            (remote['root'] / 'authorized_keys').write_text(key + '\n')
            page.uncheck('#ssh-save-history')
            page.check('#ssh-save-session')
            page.locator('#controls').screenshot(path='/tmp/standterm-direct-key-row.png')
            page.click('#connectBtn')
            page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).wait_for()
            assert len(key_metadata(page)) == 1
            state = page.evaluate('() => window.terminalTest.getSshSessionState()')
            assert len(state['profiles']) == 1 and state['profiles'][0]['id'] == 'direct-profile'
            page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).click()
            page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
            assert page.evaluate('() => window.terminalTest.getSshSessionState()') == state
            page.click('#new-tab-btn')
            routes_fixture.show_ssh(page)
            routes_fixture.select(page, 'direct-profile', direct=True)
            page.wait_for_function('() => document.getElementById("ssh-use-browser-key").checked')
            assert page.get_by_role('textbox', name='Direct Public key', exact=True).input_value() == key
        finally:
            fixture.close_context(context)


def test_temporary_key_signing_survives_other_tab_and_history_drops_refs(browser, url, known_hosts):
    with server() as remote:
        context, page = new_preparation_page(browser, url)
        try:
            page.fill('#port', str(remote['port']))
            page.locator('#ssh-direct-identity summary').click()
            page.fill('#ssh-direct-alias', 'temporary-key-test')
            first_key = generate_node_key(page, 'Direct')
            (remote['root'] / 'authorized_keys').write_text(first_key + '\n')
            before = page.evaluate('() => window.terminalTest.getSshSessionState()')
            first_tab = page.evaluate('() => window.terminalTest.getTerminalTabsState().activeTerminalId')
            page.click('#connectBtn')
            page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).wait_for()
            assert page.evaluate('() => window.terminalTest.getSshSessionState()') == before
            page.click('#new-tab-btn')
            routes_fixture.show_ssh(page)
            page.fill('#port', str(remote['port']))
            second_key = generate_node_key(page, 'Direct')
            assert first_key != second_key
            page.evaluate('id => window.terminalTest.switchTerminalForTest(id)', first_tab)
            page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).click()
            page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
            page.wait_for_function('async () => (await window.terminalTest.getSshSessionState()).history.length === 1')
            assert key_metadata(page) == []
            state = page.evaluate('() => window.terminalTest.getSshSessionState()')
            assert state['profiles'] == []
            history = page.evaluate('state => StandTermSshRoutes.checkedPath(state,state.history[0])', state)
            assert history[0]['authentication'] == {'method': 'browser-key', 'keyRef': None}
            signatures = page.evaluate('() => window.terminalTest.getEmitted().filter(item => item.event === "ssh_browser_sign_response").map(item => item.args[0])')
            assert len(signatures) == 1 and signatures[0]['status'] == 'ok'
            assert not page.evaluate('() => window.terminalTest.hasPrivateSshWireDataForTest()')
        finally:
            fixture.close_context(context)


def test_temporary_jump_keys_do_not_persist_through_history(browser, url, known_hosts):
    with contextlib.ExitStack() as stack:
        servers = [stack.enter_context(server()) for _ in range(2)]
        context, page = new_preparation_page(browser, url)
        try:
            seed_route(page, [item['port'] for item in servers], 'temporary-route')
            before = page.evaluate('() => window.terminalTest.getSshSessionState()')
            select_route(page)
            page.click('#ssh-edit-route')
            for item, role in zip(servers, ['Jump 1', 'Target']):
                (item['root'] / 'authorized_keys').write_text(generate_node_key(page, role) + '\n')
            finish_editor(page)
            prepared = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
            ids = [item['keyId'] for item in prepared['_signers']]
            page.click('#connectBtn')
            for _ in servers:
                page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).click()
            page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
            page.wait_for_function('async () => (await window.terminalTest.getSshSessionState()).history.length === 1')
            state = page.evaluate('() => window.terminalTest.getSshSessionState()')
            assert state['profiles'] == before['profiles'] and key_metadata(page) == []
            assert all(key_id not in str(state) for key_id in ids)
            history = page.evaluate('state => StandTermSshRoutes.checkedPath(state, state.history[0])', state)
            assert len(history) == 2 and all(node['authentication'] == {'method':'browser-key','keyRef':None} for node in history)
            # This page's history-only save must not stale its retained route draft.
            page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
            assert not page.evaluate('() => window.terminalTest.hasPrivateSshWireDataForTest()')
        finally:
            fixture.close_context(context)


def test_history_waits_for_other_temporary_key_login(browser, url, known_hosts, fail_second=False):
    with contextlib.ExitStack() as stack:
        servers = [stack.enter_context(server()) for _ in range(2)]
        store = SSHHostKeyStore(paramiko, known_hosts)
        store.update(store.snapshot('parallel-key-0', servers[0]['port']), servers[0]['host_key'])
        context, page = new_preparation_page(browser, url)
        try:
            page.evaluate('() => window.terminalTest.captureBrowserSshSignRequestsForTest()')
            tabs, keys = [], []
            for index, item in enumerate(servers):
                if index:
                    page.click('#new-tab-btn')
                    routes_fixture.show_ssh(page)
                page.fill('#port', str(item['port']))
                identity = page.locator('#ssh-direct-identity')
                if identity.get_attribute('open') is None:
                    page.locator('#ssh-direct-identity summary').click()
                page.fill('#ssh-direct-alias', f'parallel-key-{index}')
                public_key = generate_node_key(page, 'Direct')
                if not (index and fail_second):
                    (item['root'] / 'authorized_keys').write_text(public_key + '\n')
                tabs.append(page.evaluate('() => window.terminalTest.getTerminalTabsState().activeTerminalId'))
                keys.append(page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')['key_id'])
                page.click('#connectBtn')
                if index:
                    # Keep the second attempt at host trust while the first signs.
                    # The server allows only one outstanding signer request per browser.
                    page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).wait_for()
                else:
                    page.wait_for_function('() => window.terminalTest.getPendingBrowserSshSignRequestsForTest().length === 1')
            for index, tab in enumerate(tabs):
                page.evaluate('id => window.terminalTest.switchTerminalForTest(id)', tab)
                if index:
                    page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).click()
                    page.wait_for_function('() => window.terminalTest.getPendingBrowserSshSignRequestsForTest().length === 1')
                    if fail_second:
                        page.evaluate('id => window.terminalTest.switchTerminalForTest(id)', tabs[0])
                page.evaluate('id => window.terminalTest.releaseBrowserSshSignRequestForTest(id)', tab)
                page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
                if not index:
                    assert page.evaluate('async () => (await window.terminalTest.getSshSessionState()).history') == []
            page.wait_for_function('async (count) => (await window.terminalTest.getSshSessionState()).history.length === count', arg=1 if fail_second else 2)
            if fail_second:
                page.evaluate('id => window.terminalTest.switchTerminalForTest(id)', tabs[1])
                assert not page.evaluate('() => window.terminalTest.getActiveAgentState().connected')
                assert 'Failed' in page.locator('#ssh-login-panel').inner_text()
            replies = page.evaluate('() => window.terminalTest.getEmitted().filter(item => item.event === "ssh_browser_sign_response").map(item => item.args[0])')
            assert {item['terminal_id']: item['key_id'] for item in replies if item['status'] == 'ok'} == dict(zip(tabs, keys))
            assert key_metadata(page) == []
            assert not page.evaluate('() => window.terminalTest.hasPrivateSshWireDataForTest()')
        finally:
            fixture.close_context(context)


def test_history_flushes_when_background_key_login_fails(browser, url, known_hosts):
    test_history_waits_for_other_temporary_key_login(browser, url, known_hosts, fail_second=True)


def test_host_fingerprint_management_stays_inside_node_editor(browser, url, known_hosts):
    store = SSHHostKeyStore(paramiko, known_hosts)
    store.update(store.snapshot('identity-test-0', 22), paramiko.RSAKey.generate(2048))
    context, page = new_preparation_page(browser, url)
    try:
        routes_fixture.show_ssh(page)
        seed_route(page, alias='identity-test')
        select_route(page)
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Jump 1')
        card = page.locator('#ssh-route-editor fieldset').first
        card.locator('.ssh-host-identity summary').click()
        card.get_by_role('button', name='Forget saved fingerprint…', exact=True).wait_for()
        original = known_hosts.read_bytes()
        card.get_by_role('button', name='Forget saved fingerprint…', exact=True).click()
        page.wait_for_function('() => document.activeElement?.textContent === "Keep fingerprint"')
        assert page.locator('#actionBox').is_hidden()
        assert known_hosts.read_bytes() == original
        card.get_by_role('button', name='Keep fingerprint', exact=True).click()
        card.get_by_role('button', name='Forget saved fingerprint…', exact=True).click()
        card.get_by_role('button', name='Forget now', exact=True).click()
        card.get_by_text('No saved fingerprint.', exact=False).wait_for()
        page.get_by_role('button', name='Cancel', exact=True).click()
        assert store.snapshot('identity-test-0', 22)['keys'] == []
        # A new tab must clear a previous Direct identity and pending confirmation.
        store.update(store.snapshot('direct-identity', 22), paramiko.RSAKey.generate(2048))
        page.click('#ssh-direct-heading')
        direct = page.locator('#ssh-direct-identity')
        direct.locator('summary').click()
        page.fill('#ssh-direct-alias', 'direct-identity')
        direct.get_by_role('button', name='Check saved fingerprint', exact=True).click()
        direct.get_by_role('button', name='Forget saved fingerprint…', exact=True).click()
        direct.get_by_role('button', name='Keep fingerprint', exact=True).wait_for()
        page.click('#new-tab-btn')
        routes_fixture.show_ssh(page)
        assert page.input_value('#ssh-direct-alias') == ''
        assert direct.get_by_role('button', name='Forget now', exact=True).count() == 0
        assert direct.get_by_role('button', name='Forget saved fingerprint…', exact=True).is_hidden()
        payload = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert payload['host_key_alias'] == '' and payload['host'] == '127.0.0.1'
        assert store.snapshot('direct-identity', 22)['keys'], 'Opening a tab confirmed an old Forget action'
    finally:
        fixture.close_context(context)


def main():
    bootstrap = """
import runpy, sys
from terminal_backends.ssh import SSHBridge, SSHBackendPlugin
known_hosts_path = sys.argv[1]
original = SSHBackendPlugin.__init__
def initialize(self, *args, **kwargs):
    original(self, *args, **kwargs)
    self._bridge_kwargs['known_hosts_path'] = known_hosts_path
SSHBackendPlugin.__init__ = initialize
sys.argv = ['app.py'] + sys.argv[2:]
runpy.run_path('app.py', run_name='__main__')
"""
    with tempfile.TemporaryDirectory(prefix='standterm-preparation-browser-') as directory:
        known_hosts = Path(directory) / 'known_hosts'
        original_popen = fixture.subprocess.Popen
        def launch(args, **kwargs):
            if len(args) > 1 and args[1] == 'app.py':
                options = ['--default-connection' if value == '--force-connection' else value for value in args[2:]]
                args = [args[0], '-c', bootstrap, str(known_hosts), *options]
            return original_popen(args, **kwargs)
        with patch.object(fixture.subprocess, 'Popen', side_effect=launch):
            proc, url = fixture.start_server()
        try:
            with fixture.load_playwright()[0]() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for test in [test_direct_and_saved_routes_keep_independent_drafts,
                                 test_new_keys_are_atomic_and_cancel_keeps_no_credentials,
                                 test_saved_node_keys_survive_reload_and_authenticate_each_hop,
                                 test_host_fingerprint_management_stays_inside_node_editor,
                                 test_direct_keys_are_temporary_until_connect_saves_the_profile,
                                 test_temporary_key_signing_survives_other_tab_and_history_drops_refs,
                                 test_temporary_jump_keys_do_not_persist_through_history,
                                 test_history_waits_for_other_temporary_key_login,
                                 test_history_flushes_when_background_key_login_fails]:
                        test(browser, url, known_hosts)
                        print(test.__name__ + ': ok', flush=True)
                finally:
                    browser.close()
        finally:
            fixture.stop_server(proc)


if __name__ == '__main__':
    main()
