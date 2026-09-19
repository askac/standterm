"""Check localized SSH editing, persistence boundaries, and deletion scope."""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_browser_smoke as fixture
import ssh_profile_context_browser_smoke as profiles_fixture
import ssh_routes_browser_smoke as routes_fixture
from connection_i18n_browser_smoke import new_module_page


ENTRY_NAME = 'Entry <b>& {name}'


def seed_state():
    def node(node_id, host, next_id=None):
        return {'id':node_id, 'endpoint':{'host':host, 'port':'22', 'username':'fixture-user'},
                'authentication':{'method':'password'}, 'hostKeyAlias':'', 'nextNodeId':next_id}
    return {'version':2, 'revision':0,
            'profiles':[{'id':'entry-a','name':ENTRY_NAME,'startNodeId':'jump-a'},
                        {'id':'entry-b','name':'Other entry','startNodeId':'jump-b'}],
            'history':[{'id':'history-a','name':'Recent entry','startNodeId':'history-node','lastUsedAt':'2026-09-20T00:00:00Z'}],
            'nodes':[node('jump-a','a.test','shared-target'), node('jump-b','b.test','shared-target'),
                     node('shared-target','target.test'), node('history-node','history.test'), node('orphan','orphan.test')]}


def new_editor_page(browser, url, locale, prepare=False):
    context, page = fixture.new_page(browser, url, ui_language=locale)
    if prepare:
        page.click('#new-tab-btn')
    page.evaluate('() => window.terminalTest.captureSshStartsForTest()')
    page.evaluate('state => window.terminalTest.setSshSessionState(state)', seed_state())
    if prepare:
        routes_fixture.show_ssh(page)
    page.evaluate('() => window.terminalTest.clearEmitted()')
    return context, page


def state(page):
    return page.evaluate('() => window.terminalTest.getSshSessionState()')


def message(page, key, params=None):
    value = page.evaluate("""({key,params}) => {
        const locale = document.documentElement.lang;
        return StandTermI18n.create(StandTermMessages, locale).t(key, params);
    }""", {'key':key,'params':params or {}})
    assert value != key, f'Missing reviewed translation: {key}'
    return value


def edit_host(page, index, host):
    card = page.locator('#ssh-route-editor fieldset').nth(index)
    toggle = card.locator('.ssh-route-card-toggle')
    if toggle.get_attribute('aria-expanded') != 'true':
        toggle.click()
    card.locator('.ssh-route-fields > label > input').first.fill(host)


def finish(page, cancel=False):
    actions = page.locator('#ssh-route-editor .ssh-route-actions')
    actions.locator('button').first.click() if cancel else actions.locator('button.primary').click()
    page.wait_for_selector('#ssh-route-editor', state='detached')


def paths(value):
    nodes = {node['id']:node for node in value['nodes']}
    result = {}
    for entry in value['profiles']:
        path, current = [], entry['startNodeId']
        while current:
            assert current not in [node['id'] for node in path], 'Fixture path contains a cycle'
            path.append(nodes[current])
            current = nodes[current]['nextNodeId']
        result[entry['id']] = path
    return result


def active_connection(page):
    return page.evaluate("""() => ({socket:window.terminalTest.getSocketState(),
        terminal:window.terminalTest.getActiveAgentState()})""")


def test_preparation_done_and_cancel_do_not_persist(browser, url):
    for locale in ['en','zh-TW']:
        context, page = new_editor_page(browser, url, locale, prepare=True)
        try:
            routes_fixture.select(page, 'entry-a')
            before = state(page)
            page.click('#ssh-edit-route')
            editor = page.locator('#ssh-route-editor')
            assert editor.locator('button.primary').inner_text() == message(page, 'ssh.editor.done')
            assert editor.locator('.ssh-route-actions button').first.inner_text() == message(page, 'common.cancel')
            assert editor.locator('legend').all_text_contents() == (
                ['中繼站 1','目標主機'] if locale == 'zh-TW' else ['Jump 1','Target'])
            assert editor.locator('fieldset').evaluate_all('cards => cards.map(card => card.dataset.role)') == ['Jump 1','Target']
            assert editor.locator('.ssh-route-heading input').input_value() == ENTRY_NAME
            assert editor.locator('b').count() == 0
            edit_host(page, 1, 'temporary.test')
            editor.locator('.ssh-route-actions input[type=checkbox]').check()
            finish(page)
            assert state(page) == before, 'Done persisted a preparation draft before Connect'
            prepared = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
            assert [node['host'] for node in prepared['route']] == ['a.test','temporary.test']
            assert state(page) == before
            page.click('#ssh-edit-route')
            edit_host(page, 1, 'cancelled.test')
            finish(page, cancel=True)
            assert state(page) == before, 'Cancel persisted a preparation edit'
            prepared = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
            assert [node['host'] for node in prepared['route']] == ['a.test','temporary.test']
            assert not fixture.get_emitted(page, 'start_ssh'), 'Editing a route started a connection'
        finally:
            fixture.close_context(context)


def test_managed_save_and_stale_draft_preserve_active_connection(browser, url):
    for locale in ['en','zh-TW']:
        context, page = new_editor_page(browser, url, locale)
        try:
            before_connection = active_connection(page)
            profiles_fixture.open_profiles(page)
            page.click('#ssh-profile-list [data-profile-id="entry-a"]')
            original = state(page)
            page.click('#ssh-profile-edit-route')
            assert page.locator('#ssh-route-editor button.primary').inner_text() == message(page, 'ssh.editor.save_route')
            assert page.locator('#ssh-route-editor .ssh-route-actions input[type=checkbox]').count() == 0
            edit_host(page, 1, 'managed.test')
            finish(page)
            saved = state(page)
            assert [entry['id'] for entry in saved['profiles']] == ['entry-a','entry-b']
            assert [node['endpoint']['host'] for node in paths(saved)['entry-a']] == ['a.test','managed.test']
            assert paths(saved)['entry-b'] == paths(original)['entry-b']
            assert all(node in saved['nodes'] for node in original['nodes']), 'Entry-only edit changed existing shared nodes'
            assert active_connection(page) == before_connection, 'Managing a saved route changed the active connection'

            page.click('#ssh-profile-edit-route')
            edit_host(page, 1, 'unsaved.test')
            page.evaluate("""async () => {
                const current = await window.terminalTest.getSshSessionState();
                current.profiles[1].name = 'Concurrent change';
                await window.terminalTest.setSshSessionState(current);
            }""")
            concurrent = state(page)
            page.locator('#ssh-route-editor button.primary').click()
            stale_message = 'SSH settings changed in another window. Reload before saving.'
            page.wait_for_function('expected => document.querySelector("#ssh-route-editor > [role=status]").textContent === expected', arg=stale_message)
            assert state(page) == concurrent, 'Stale editor overwrote a newer stored revision'
            assert page.locator('#ssh-route-editor fieldset').last.locator('.ssh-route-fields > label > input').first.input_value() == 'unsaved.test'
            assert page.locator('#ssh-route-editor button.primary').is_enabled()
            finish(page, cancel=True)
            assert state(page) == concurrent
            assert active_connection(page) == before_connection
        finally:
            fixture.close_context(context)


def test_editor_scope_copy_and_reference_preserve_topology(browser, url):
    for locale in ['en','zh-TW']:
        context, page = new_module_page(browser, locale)
        try:
            page.add_script_tag(path=str(fixture.ROOT / 'static/js/standterm-ssh-route-editor.js'))
            page.evaluate('locale => {document.documentElement.lang = locale;}', locale)
            for scope in ['entry','all']:
                for copied in [False,True]:
                    original = seed_state()
                    original['profiles'][0]['startNodeId'] = 'jump-a'
                    original['profiles'][1]['startNodeId'] = 'jump-b'
                    target_key = page.evaluate('endpoint => StandTermSshRoutes.endpointKey(endpoint)', original['nodes'][1]['endpoint'])
                    original['nodes'][1]['authentication'] = {'method':'browser-key','keyRef':{
                        'kind':'credential','keyId':'fixture-key','targetKey':target_key}}
                    page.evaluate("""original => {
                        window.editorOriginal = original; window.editorResult = null;
                        StandTermSshRoutes.edit({state:original,entryId:'entry-a',target:{},translate:fixtureTranslate,
                            onDone:draft => {editorResult = structuredClone(draft);}});
                    }""", original)
                    editor = page.locator('#ssh-route-editor')
                    editor.locator(':scope > details > summary').click()
                    selector = editor.locator(':scope > details > select')
                    assert selector.locator('option').evaluate_all('items => items.map(item => item.value)') == ['entry','all']
                    assert selector.locator('option').all_text_contents() == [
                        message(page, 'ssh.editor.scope_entry'), message(page, 'ssh.editor.scope_all')]
                    selector.select_option(scope)
                    if scope == 'all':
                        assert ENTRY_NAME in editor.inner_text()
                        assert '{name}' in editor.inner_text() and editor.locator('b').count() == 0
                    edit_host(page, 0, 'changed-a.test')
                    advanced = editor.locator('fieldset').first.locator('details').last
                    advanced.locator('summary').click()
                    advanced.locator('select').last.select_option('entry-b')
                    assert advanced.locator('option[value="entry-a"]').inner_text() == ENTRY_NAME
                    assert editor.locator('b').count() == 0
                    assert advanced.locator('.ssh-route-buttons > button').all_text_contents() == [
                        message(page, 'ssh.editor.reference_route'), message(page, 'ssh.editor.copy_route')]
                    advanced.locator('.ssh-route-buttons > button').nth(1 if copied else 0).click()
                    finish(page)
                    result = page.evaluate('() => editorResult')
                    assert page.evaluate('() => editorOriginal') == original, 'Editor mutated its input snapshot'
                    resolved = paths(result)
                    assert [node['endpoint']['host'] for node in resolved['entry-a']] == ['changed-a.test','b.test','target.test']
                    assert resolved['entry-b'] == paths(original)['entry-b']
                    assert (resolved['entry-a'][0]['id'] == 'jump-a') == (scope == 'all')
                    assert (resolved['entry-a'][1]['id'] == 'jump-b') == (not copied)
                    assert (resolved['entry-a'][2]['id'] == 'shared-target') == (not copied)
                    assert resolved['entry-a'][1]['authentication'] == original['nodes'][1]['authentication']
                    assert next(node for node in result['nodes'] if node['id'] == 'orphan') == original['nodes'][-1]
            for mode in ['prepare','manage']:
                cyclic = seed_state()
                cyclic['nodes'][2]['nextNodeId'] = 'jump-a'
                page.evaluate("""({cyclic,mode}) => {
                    window.editorDoneCalls = 0;
                    StandTermSshRoutes.edit({state:cyclic,entryId:'entry-a',target:{},mode,
                        translate:fixtureTranslate,onDone:() => {editorDoneCalls += 1;}});
                }""", {'cyclic':cyclic,'mode':mode})
                editor = page.locator('#ssh-route-editor')
                completion = message(page, 'ssh.editor.save_route' if mode == 'manage' else 'ssh.editor.done')
                assert editor.locator('button.primary').inner_text() == completion
                editor.locator('fieldset').first.get_by_role('button', name=message(page, 'ssh.editor.move_down'), exact=True).click()
                status = editor.locator(':scope > [role=status]')
                assert status.inner_text() == message(page, 'ssh.editor.review_before_reorder', {'action':completion})
                assert page.evaluate('() => editorDoneCalls') == 0
                editor.locator('button.primary').click()
                assert status.locator('button').count() > 0
                assert page.evaluate('() => editorDoneCalls') == 0, 'Offering cycle repairs called onDone'
                status.locator('button').first.click()
                assert status.inner_text() == message(page, 'ssh.editor.repair_selected', {'action':completion})
                if mode == 'manage':
                    assert message(page, 'ssh.editor.done') not in status.inner_text()
                assert page.evaluate('() => editorDoneCalls') == 0, 'Selecting a repair called onDone before confirmation'
                finish(page, cancel=True)
        finally:
            fixture.close_context(context)


def test_profile_delete_and_history_clear_have_exact_scope(browser, url):
    for locale in ['en','zh-TW']:
        context, page = new_editor_page(browser, url, locale)
        try:
            before_connection = active_connection(page)
            routes_fixture.show_ssh(page)
            profiles_fixture.open_profiles(page)
            page.click('#ssh-profile-list [data-profile-id="entry-a"]')
            page.click('#ssh-profile-edit-route')
            target = page.locator('#ssh-route-editor fieldset').last
            target.locator('.ssh-route-card-toggle').click()
            target.locator('.ssh-key-toggle input').check()
            page.wait_for_function('() => document.querySelector("#ssh-route-editor fieldset:last-of-type .ssh-node-public-key").value.startsWith("ssh-ed25519 ")')
            finish(page)
            original = state(page)
            key_ref = paths(original)['entry-a'][-1]['authentication']['keyRef']
            assert key_ref['kind'] == 'credential'
            assert page.evaluate('keyId => window.terminalTest.browserSshKeyRecordExistsForTest(keyId)', key_ref['keyId'])
            prompts = []
            def dismiss(dialog):
                prompts.append(dialog.message)
                dialog.dismiss()
            def accept(dialog):
                prompts.append(dialog.message)
                dialog.accept()
            page.once('dialog', dismiss)
            page.click('#ssh-profile-delete')
            assert prompts[-1] == message(page, 'ssh.profiles.confirm_delete', {'name':ENTRY_NAME})
            assert state(page) == original, 'Cancelled profile deletion changed storage'
            page.once('dialog', accept)
            page.click('#ssh-profile-delete')
            page.wait_for_function('async () => (await window.terminalTest.getSshSessionState()).profiles.length === 1')
            deleted = state(page)
            assert deleted['profiles'] == [original['profiles'][1]]
            assert deleted['history'] == original['history'] and deleted['nodes'] == original['nodes']
            page.once('dialog', dismiss)
            page.click('#ssh-history-clear')
            assert prompts[-1] == message(page, 'ssh.profiles.confirm_clear')
            assert state(page) == deleted, 'Cancelled history clearing changed storage'
            page.once('dialog', accept)
            page.click('#ssh-history-clear')
            page.wait_for_function('async () => (await window.terminalTest.getSshSessionState()).history.length === 0')
            cleared = state(page)
            assert cleared['history'] == []
            assert cleared['profiles'] == deleted['profiles'] and cleared['nodes'] == deleted['nodes']
            assert page.evaluate('keyId => window.terminalTest.browserSshKeyRecordExistsForTest(keyId)', key_ref['keyId']), 'Deleting a profile or history deleted an independent credential'
            assert active_connection(page) == before_connection
            assert not any(event['event'] in {'start_ssh','cancel_ssh_start','stop_ssh','ssh_input'} for event in fixture.get_emitted(page))
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
                for test in [test_preparation_done_and_cancel_do_not_persist,
                             test_managed_save_and_stale_draft_preserve_active_connection,
                             test_editor_scope_copy_and_reference_preserve_topology,
                             test_profile_delete_and_history_clear_have_exact_scope]:
                    test(browser, url)
                    print(test.__name__ + ': ok', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
