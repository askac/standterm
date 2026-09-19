"""Check localized settings transfer without translating stored data or key material."""
import base64
import io
import json
from pathlib import Path
import re
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_browser_smoke as fixture


PROFILE_NAME = 'Local <b>& {profiles}'
INITIAL_POSITION = {'left':55, 'top':66}
QUOTA_DETAIL = 'Fixture quota <b>& {detail}'


def message(page, key, params=None):
    value = page.evaluate("""({key,params}) =>
        StandTermI18n.create(StandTermMessages, document.documentElement.lang).t(key, params)
    """, {'key':key, 'params':params or {}})
    assert value != key, f'Missing reviewed translation: {key}'
    return value


def new_page(browser, url, locale):
    context = browser.new_context(viewport={'width':1280, 'height':800}, locale='en-US')
    page = context.new_page()
    initial = {'uiLanguage':locale, 'fontSize':18, 'copyOnSelect':False,
               'saveSshHistory':False, 'agentAccessMintMode':'approval_pending'}
    page.add_init_script("""if (!localStorage.getItem('terminal.pref.v1')) {
        localStorage.setItem('terminal.pref.v1', JSON.stringify(%s));
        localStorage.setItem('agentPanelPosition.v1', JSON.stringify(%s));
    }""" % (json.dumps(initial), json.dumps(INITIAL_POSITION)))
    page.goto(fixture.debug_url(url), wait_until='domcontentloaded')
    page.wait_for_function('() => window.terminalTest?.getSocketState().connected === true')
    page.add_style_tag(content='#debug-hud, #payload-log { display:none !important; }')
    page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
    page.evaluate("""name => window.terminalTest.setSshSessionState({profiles:[
        {id:'profile-primary',name,host:'local.test',port:'2222',username:'local-user'},
        {id:'profile-other',name:'Unchanged',host:'other.test',port:'22',username:'other-user'}],
        history:[0,1].map(index => ({id:`local-history-${index}`,host:`local-${index}.test`,
            port:'22',username:'recent',lastUsedAt:`2026-09-20T00:00:0${index}Z`}))})""", PROFILE_NAME)
    key = page.evaluate("() => window.terminalTest.createBrowserSshKeyForProfileForTest('profile-primary')")
    page.evaluate("() => {window.transferDocumentMarker = 'original'; window.terminalTest.clearEmitted();}")
    page.click('#quick-settings')
    assert page.locator('#settings-export').inner_text() == message(page, 'settings.transfer.export')
    assert page.locator('#settings-import').inner_text() == message(page, 'settings.transfer.import')
    wait_status(page, message(page, 'settings.transfer.hint'))
    return context, page, key


def snapshot(page):
    return page.evaluate("""async () => ({
        storage:Object.fromEntries(Object.keys(localStorage).sort().map(key => [key,localStorage.getItem(key)])),
        ssh:await window.terminalTest.getSshSessionState(),
        key:await window.terminalTest.getBrowserSshKeyMetadataForTest('profile-primary')
    })""")


def payload(locale):
    def node(node_id, host, next_id=None):
        return {'id':node_id,'endpoint':{'host':host,'port':'2200','username':'import-user'},
                'authentication':{'method':'password'},'hostKeyAlias':'','nextNodeId':next_id}
    return {'format':'standterm-browser-settings','version':1,'exportedAt':'2026-09-20T00:00:00Z',
            'preferences':{'copyOnSelect':True,'saveSshHistory':True,'uiLanguage':locale},
            'ui':{'agentPanelPosition':{'left':123,'top':130}},
            'ssh':{'version':2,'revision':0,
                   'profiles':[{'id':'profile-primary','name':'Imported <b>& {profiles}','startNodeId':'import-jump'},
                               {'id':'profile-other','name':'Shared target','startNodeId':'import-target'}],
                   'history':[{'id':f'import-history-{index}','name':f'Recent {index}',
                               'startNodeId':f'import-history-node-{index}',
                               'lastUsedAt':f'2026-09-20T01:00:0{index}Z'} for index in range(7)],
                   'nodes':[node('import-jump','jump.test','import-target'),node('import-target','target.test')]
                           + [node(f'import-history-node-{index}',f'import-{index}.test') for index in range(7)]}}


def envelope(inner):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as zipped:
        zipped.writestr('standterm-settings.json', json.dumps(inner, ensure_ascii=False))
    return {'format':'standterm-settings-envelope','version':1,'contentType':'application/zip',
            'encoding':'base64','content':base64.b64encode(archive.getvalue()).decode('ascii')}


def upload(page, value):
    content = value if isinstance(value, str) else json.dumps(value)
    page.locator('#settings-import-file').set_input_files({
        'name':'settings.json','mimeType':'application/json','buffer':content.encode('utf-8')})


def wait_status(page, expected):
    page.wait_for_function('expected => document.getElementById("settings-transfer-status").textContent === expected', arg=expected)


def test_export_download_keeps_data_and_excludes_keys(browser, url):
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            before = snapshot(page)
            with page.expect_download() as download_info:
                page.click('#settings-export')
            download = download_info.value
            wait_status(page, message(page, 'settings.transfer.exported'))
            assert re.fullmatch(r'standterm-settings-\d{8}\.json', download.suggested_filename)
            exported_envelope = json.loads(Path(download.path()).read_text(encoding='utf-8'))
            assert set(exported_envelope) == {'format','version','contentType','encoding','content'}
            assert {key:value for key,value in exported_envelope.items() if key != 'content'} == {
                'format':'standterm-settings-envelope','version':1,'contentType':'application/zip','encoding':'base64'}
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported_envelope['content']))) as zipped:
                assert zipped.namelist() == ['standterm-settings.json']
                inner = json.loads(zipped.read('standterm-settings.json'))
            assert inner == page.evaluate('value => window.terminalTest.decodeBrowserSettingsEnvelopeForTest(value)', exported_envelope)
            assert inner['format'] == 'standterm-browser-settings' and inner['version'] == 1
            assert inner['preferences']['uiLanguage'] == locale
            assert inner['preferences']['agentAccessMintMode'] == 'approval_pending'
            assert inner['preferences']['fontSize'] == 18
            assert inner['ui']['agentPanelPosition'] == INITIAL_POSITION
            assert inner['ssh']['profiles'][0]['id'] == 'profile-primary'
            assert inner['ssh']['profiles'][0]['name'] == PROFILE_NAME
            assert any(node['endpoint'] == {'host':'local.test','port':'2222','username':'local-user'} for node in inner['ssh']['nodes'])
            assert 'keys' not in inner and all('keyId' not in profile for profile in inner['ssh']['profiles'])
            keyed = [node for node in inner['ssh']['nodes'] if node['authentication']['method'] == 'browser-key']
            assert keyed and all(node['authentication']['keyRef'] is None for node in keyed)
            encoded = json.dumps(inner)
            assert all(value not in encoded for value in [key['keyId'],key['publicKeyOpenSsh'],'privateKey','ssh-ed25519 '])
            assert snapshot(page) == before
        finally:
            fixture.close_context(context)


def test_import_confirmation_cancel_preserves_all_state(browser, url):
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            before = snapshot(page)
            connection = page.evaluate('() => ({socket:window.terminalTest.getSocketState(),active:window.terminalTest.getActiveAgentState()})')
            prompts = []
            page.once('dialog', lambda dialog: (prompts.append(dialog.message), dialog.dismiss()))
            with page.expect_event('dialog'):
                upload(page, envelope(payload(locale)))
            page.wait_for_function('() => document.getElementById("settings-import-file").value === ""')
            assert len(prompts) == 1
            assert prompts[0] == message(page, 'settings.transfer.confirm', {
                'preferences':3,'profiles':2,'history':7,'limit':6})
            assert snapshot(page) == before
            page.wait_for_timeout(100)
            assert page.evaluate('() => window.transferDocumentMarker') == 'original'
            assert page.evaluate('() => ({socket:window.terminalTest.getSocketState(),active:window.terminalTest.getActiveAgentState()})') == connection
            assert not fixture.get_emitted(page, 'start_ssh')
        finally:
            fixture.close_context(context)


def test_import_merges_data_and_reloads_provided_preferences(browser, url):
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            before = snapshot(page)
            next_locale = 'zh-TW' if locale == 'en' else 'en'
            imported = payload(next_locale)
            if locale == 'zh-TW':
                imported['ui']['agentPanelPosition'] = None
            expected_prompt = message(page, 'settings.transfer.confirm', {
                'preferences':3,'profiles':2,'history':7,'limit':6})
            expected_status = message(page, 'settings.transfer.imported')
            statuses = []
            page.expose_function('recordTransferStatus', lambda value: statuses.append(value))
            page.evaluate("""() => {
                const status = document.getElementById('settings-transfer-status');
                new MutationObserver(() => window.recordTransferStatus(status.textContent))
                    .observe(status, {childList:true,subtree:true,characterData:true});
            }""")
            prompts = []
            page.once('dialog', lambda dialog: (prompts.append(dialog.message), dialog.accept()))
            with page.expect_navigation(wait_until='domcontentloaded'):
                upload(page, envelope(imported))
            page.wait_for_function('() => !!window.terminalTest')
            after = snapshot(page)
            assert prompts == [expected_prompt]
            assert expected_status in statuses
            assert page.evaluate('() => performance.getEntriesByType("navigation")[0].type') == 'reload'
            assert page.evaluate('() => window.transferDocumentMarker === undefined')
            assert page.locator('html').get_attribute('lang') == next_locale
            preferences = json.loads(after['storage']['terminal.pref.v1'])
            previous = json.loads(before['storage']['terminal.pref.v1'])
            for name, value in previous.items():
                assert preferences[name] == imported['preferences'].get(name, value), name
            page.click('#quick-settings')
            assert page.locator('#pref-copyOnSelect').is_checked()
            assert page.locator('#ssh-save-history').is_checked()
            position = imported['ui']['agentPanelPosition']
            assert json.loads(after['storage'].get('agentPanelPosition.v1','null')) == position
            merged = after['ssh']
            assert merged['profiles'][:2] == before['ssh']['profiles']
            assert len(merged['profiles']) == 4
            source_ids = {item['id'] for item in imported['ssh']['profiles']}
            assert all(item['id'] not in source_ids for item in merged['profiles'][2:])
            assert [item['name'] for item in merged['profiles'][2:]] == [item['name'] for item in imported['ssh']['profiles']]
            assert merged['history'][:2] == before['ssh']['history'] and len(merged['history']) == 6
            assert [item['host'] for item in merged['history'][2:]] == [f'import-{index}.test' for index in range(4)]
            assert all(node in merged['nodes'] for node in before['ssh']['nodes'])
            nodes = {node['id']:node for node in merged['nodes']}
            route = nodes[merged['profiles'][2]['startNodeId']]
            target = nodes[route['nextNodeId']]
            assert route['endpoint']['host'] == 'jump.test' and target['endpoint']['host'] == 'target.test'
            assert target['nextNodeId'] is None and merged['profiles'][3]['startNodeId'] == target['id']
            original_node_ids = {node['id'] for node in before['ssh']['nodes'] + imported['ssh']['nodes']}
            assert route['id'] not in original_node_ids and target['id'] not in original_node_ids
            assert after['key'] == key
            assert page.evaluate('keyId => window.terminalTest.browserSshKeyRecordExistsForTest(keyId)', key['keyId'])
        finally:
            fixture.close_context(context)


def test_import_validation_and_partial_failure_boundaries(browser, url):
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            before = snapshot(page)
            prompts = []
            def dismiss(dialog):
                prompts.append(dialog.message)
                dialog.dismiss()
            page.on('dialog', dismiss)
            valid = envelope(payload(locale))
            corrupted = dict(valid)
            archive = bytearray(base64.b64decode(valid['content']))
            content_offset = 30 + int.from_bytes(archive[26:28], 'little') + int.from_bytes(archive[28:30], 'little')
            archive[content_offset] ^= 1
            corrupted['content'] = base64.b64encode(archive).decode('ascii')
            invalid_payload = payload(locale)
            invalid_payload['keys'] = [{'keyId':'forbidden'}]
            raw_json_error = page.evaluate("""() => {try {JSON.parse('not-json');} catch (error) {return error.message;}}""")
            invalid_node = payload(locale)
            invalid_node['ssh']['nodes'][0]['endpoint']['host'] = ''
            variants = [('not-json',raw_json_error),
                        (dict(valid,version=99),message(page, 'settings.transfer.envelope_unsupported')),
                        (envelope(invalid_payload),message(page, 'settings.transfer.payload_unsupported')),
                        (dict(valid,content=base64.b64encode(b'short').decode('ascii')),message(page, 'settings.transfer.archive_size_invalid')),
                        (corrupted,message(page, 'settings.transfer.archive_checksum_failed')),
                        (envelope(invalid_node),'Each SSH node needs a valid host, port and username.')]
            for value, expected in variants:
                upload(page, value)
                wait_status(page, expected)
                assert not prompts and snapshot(page) == before
            upload(page, 'x' * 1048577)
            wait_status(page, message(page, 'settings.transfer.file_size_invalid'))
            assert not prompts and snapshot(page) == before
            page.evaluate("""() => {
                window.originalFileText = File.prototype.text;
                File.prototype.text = () => Promise.reject(new Error(''));
            }""")
            try:
                upload(page, valid)
                wait_status(page, message(page, 'settings.transfer.import_failed'))
            finally:
                page.evaluate('() => {File.prototype.text = window.originalFileText;}')
            page.evaluate("""() => {
                window.originalBlob = window.Blob;
                window.Blob = function() {throw new Error('');};
            }""")
            try:
                page.click('#settings-export')
                wait_status(page, message(page, 'settings.transfer.export_failed'))
                page.wait_for_function('() => !document.getElementById("settings-export").disabled')
            finally:
                page.evaluate('() => {window.Blob = window.originalBlob;}')
            assert not prompts and snapshot(page) == before
            assert page.evaluate('() => window.transferDocumentMarker') == 'original'
            page.remove_listener('dialog', dismiss)
            page.once('dialog', lambda dialog: (prompts.append(dialog.message), dialog.accept()))
            page.evaluate("""detail => {
                window.originalSetItem = Storage.prototype.setItem;
                Storage.prototype.setItem = function(key, value) {
                    if (key === 'terminal.pref.v1') throw new DOMException(detail, 'QuotaExceededError');
                    return originalSetItem.call(this, key, value);
                };
            }""", QUOTA_DETAIL)
            try:
                upload(page, valid)
                wait_status(page, message(page, 'settings.transfer.partial_failure', {'detail':QUOTA_DETAIL}))
                assert page.locator('#settings-transfer-status b').count() == 0
            finally:
                page.evaluate('() => {Storage.prototype.setItem = window.originalSetItem;}')
            partially_imported = snapshot(page)
            assert partially_imported['storage'] == before['storage']
            assert partially_imported['key'] == before['key']
            assert partially_imported['ssh']['profiles'][:2] == before['ssh']['profiles']
            assert len(partially_imported['ssh']['profiles']) == 4
            assert len(partially_imported['ssh']['history']) == 6
            assert page.evaluate('keyId => window.terminalTest.browserSshKeyRecordExistsForTest(keyId)', key['keyId'])
            # The successful path schedules reload after 50 ms; this failed path must not schedule it.
            page.wait_for_timeout(150)
            assert page.evaluate('() => window.transferDocumentMarker') == 'original'
            assert snapshot(page) == partially_imported, 'A partial failure automatically retried the import'
            assert len(prompts) == 1
        finally:
            fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for test in [test_export_download_keeps_data_and_excludes_keys,
                             test_import_confirmation_cancel_preserves_all_state,
                             test_import_merges_data_and_reloads_provided_preferences,
                             test_import_validation_and_partial_failure_boundaries]:
                    test(browser, url)
                    print(test.__name__ + ': ok', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
