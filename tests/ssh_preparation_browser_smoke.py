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


def test_direct_and_saved_routes_keep_independent_drafts(browser, url, known_hosts):
    context, page = new_preparation_page(browser, url)
    try:
        routes_fixture.show_ssh(page)
        assert page.locator('#ssh-use-browser-key-label').is_hidden()
        page.fill('#host', 'direct.test')
        page.fill('#username', 'direct-user')
        page.fill('#password', 'direct-password')
        page.click('#ssh-route-heading')
        assert page.locator('#connectBtn').is_disabled()
        assert page.locator('#ssh-direct-body').is_hidden()
        seed_route(page)
        select_route(page)
        route = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert [node['host'] for node in route['route']] == ['node-0.test', 'node-1.test']
        assert all(not node['password'] for node in route['route'])
        assert page.locator('#ssh-route-body input[type=password]').count() == 0
        assert page.locator('#ssh-forget-host-key').count() == 0
        page.click('#ssh-direct-heading')
        direct = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert direct['host'] == 'direct.test' and direct['username'] == 'direct-user'
        assert direct['password'] == 'direct-password' and 'route' not in direct
        select_route(page)
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Target')
        page.get_by_role('textbox', name='Target Host', exact=True).fill('changed-target.test')
        page.get_by_role('button', name='Save route', exact=True).click()
        page.wait_for_selector('#ssh-route-editor', state='detached')
        assert 'changed-target.test' in page.locator('#ssh-route-path').inner_text()
        assert page.input_value('#host') == 'direct.test'
        assert page.locator('#ssh-direct-body').is_hidden()
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Target')
        page.get_by_role('textbox', name='Target Host', exact=True).fill('cancelled.test')
        page.get_by_role('button', name='Cancel', exact=True).click()
        assert 'changed-target.test' in page.locator('#ssh-route-path').inner_text()
        page.add_style_tag(content='#policy-debug-panel { display: none !important; }')
        page.locator('#controls').screenshot(path='/tmp/standterm-saved-routes.png')
        page.click('#ssh-edit-route')
        page.locator('#ssh-route-editor fieldset').first.get_by_role('button', name='Remove', exact=True).click()
        page.get_by_role('button', name='Save route', exact=True).click()
        page.wait_for_selector('#ssh-route-editor', state='detached')
        assert page.locator('#ssh-direct-body').is_hidden()
        assert '0 jumps' in page.locator('#ssh-route-path').inner_text()
        assert len(page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')['route']) == 1
        page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            state.profiles = [];
            await window.terminalTest.setSshSessionState(state);
        }""")
        assert page.locator('#connectBtn').is_disabled()
        error = page.evaluate("() => window.terminalTest.prepareSshConnectionForTest().then(() => '', error => error.message)")
        assert 'Choose a saved route' in error
    finally:
        fixture.close_context(context)


def test_new_keys_are_atomic_and_cancel_keeps_no_credentials(browser, url, known_hosts):
    context, page = new_preparation_page(browser, url)
    try:
        routes_fixture.show_ssh(page)
        seed_route(page)
        select_route(page)
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Jump 1')
        card = page.locator('#ssh-route-editor fieldset').first
        card.get_by_role('combobox', name='Jump 1 Authentication', exact=True).select_option('browser-key')
        card.get_by_role('button', name='Create key', exact=True).click()
        page.get_by_role('button', name='Save & show public keys', exact=True).wait_for()
        assert card.locator('.ssh-node-public-key').is_hidden()
        assert card.get_by_role('button', name='Copy public key', exact=True).is_hidden()
        assert key_metadata(page) == []
        page.get_by_role('button', name='Cancel', exact=True).click()
        assert key_metadata(page) == []
        page.click('#ssh-edit-route')
        routes_fixture.open_card(page, 'Jump 1')
        card.get_by_role('combobox', name='Jump 1 Authentication', exact=True).select_option('browser-key')
        card.get_by_role('button', name='Create key', exact=True).click()
        page.get_by_role('button', name='Save & show public keys', exact=True).wait_for()
        page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            state.profiles[0].name = 'Changed in another window';
            await window.terminalTest.setSshSessionState(state);
        }""")
        page.get_by_role('button', name='Save & show public keys', exact=True).click()
        page.locator('#ssh-route-editor > [role=status]').get_by_text('SSH settings changed in another window.', exact=False).wait_for()
        assert key_metadata(page) == []
        assert card.locator('.ssh-node-public-key').is_hidden()
        page.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        fixture.close_context(context)


def test_saved_node_keys_survive_reload_and_authenticate_each_hop(browser, url, known_hosts):
    with contextlib.ExitStack() as stack:
        servers = [stack.enter_context(server()) for _ in range(3)]
        context, page = new_preparation_page(browser, url)
        try:
            routes_fixture.show_ssh(page)
            seed_route(page, [item['port'] for item in servers], 'node-key-test')
            select_route(page)
            page.click('#ssh-edit-route')
            for role in ['Jump 1', 'Jump 2', 'Target']:
                routes_fixture.open_card(page, role)
                card = page.locator(f'#ssh-route-editor fieldset[data-role="{role}"]')
                card.get_by_role('combobox', name=f'{role} Authentication', exact=True).select_option('browser-key')
                card.get_by_role('button', name='Create key', exact=True).click()
                card.get_by_text('Save route to keep this key and show its public key.', exact=True).wait_for()
                assert card.locator('.ssh-node-public-key').is_hidden()
            assert key_metadata(page) == []
            page.get_by_role('button', name='Save & show public keys', exact=True).click()
            page.get_by_role('button', name='Close', exact=True).wait_for()
            metadata = key_metadata(page)
            assert len(metadata) == 3 and all(item['owner'] is None and not item['extractable'] for item in metadata)
            state = page.evaluate('() => window.terminalTest.getSshSessionState()')
            assert len(state['profiles']) == 1, 'Creating node keys created fake profiles'
            for index, role in enumerate(['Jump 1', 'Jump 2', 'Target']):
                routes_fixture.open_card(page, role)
                public_key = page.get_by_role('textbox', name=f'{role} Public key', exact=True).input_value()
                assert public_key.startswith('ssh-ed25519 ')
                (servers[index]['root'] / 'authorized_keys').write_text(public_key + '\n')
            page.get_by_role('button', name='Close', exact=True).click()
            # Copies and reordering preserve key IDs; Password only unlinks a node.
            page.click('#ssh-edit-route')
            page.locator('#ssh-route-editor fieldset').first.get_by_role('button', name='Move down', exact=True).click()
            page.locator('#ssh-route-editor fieldset').nth(1).get_by_role('button', name='Move up', exact=True).click()
            routes_fixture.open_card(page, 'Target')
            page.get_by_role('combobox', name='Target Authentication', exact=True).select_option('password')
            page.get_by_role('button', name='Save route', exact=True).click()
            page.wait_for_selector('#ssh-route-editor', state='detached')
            assert key_metadata(page) == metadata
            page.click('#ssh-edit-route')
            routes_fixture.open_card(page, 'Target')
            page.get_by_role('combobox', name='Target Authentication', exact=True).select_option('browser-key')
            target_key = page.evaluate('state => StandTermSshRoutes.checkedPath(state, state.profiles[0]).at(-1).authentication.keyRef.keyId', state)
            page.get_by_role('combobox', name='Target Browser key', exact=True).select_option(target_key)
            page.get_by_role('button', name='Save route', exact=True).click()
            page.wait_for_selector('#ssh-route-editor', state='detached')
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('() => window.terminalTest?.getSocketState().connected')
            page.wait_for_function('() => window.terminalTest.getActiveAgentState()?.connected === true')
            page.click('#new-tab-btn')
            routes_fixture.show_ssh(page)
            select_route(page)
            assert key_metadata(page) == metadata
            page.click('#connectBtn')
            for _ in servers:
                page.locator('#ssh-login-panel').get_by_role('button', name='Trust and continue', exact=True).click()
            page.wait_for_function('() => window.terminalTest.getActiveAgentState().connected === true')
            signatures = page.evaluate('() => window.terminalTest.getEmitted().filter(item => item.event === "ssh_browser_sign_response").map(item => item.args[0])')
            assert len(signatures) == 3 and all(item['status'] == 'ok' and item.get('credential_id') for item in signatures)
            assert len({item['credential_id'] for item in signatures}) == 3
        finally:
            fixture.close_context(context)


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
                                 test_host_fingerprint_management_stays_inside_node_editor]:
                        test(browser, url, known_hosts)
                        print(test.__name__ + ': ok', flush=True)
                finally:
                    browser.close()
        finally:
            fixture.stop_server(proc)


if __name__ == '__main__':
    main()
