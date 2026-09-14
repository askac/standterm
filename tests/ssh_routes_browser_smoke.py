"""Exercise route editing, IndexedDB conflicts, and credential references in Chromium."""
import base64
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_browser_smoke as fixture


def new_page(browser, url):
    context, page = fixture.new_page(browser, url)
    page.click('#new-tab-btn')
    page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
    return context, page


def show_ssh(page):
    page.add_style_tag(content='#debug-hud, #payload-log, #policy-debug-panel { display: none !important; }')
    page.evaluate("""() => {
        const policy = window.terminalTest.getTerminalPolicy();
        policy.force_connection = null;
        policy.default_connection = 'ssh';
        const option = policy.connection_options.find(item => item.connection_type === 'ssh');
        option.allowed = true;
        option.browser_key_allowed = true;
        window.terminalTest.applyTerminalPolicy(policy);
        window.terminalTest.setConnectionTypeForTest('ssh');
        for (const id of ['controls', 'connection-form', 'ssh-fields']) document.getElementById(id).style.display = 'block';
    }""")


def select(page, entry_id, direct=False):
    if not direct:
        page.click('#ssh-route-heading')
        page.select_option('#ssh-route-picker', entry_id)
        return
    page.click('#ssh-direct-heading')
    page.evaluate("""id => {
        document.getElementById('ssh-session-picker-toggle').click();
        document.querySelector(`.ssh-session-picker-entry[data-entry-id="${id}"]`).click();
    }""", entry_id)


def open_card(page, role):
    toggle = page.get_by_role('button', name=f'Edit {role}', exact=True)
    if toggle.get_attribute('aria-expanded') != 'true':
        toggle.click()


def test_ordered_cards_allow_incomplete_drafts_and_move_target(browser, url):
    context, page = new_page(browser, url)
    try:
        show_ssh(page)
        # Native drag_to needs both cards visible; small windows can use Move buttons.
        page.set_viewport_size({'width': 1100, 'height': 1600})
        page.fill('#host', '')
        page.fill('#username', '')
        page.locator('#ssh-route-heading').click()
        page.locator('#ssh-edit-route').click()
        editor = page.locator('#ssh-route-editor')
        cards = editor.locator('fieldset')
        assert cards.count() == 1
        page.get_by_role('button', name='Add jump node', exact=True).click()
        page.get_by_role('button', name='Add jump node', exact=True).click()
        assert cards.locator('legend').all_text_contents() == ['Jump 1', 'Jump 2', 'Target']
        assert not editor.locator(':scope > [role=status]').inner_text()
        cards.last.get_by_role('button', name='Move up', exact=True).click()
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        assert 'Jump 1:' in editor.locator(':scope > [role=status]').inner_text()
        for role, host, user, port in [('Jump 1', 'first.test', 'one', '2221'),
                                       ('Jump 2', 'second.test', 'two', '2222'),
                                       ('Target', 'last.test', 'last', '2223')]:
            open_card(page, role)
            page.get_by_role('textbox', name=f'{role} Host', exact=True).fill(host)
            page.get_by_role('textbox', name=f'{role} Username', exact=True).fill(user)
            page.get_by_role('textbox', name=f'{role} Port', exact=True).fill(port)
        cards.last.locator('.ssh-host-identity summary').click()
        page.get_by_role('textbox', name='Target Host key alias (optional)', exact=True).fill('last-site')
        # Move the Target with a real drag, then move it with keyboard controls.
        page.get_by_role('button', name='Reorder Target', exact=True).drag_to(cards.first)
        assert cards.locator('legend').all_text_contents() == ['Jump 1', 'Jump 2', 'Target']
        assert page.get_by_role('textbox', name='Jump 1 Host', exact=True).input_value() == 'last.test'
        page.get_by_role('button', name='Reorder Jump 1', exact=True).press('ArrowDown')
        assert page.get_by_role('textbox', name='Jump 2 Host', exact=True).input_value() == 'last.test'
        # Removing a new, unfinished card must not leave an invalid hidden node.
        page.get_by_role('button', name='Add jump node', exact=True).click()
        cards.nth(2).get_by_role('button', name='Remove', exact=True).click()
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        page.wait_for_selector('#ssh-route-editor', state='detached')
        page.evaluate('() => window.terminalTest.prepareSshConnectionForTest(true)')
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert len(state['nodes']) == 3, state
        assert state['profiles'][0]['name'] == 'two@second.test'
        assert page.input_value('#host') == ''
        assert page.locator('#ssh-direct-body').is_hidden()
        assert page.locator('#ssh-route-body input[type=password]').count() == 0
        payload = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert [(hop['host'], hop['username'], str(hop['port'])) for hop in payload['route']] == [
            ('first.test', 'one', '2221'), ('last.test', 'last', '2223'), ('second.test', 'two', '2222')], payload
        assert [hop['password'] for hop in payload['route']] == ['', '', '']
        assert [hop['host_key_alias'] for hop in payload['route']] == ['', 'last-site', '']
        assert 'password' not in str(state['nodes'][0]['endpoint'])
    finally:
        fixture.close_context(context)


def test_shared_editor_and_atomic_storage(browser, url):
    context, page = new_page(browser, url)
    try:
        page.evaluate("""async () => {
            await window.terminalTest.setSshSessionState({ profiles: [
                { id:'entry-a', name:'A via B', host:'b.test', port:'22', username:'u' },
                { id:'entry-x', name:'X via B', host:'b.test', port:'22', username:'u' }
            ], history: [] });
            const state = await window.terminalTest.getSshSessionState();
            const tail = state.nodes[0];
            tail.hostKeyAlias = 'test-route-site';
            for (const [index, host] of ['a.test', 'x.test'].entries()) {
                const node = { ...structuredClone(tail), id:`jump-${index}`, endpoint:{...tail.endpoint,host}, nextNodeId:tail.id };
                state.nodes.push(node);
                state.profiles[index].startNodeId = node.id;
            }
            await window.terminalTest.setSshSessionState(state);
        }""")
        show_ssh(page)
        select(page, 'entry-a')
        page.locator('#ssh-route-heading').click()
        page.locator('#ssh-edit-route').click()
        open_card(page, 'Target')
        page.get_by_role('textbox', name='Target Host', exact=True).fill('changed.test')
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        page.wait_for_selector('#ssh-route-editor', state='detached')
        page.evaluate('() => window.terminalTest.prepareSshConnectionForTest(true)')
        result = page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            return state.profiles.map(entry => StandTermSshRoutes.checkedPath(state, entry).map(node => node.endpoint.host));
        }""")
        assert result == [['a.test', 'changed.test'], ['x.test', 'b.test']], result
        page.click('#ssh-edit-route')
        open_card(page, 'Target')
        identity = page.locator('#ssh-route-editor fieldset').last.locator('.ssh-host-identity')
        identity.locator('summary').click()
        page.wait_for_function("() => window.terminalTest.getEmitted().some(item => item.event === 'ssh_host_identity')")
        action = page.evaluate("() => window.terminalTest.getEmitted().filter(item => item.event === 'ssh_host_identity').at(-1).args[0]")
        assert action['host_key_alias'] == 'test-route-site' and action['host'] == 'changed.test', action
        page.get_by_role('button', name='Cancel', exact=True).click()
        page.click('#ssh-direct-heading')
        page.fill('#host', 'modified.test')
        page.locator('#ssh-direct-identity summary').click()
        page.wait_for_function("() => window.terminalTest.getEmitted().filter(item => item.event === 'ssh_host_identity').at(-1)?.args[0].host === 'modified.test'")
        action = page.evaluate("() => window.terminalTest.getEmitted().filter(item => item.event === 'ssh_host_identity').at(-1).args[0]")
        assert action['host_key_alias'] == '', action
        # Two connections to the same IndexedDB must not both commit the same revision.
        result = page.evaluate("""async () => {
            const first = await window.terminalTest.getSshSessionState();
            const second = structuredClone(first);
            first.profiles[0].name = 'first writer';
            second.profiles[0].name = 'second writer';
            const results = await Promise.allSettled([
                StandTermSshRoutes.save(openSshSessionsDb, first), StandTermSshRoutes.save(openSshSessionsDb, second)
            ]);
            const db = await openSshSessionsDb();
            const records = await new Promise((resolve, reject) => {
                const request = db.transaction('state').objectStore('state').getAllKeys();
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
            db.close();
            return { statuses: results.map(item => item.status), reason: results.find(item => item.status === 'rejected')?.reason.code,
                nodeRecords: records.filter(key => String(key).startsWith('routes-v2:node:')).length };
        }""")
        assert sorted(result['statuses']) == ['fulfilled', 'rejected'], result
        assert result['reason'] == 'stale' and result['nodeRecords'] >= 6, result
    finally:
        fixture.close_context(context)


def test_shared_card_scope_tracks_references_and_preserves_stored_nodes(browser, url):
    context, page = new_page(browser, url)
    try:
        page.evaluate("""async () => {
            await window.terminalTest.setSshSessionState({profiles:[
                {id:'a',name:'Entry A',host:'a.test',port:'22',username:'a'},
                {id:'b',name:'Entry B',host:'b.test',port:'22',username:'b'}
            ],history:[]});
            const state = await window.terminalTest.getSshSessionState();
            state.nodes.push({...structuredClone(state.nodes[0]),id:'stored-orphan'});
            await window.terminalTest.setSshSessionState(state);
        }""")
        show_ssh(page)
        select(page, 'a')
        page.locator('#ssh-route-heading').click()
        page.locator('#ssh-edit-route').click()
        editor = page.locator('#ssh-route-editor')
        page.get_by_text('Advanced sharing', exact=True).click()
        page.get_by_role('combobox', name='Edit scope', exact=True).select_option('all')
        notice = editor.locator('p').filter(has_text='Node edits also affect:')
        assert 'Entry B' not in notice.inner_text()
        editor.locator('fieldset details').filter(has=page.get_by_text('Advanced node settings', exact=True)).locator('summary').click()
        page.get_by_role('combobox', name='Target Next route', exact=True).select_option('b')
        page.get_by_role('button', name='Reference route after this node', exact=True).click()
        assert 'Entry B' in notice.inner_text() and notice.is_visible()
        page.get_by_text('Advanced sharing', exact=True).click()
        assert notice.is_visible(), 'Shared-edit notice disappeared with advanced settings'
        open_card(page, 'Target')
        page.get_by_role('textbox', name='Target Host', exact=True).fill('shared-change.test')
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        page.wait_for_selector('#ssh-route-editor', state='detached')
        page.evaluate('() => window.terminalTest.prepareSshConnectionForTest(true)')
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert next(item for item in state['profiles'] if item['id'] == 'b')['host'] == 'shared-change.test'
        assert next(item for item in state['nodes'] if item['id'] == 'stored-orphan')['endpoint']['host'] == 'a.test'
        page.locator('#ssh-edit-route').click()
        page.get_by_text('Advanced sharing', exact=True).click()
        page.get_by_role('combobox', name='Edit scope', exact=True).select_option('all')
        assert 'Entry B' in notice.inner_text()
        editor.locator('fieldset').first.get_by_role('button', name='Move down', exact=True).click()
        assert 'Entry B' not in notice.inner_text(), 'Reordering retained a stale shared-edit warning'
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        page.wait_for_selector('#ssh-route-editor', state='detached')
        page.evaluate('() => window.terminalTest.prepareSshConnectionForTest(true)')
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert next(item for item in state['profiles'] if item['id'] == 'a')['host'] == 'a.test'
        assert next(item for item in state['profiles'] if item['id'] == 'b')['host'] == 'shared-change.test'
        assert next(item for item in state['nodes'] if item['id'] == 'stored-orphan')['endpoint']['host'] == 'a.test'
    finally:
        fixture.close_context(context)


def test_receiver_and_owner_renames_preserve_credentials(browser, url):
    context, page = new_page(browser, url)
    try:
        page.evaluate("""async () => {
            await window.terminalTest.setSshSessionState({profiles:[
                {id:'owner',name:'Owner B',host:'b.test',port:'22',username:'u'}
            ],history:[]});
            await window.terminalTest.createBrowserSshKeyForProfileForTest('owner');
            const state = await window.terminalTest.getSshSessionState();
            const owner = state.profiles[0];
            state.profiles.push({...owner,id:'receiver',name:'Receiver',keyId:null,keyTarget:null});
            await window.terminalTest.setSshSessionState(state);
        }""")
        show_ssh(page)
        select(page, 'receiver')
        payload = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert payload['route'][0]['use_browser_key'] and payload['route'][0]['profile_id'] == 'owner', payload
        assert payload['attempt_id'] and payload['_signers'][0]['nodeId'] == payload['route'][0]['node_id']
        before = page.evaluate('() => window.terminalTest.getSshSessionState()')
        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        page.click('#ssh-profile-list button[data-profile-id="receiver"]')
        page.fill('#ssh-profile-name', 'Receiver renamed')
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').innerText === 'Saved Receiver renamed.'")
        after = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert before['nodes'] == after['nodes'], 'Renaming the receiver modified route nodes'
        page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            const owner = state.profiles.find(item => item.id === 'owner');
            const old = StandTermSshRoutes.checkedPath(state, owner)[0];
            const target = { ...structuredClone(old), id:'target-c', endpoint:{host:'c.test',port:'22',username:'u'},
                authentication:{method:'password'},nextNodeId:null };
            const hop = { ...structuredClone(old), id:'hop-b',nextNodeId:target.id };
            state.nodes.push(hop,target);
            owner.startNodeId = hop.id;
            await window.terminalTest.setSshSessionState(state);
        }""")
        page.click('#ssh-profile-list button[data-profile-id="owner"]')
        page.wait_for_function("() => document.getElementById('ssh-profile-key-enabled').checked")
        page.fill('#ssh-profile-name', 'Owner renamed')
        page.click('#ssh-profile-save')
        page.wait_for_function("() => document.getElementById('ssh-profile-status').innerText === 'Saved Owner renamed.'")
        state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        owner = next(item for item in state['profiles'] if item['id'] == 'owner')
        assert owner['keyTarget']['host'] == 'b.test' and owner['host'] == 'c.test', owner
        assert next(item for item in state['nodes'] if item['id'] == 'target-c')['authentication']['method'] == 'password'
        page.click('#settings-close')
        select(page, 'owner')
        payload = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
        assert payload['route'][0]['profile_id'] == 'owner' and not payload['route'][1].get('use_browser_key'), payload
    finally:
        fixture.close_context(context)


def test_cycle_repair_and_rejected_depth_leave_other_entries_intact(browser, url):
    context, page = new_page(browser, url)
    try:
        page.evaluate("""async () => {
            const state = {version:2,revision:0,history:[],profiles:[
                {id:'entry-a',name:'A to C',startNodeId:'A'}, {id:'entry-b',name:'B to C',startNodeId:'B'}
            ],nodes:['A','B','C'].map((id,index) => ({id,endpoint:{host:id.toLowerCase()+'.test',port:'22',username:'u'},
                authentication:{method:'password'},hostKeyAlias:'',nextNodeId:['B','C',null][index]}))};
            await window.terminalTest.setSshSessionState(state);
        }""")
        show_ssh(page)
        select(page, 'entry-a')
        page.locator('#ssh-route-heading').click()
        page.locator('#ssh-edit-route').click()
        open_card(page, 'Target')
        page.locator('#ssh-route-editor fieldset').last.get_by_text('Advanced node settings', exact=True).click()
        page.get_by_role('combobox', name='Target Next route', exact=True).select_option('entry-b')
        page.locator('#ssh-route-editor fieldset').last.get_by_role('button', name='Reference route after this node', exact=True).click()
        assert page.locator('#ssh-route-editor fieldset').count() == 5
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        page.wait_for_function("() => document.querySelector('#ssh-route-editor > [role=status]').textContent.includes('at most 3')")
        # Reject on Done, retain the draft for review, and leave storage untouched.
        assert page.locator('#ssh-route-editor fieldset').count() == 5
        saved = page.evaluate('() => window.terminalTest.getSshSessionState()')
        assert len(saved['nodes']) == 3
        page.get_by_role('button', name='Cancel', exact=True).click()
        page.locator('#ssh-edit-route').click()
        page.get_by_text('Advanced sharing', exact=True).click()
        page.get_by_role('combobox', name='Edit scope', exact=True).select_option('all')
        open_card(page, 'Target')
        page.locator('#ssh-route-editor fieldset').last.get_by_text('Advanced node settings', exact=True).click()
        page.get_by_role('combobox', name='Target Next route', exact=True).select_option('entry-b')
        page.locator('#ssh-route-editor fieldset').last.get_by_role('button', name='Reference route after this node', exact=True).click()
        status = page.locator('#ssh-route-editor > [role=status]')
        # Ordering a cyclic, truncated view must not silently erase its back edge.
        page.locator('#ssh-route-editor fieldset').last.get_by_role('button', name='Move up', exact=True).click()
        assert 'invalid link' in status.inner_text()
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        assert 'cycle rejected' in status.inner_text()
        assert 'target u@b.test:22' in status.inner_text() and 'target u@c.test:22' in status.inner_text()
        status.get_by_role('button', name='Remove loop:', exact=False).click()
        page.get_by_role('checkbox', name='Save route', exact=True).check()
        page.get_by_role('button', name='Done', exact=True).click()
        page.wait_for_selector('#ssh-route-editor', state='detached')
        page.evaluate('() => window.terminalTest.prepareSshConnectionForTest(true)')
        result = page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            return state.profiles.map(entry => StandTermSshRoutes.checkedPath(state, entry).map(node => node.endpoint.host));
        }""")
        assert result == [['a.test', 'b.test'], ['b.test', 'c.test']], result
    finally:
        fixture.close_context(context)


def test_v1_migration_preserves_old_record_and_private_key(browser, url):
    context, page = new_page(browser, url)
    try:
        original = page.evaluate("""async () => {
            await window.terminalTest.setSshSessionState({profiles:[
                {id:'old-owner',name:'Old owner',host:'old.test',port:'22',username:'u'}
            ],history:[]});
            const key = await window.terminalTest.createBrowserSshKeyForProfileForTest('old-owner');
            const legacy = {version:1,profiles:[{id:'old-owner',name:'Old owner',sortOrder:0,
                host:'old.test',port:'22',username:'u',keyId:key.keyId}],history:[]};
            const db = await openSshSessionsDb();
            await new Promise((resolve,reject) => {
                const tx = db.transaction('state','readwrite');
                const store = tx.objectStore('state');
                const keys = store.getAllKeys();
                keys.onsuccess = () => keys.result.filter(key => String(key).startsWith('routes-v2:')).forEach(key => store.delete(key));
                store.put(legacy,'current');
                tx.oncomplete = resolve;
                tx.onabort = () => reject(tx.error);
            });
            db.close();
            return legacy;
        }""")
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest')
        page.wait_for_function('() => window.terminalTest.getSocketState().connected === true')
        result = page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            const key = await window.terminalTest.getBrowserSshKeyMetadataForTest('old-owner');
            const db = await openSshSessionsDb();
            const old = await new Promise((resolve,reject) => {
                const request = db.transaction('state').objectStore('state').get('current');
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
            db.close();
            return {state,key,old};
        }""")
        assert result['old'] == original, 'Migration overwrote the older Core record'
        assert result['state']['version'] == 2 and result['state']['profiles'][0]['id'] == 'old-owner'
        assert result['key']['keyId'] == original['profiles'][0]['keyId'] and not result['key']['privateKeyExtractable']
        challenge = b'Migrated key remains usable'
        signature = page.evaluate("challenge => window.terminalTest.signBrowserSshChallengeForTest('old-owner',challenge)",
                                  base64.b64encode(challenge).decode())
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(base64.b64decode(result['key']['publicKeyRawB64'])).verify(
            base64.b64decode(signature), challenge)
    finally:
        fixture.close_context(context)


def test_trust_retry_matches_attempt_action_and_revision_without_logging_passwords(browser, url):
    context, page = new_page(browser, url)
    try:
        page.evaluate("""async () => {
            await window.terminalTest.setSshSessionState({profiles:[
                {id:'retry-entry',name:'Retry entry',host:'target.test',port:'22',username:'u'}
            ],history:[]});
        }""")
        show_ssh(page)
        select(page, 'retry-entry', direct=True)
        page.fill('#password', 'password-sentinel-do-not-log')
        page.evaluate("""() => {
            window.terminalTest.captureSshStartsForTest();
            window.terminalTest.clearEmitted();
            document.getElementById('connectBtn').click();
        }""")
        page.wait_for_function("() => window.terminalTest.getEmitted().some(item => item.event === 'start_ssh')")
        start = page.evaluate("() => window.terminalTest.getEmitted().find(item => item.event === 'start_ssh').args[0]")
        attempt = start['attempt_id']
        failure = {'terminal_id': start['terminal_id'], 'message_type': 'connection_error', 'attempt_id': attempt,
                   'action_type': 'confirm_ssh_host_key', 'action_id': 'trust-one', 'message': 'Host key is unknown',
                   'action_message': 'Fingerprint fixture', 'action_question': 'Trust this key?'}
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', failure)
        page.click('#actionYesBtn')
        response = {'terminal_id': start['terminal_id'], 'message_type': 'host_key_result', 'attempt_id': attempt,
                    'action_id': 'trust-one', 'operation': 'confirm', 'action_type': 'confirm_ssh_host_key', 'status': 'success'}
        for change in ({'action_id': 'other-action'}, {'action_type': 'forget_ssh_host_key'}, {'attempt_id': 'older-attempt'}):
            page.evaluate('data => window.terminalTest.handleSshOutput(data)', {**response, **change})
        assert page.evaluate("() => window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh').length") == 1
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', response)
        page.wait_for_function("() => window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh').length === 2")
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', response)
        assert page.evaluate("() => window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh').length") == 2
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', {**failure, 'action_id': 'trust-two'})
        page.click('#actionYesBtn')
        page.evaluate("""async () => {
            const state = await window.terminalTest.getSshSessionState();
            state.profiles[0].name = 'Changed elsewhere';
            await StandTermSshRoutes.save(openSshSessionsDb, state);
        }""")
        page.evaluate('data => window.terminalTest.handleSshOutput(data)', {**response, 'action_id': 'trust-two'})
        page.wait_for_function("() => document.getElementById('ssh-session-message').textContent.includes('changed while confirming')")
        emitted = page.evaluate('() => JSON.stringify(window.terminalTest.getEmitted())')
        assert 'password-sentinel-do-not-log' not in emitted
        assert page.evaluate("() => window.terminalTest.getEmitted().filter(item => item.event === 'start_ssh').length") == 2
    finally:
        fixture.close_context(context)


def main():
    original_popen = fixture.subprocess.Popen
    def launch(args, **kwargs):
        if len(args) > 1 and args[1] == 'app.py':
            args = ['--default-connection' if value == '--force-connection' else value for value in args]
        return original_popen(args, **kwargs)
    with patch.object(fixture.subprocess, 'Popen', side_effect=launch):
        proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for test in (test_ordered_cards_allow_incomplete_drafts_and_move_target,
                             test_shared_editor_and_atomic_storage,
                             test_shared_card_scope_tracks_references_and_preserves_stored_nodes,
                             test_receiver_and_owner_renames_preserve_credentials,
                             test_cycle_repair_and_rejected_depth_leave_other_entries_intact,
                             test_v1_migration_preserves_old_record_and_private_key,
                             test_trust_retry_matches_attempt_action_and_revision_without_logging_passwords):
                    test(browser, url)
                    print(test.__name__ + ': ok', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
