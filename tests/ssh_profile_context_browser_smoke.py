"""Keep the SSH profile editor bound to its current connection context."""
import agent_browser_smoke as fixture
from ssh_routes_browser_smoke import show_ssh, open_card


def set_target(page, host, username, terminal_id='main'):
    page.evaluate("""({host, username, terminalId}) => {
        document.getElementById('host').value = host;
        document.getElementById('port').value = '2222';
        document.getElementById('username').value = username;
        window.terminalTest.applyTerminalListForTest({terminals: [{
            terminal_id: terminalId, connection_type: 'ssh', connected: true,
            terminal_label: 'A display label is not an endpoint',
            ssh_target: {host, port: '2222', username}
        }]});
    }""", {'host': host, 'username': username, 'terminalId': terminal_id})


def open_profiles(page):
    page.click('#quick-settings')
    page.click('.settings-nav-item[data-tab="ssh-sessions"]')
    page.wait_for_function("() => !document.getElementById('ssh-profile-save').disabled")


def editor(page):
    return page.evaluate("""() => ({
        name: document.getElementById('ssh-profile-name').value,
        summary: document.getElementById('ssh-profile-route-summary').textContent,
        saveDisabled: document.getElementById('ssh-profile-save').disabled
    })""")


def test_new_connection_replaces_previous_editor_context(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        page.evaluate('() => window.terminalTest.setSshSessionState({profiles: [], history: []})')
        set_target(page, 'first.example', 'first')
        open_profiles(page)
        page.fill('#ssh-profile-name', 'First profile')
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent === 'Saved First profile. Changes apply to the next connection.'")
        first = page.evaluate('() => window.terminalTest.getSshSessionState()')['profiles'][0]
        page.click('#settings-close')
        set_target(page, 'second.example', 'second')
        open_profiles(page)
        assert editor(page) == {'name': 'second@second.example',
                                'summary': 'New direct session: second@second.example:2222',
                                'saveDisabled': False}, editor(page)
        # Reopening the same context preserves an unfinished draft.
        page.fill('#ssh-profile-name', 'Second draft')
        page.click('#settings-close')
        open_profiles(page)
        assert editor(page)['name'] == 'Second draft'
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent === 'Saved Second draft. Changes apply to the next connection.'")
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert len(state['profiles']) == 2
        assert next(item for item in state['profiles'] if item['id'] == first['id']) == first
        page.click('#settings-close')
        # Restore two tabs from structured server metadata while Quick Connect
        # still contains the second target. Selecting the first must use its own.
        page.evaluate("""() => window.terminalTest.applyTerminalListForTest({terminals: [
            {terminal_id: 'main', connection_type: 'ssh', connected: true, terminal_label: 'Second',
             ssh_target: {host: 'second.example', port: '2222', username: 'second'}},
            {terminal_id: 'first-tab', connection_type: 'ssh', connected: true, terminal_label: 'Unrelated title',
             ssh_target: {host: 'first.example', port: '2222', username: 'first'}}
        ]})""")
        page.click('.terminal-tab[data-terminal-id="first-tab"]')
        open_profiles(page)
        assert 'first@first.example:2222' in editor(page)['summary'], editor(page)
        assert editor(page)['saveDisabled'] is False
        # Explicit profile selection remains an edit until the context changes.
        page.click(f'#ssh-profile-list [data-profile-id="{first["id"]}"]')
        page.fill('#ssh-profile-name', 'Unfinished explicit edit')
        page.click('#settings-close')
        open_profiles(page)
        assert editor(page)['name'] == 'Unfinished explicit edit'
        assert editor(page)['saveDisabled'] is False
    finally:
        fixture.close_context(context)


def saved_state(page):
    return page.evaluate('() => window.terminalTest.getSshSessionState()')


def managed_editor(page):
    page.click('#ssh-profile-edit-route')
    page.locator('#ssh-route-editor').wait_for()
    assert page.get_by_role('checkbox', name='Save route', exact=True).count() == 0


def test_route_management_preserves_full_path_and_failed_draft(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        show_ssh(page)
        page.evaluate("""async () => {
            await window.terminalTest.setSshSessionState({profiles: [
                {id: 'route', name: 'Full route', host: 'target.test', port: '22', username: 'target'}
            ], history: []});
            const state = await window.terminalTest.getSshSessionState();
            const target = state.nodes[0];
            state.nodes.push({...structuredClone(target), id: 'jump',
                endpoint: {host: 'jump.test', port: '2222', username: 'jumper'}, nextNodeId: target.id});
            state.profiles[0].startNodeId = 'jump';
            await window.terminalTest.setSshSessionState(state);
        }""")
        open_profiles(page)
        page.click('#ssh-profile-list [data-profile-id="route"]')
        assert editor(page)['summary'] == 'Core host → jumper@jump.test:2222 → target@target.test:22'
        original = saved_state(page)
        page.fill('#ssh-profile-name', 'Renamed route')
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent === 'Saved Renamed route.'")
        assert saved_state(page)['nodes'] == original['nodes']
        managed_editor(page)
        open_card(page, 'Jump 1')
        page.get_by_label('Jump 1 Host', exact=True).fill('edited.jump')
        open_card(page, 'Target')
        page.get_by_label('Target Host', exact=True).fill('edited.target')
        page.get_by_label('Target Use key', exact=True).check()
        page.wait_for_function("() => document.querySelector('[aria-label=\"Target Public key\"]').value.startsWith('ssh-ed25519 ')")
        public_key = page.get_by_label('Target Public key', exact=True).input_value()
        page.get_by_role('button', name='Save route', exact=True).click()
        page.locator('#ssh-route-editor').wait_for(state='detached')
        state = saved_state(page)
        assert state['profiles'][0]['id'] == 'route'
        assert state['profiles'][0]['host'] == 'edited.target'
        assert 'jumper@edited.jump:2222' in editor(page)['summary']
        managed_editor(page)
        assert page.get_by_label('Jump 1 Host', exact=True).input_value() == 'edited.jump'
        open_card(page, 'Target')
        assert page.get_by_label('Target Use key', exact=True).is_checked()
        assert page.get_by_label('Target Public key', exact=True).input_value() == public_key
        page.get_by_label('Target Host', exact=True).fill('cancelled.target')
        page.get_by_role('button', name='Cancel', exact=True).click()
        assert saved_state(page) == state
        managed_editor(page)
        open_card(page, 'Jump 1')
        page.get_by_label('Jump 1 Host', exact=True).fill('unsaved.jump')
        page.get_by_label('Jump 1 Use key', exact=True).check()
        page.wait_for_function("() => document.querySelector('[aria-label=\"Jump 1 Public key\"]').value.startsWith('ssh-ed25519 ')")
        # Simulate another tab committing while this editor retains its older revision.
        page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            state.profiles[0].name = 'Other window';
            await window.terminalTest.setSshSessionState(state);
        }""")
        concurrent = saved_state(page)
        page.evaluate("""() => {
            const save = StandTermSshRoutes.save;
            StandTermSshRoutes.save = (...args) => {
                StandTermSshRoutes.save = save;
                window.failedKeyIds = args[2].map(change => change.record.keyId);
                return save(...args);
            };
        }""")
        page.get_by_role('button', name='Save route', exact=True).click()
        page.wait_for_function("() => document.querySelector('#ssh-route-editor > [role=status]').textContent.includes('changed in another window')")
        assert saved_state(page) == concurrent
        key_ids = page.evaluate('() => window.failedKeyIds')
        assert len(key_ids) == 1
        assert not page.evaluate('id => window.terminalTest.browserSshKeyRecordExistsForTest(id)', key_ids[0])
        assert page.get_by_label('Jump 1 Host', exact=True).input_value() == 'unsaved.jump'
        assert page.get_by_role('button', name='Save route', exact=True).is_enabled()
        page.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        fixture.close_context(context)


def test_connect_rejects_changed_form_during_storage_load(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        page.click('#new-tab-btn')
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        show_ssh(page)
        page.fill('#host', 'first.test')
        page.fill('#username', 'first')
        page.check('#ssh-save-session')
        original = saved_state(page)
        page.evaluate("""() => {
            const load = StandTermSshRoutes.load;
            StandTermSshRoutes.load = async (...args) => {
                StandTermSshRoutes.load = load;
                await new Promise(resolve => { window.releaseSshLoad = resolve; });
                return load(...args);
            };
            window.pendingPreparation = window.terminalTest.prepareSshConnectionForTest(true)
                .then(() => 'unexpected success', error => error.message);
        }""")
        page.wait_for_function('() => !!window.releaseSshLoad')
        page.fill('#host', 'second.test')
        page.uncheck('#ssh-save-session')
        page.evaluate('() => window.releaseSshLoad()')
        error = page.evaluate('() => window.pendingPreparation')
        assert 'settings changed' in error, error
        assert saved_state(page) == original
    finally:
        fixture.close_context(context)


def inline_fields(page):
    return page.locator('#ssh-profile-node')


def wait_saved(page):
    page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent.endsWith('Changes apply to the next connection.')")


def test_inline_direct_save_preserves_shared_nodes_and_order(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        show_ssh(page)
        page.evaluate("""async () => {
            await window.terminalTest.setSshSessionState({profiles: [
                {id: 'direct', name: 'Direct', host: 'old.test', port: '22', username: 'u'}
            ], history: []});
            const state = await window.terminalTest.getSshSessionState();
            const target = state.nodes[0];
            state.nodes.push({...structuredClone(target), id: 'jump',
                endpoint: {host: 'jump.test', port: '22', username: 'j'}, nextNodeId: target.id});
            state.profiles.push({...state.profiles[0], id: 'shared', name: 'Shared route',
                sortOrder: 1, startNodeId: 'jump'});
            await window.terminalTest.setSshSessionState(state);
        }""")
        original = saved_state(page)
        open_profiles(page)
        page.click('#ssh-profile-list [data-profile-id="direct"]')
        fields = inline_fields(page)
        fields.get_by_label('Target Host', exact=True).fill('edited.test')
        page.fill('#ssh-profile-name', 'Edited Direct')
        fields.locator('summary').click()
        fields.get_by_label('Target Host key alias (optional)', exact=True).fill('lab')
        fields.get_by_label('Target Use key', exact=True).check()
        page.wait_for_function("() => document.querySelector('#ssh-profile-node [aria-label=\"Target Public key\"]').value.startsWith('ssh-ed25519 ')")
        public_key = fields.get_by_label('Target Public key', exact=True).input_value()
        page.click('#ssh-profile-down')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent === 'Profile order updated.'")
        assert fields.get_by_label('Target Host', exact=True).input_value() == 'edited.test'
        page.click('#ssh-profile-save')
        wait_saved(page)
        state = saved_state(page)
        assert [item['id'] for item in state['profiles']] == ['shared', 'direct']
        for old in original['nodes']:
            assert next(node for node in state['nodes'] if node['id'] == old['id']) == old
        path = page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            return StandTermSshRoutes.checkedPath(state, state.profiles.find(p => p.id === 'direct'));
        }""")
        assert path[0]['endpoint']['host'] == 'edited.test' and path[0]['hostKeyAlias'] == 'lab'
        assert path[0]['authentication']['keyRef']['kind'] == 'credential'
        page.click('#settings-close')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest')
        open_profiles(page)
        page.click('#ssh-profile-list [data-profile-id="direct"]')
        fields.get_by_label('Target Host', exact=True).wait_for()
        assert fields.get_by_label('Target Use key', exact=True).is_checked()
        assert fields.get_by_label('Target Public key', exact=True).input_value() == public_key
    finally:
        fixture.close_context(context)


def test_inline_draft_promotes_to_route_without_saving_on_cancel(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        show_ssh(page)
        page.evaluate('() => window.terminalTest.setSshSessionState({profiles: [], history: []})')
        open_profiles(page)
        fields = inline_fields(page)
        page.fill('#ssh-profile-name', 'New draft')
        fields.get_by_label('Target Host', exact=True).fill('target.test')
        fields.get_by_label('Target Username', exact=True).fill('user')
        fields.get_by_label('Target Port', exact=True).fill('2222')
        fields.get_by_label('Target Use key', exact=True).check()
        page.wait_for_function("() => document.querySelector('#ssh-profile-node [aria-label=\"Target Public key\"]').value.startsWith('ssh-ed25519 ')")
        public_key = fields.get_by_label('Target Public key', exact=True).input_value()
        original = saved_state(page)
        page.click('#ssh-profile-edit-route')
        modal = page.locator('#ssh-route-editor')
        assert modal.get_by_label('Entry name', exact=True).input_value() == 'New draft'
        assert modal.get_by_label('Jump 1 Host', exact=True).input_value() == ''
        open_card(page, 'Target')
        assert modal.get_by_label('Target Port', exact=True).input_value() == '2222'
        assert modal.get_by_label('Target Public key', exact=True).input_value() == public_key
        modal.get_by_role('button', name='Cancel', exact=True).click()
        assert saved_state(page) == original
        assert fields.get_by_label('Target Public key', exact=True).input_value() == public_key
        # Incomplete inline edits can be completed after opening the full editor.
        fields.get_by_label('Target Host', exact=True).fill('')
        page.click('#ssh-profile-edit-route')
        modal.get_by_label('Jump 1 Host', exact=True).fill('jump.test')
        modal.get_by_label('Jump 1 Username', exact=True).fill('jumper')
        open_card(page, 'Target')
        modal.get_by_label('Target Host', exact=True).fill('target.test')
        assert modal.get_by_label('Target Public key', exact=True).input_value() == public_key
        modal.get_by_role('button', name='Save route', exact=True).click()
        modal.wait_for(state='detached')
        wait_saved(page)
        state = saved_state(page)
        assert len(state['profiles']) == 1 and state['profiles'][0]['name'] == 'New draft'
        assert len(state['nodes']) == 2, state
        assert fields.is_hidden()
        assert page.locator('#ssh-profile-save').inner_text() == 'Save name'
        assert 'jump.test' in editor(page)['summary'] and 'target.test' in editor(page)['summary']
    finally:
        fixture.close_context(context)


def test_inline_conflict_retains_fields_and_does_not_store_key(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        show_ssh(page)
        page.evaluate("() => window.terminalTest.setSshSessionState({profiles:[{id:'direct',name:'Direct',host:'host.test',port:'22',username:'u'}],history:[]})")
        open_profiles(page)
        page.click('#ssh-profile-list [data-profile-id="direct"]')
        fields = inline_fields(page)
        fields.get_by_label('Target Host', exact=True).fill('unsaved.test')
        fields.get_by_label('Target Use key', exact=True).check()
        page.wait_for_function("() => document.querySelector('#ssh-profile-node [aria-label=\"Target Public key\"]').value.startsWith('ssh-ed25519 ')")
        other = context.new_page()
        other.goto(fixture.debug_url(url), wait_until='domcontentloaded')
        other.wait_for_function('() => !!window.terminalTest')
        other.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            state.profiles[0].name = 'Other window';
            await window.terminalTest.setSshSessionState(state);
        }""")
        concurrent = saved_state(other)
        page.evaluate("""() => {
            const save = StandTermSshRoutes.save;
            StandTermSshRoutes.save = (...args) => {
                StandTermSshRoutes.save = save;
                window.failedKeyIds = args[2].map(change => change.record.keyId);
                return save(...args);
            };
        }""")
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').textContent.includes('changed in another window')")
        assert page.locator('#ssh-profile-save').is_enabled()
        assert fields.get_by_label('Target Host', exact=True).input_value() == 'unsaved.test'
        assert fields.get_by_label('Target Use key', exact=True).is_checked()
        key_ids = page.evaluate('() => window.failedKeyIds')
        assert len(key_ids) == 1
        assert not page.evaluate('id => window.terminalTest.browserSshKeyRecordExistsForTest(id)', key_ids[0])
        other.reload(wait_until='domcontentloaded')
        other.wait_for_function('() => !!window.terminalTest')
        assert saved_state(other) == concurrent
    finally:
        fixture.close_context(context)


if __name__ == '__main__':
    server, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                test_new_connection_replaces_previous_editor_context(browser, url)
                print('SSH profile editor context: PASS', flush=True)
                test_route_management_preserves_full_path_and_failed_draft(browser, url)
                print('SSH route management: PASS', flush=True)
                test_connect_rejects_changed_form_during_storage_load(browser, url)
                print('SSH preparation context: PASS', flush=True)
                test_inline_direct_save_preserves_shared_nodes_and_order(browser, url)
                print('Inline Direct save and references: PASS', flush=True)
                test_inline_draft_promotes_to_route_without_saving_on_cancel(browser, url)
                print('Inline Direct route promotion: PASS', flush=True)
                test_inline_conflict_retains_fields_and_does_not_store_key(browser, url)
                print('Inline Direct save conflict: PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(server)
