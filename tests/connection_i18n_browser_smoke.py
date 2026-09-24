"""Check localized connection controls without opening SSH or UART targets."""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_browser_smoke as fixture
import ssh_routes_browser_smoke as routes_fixture


def new_module_page(browser, ui_language=None):
    context = browser.new_context(viewport={'width': 1100, 'height': 900}, locale='en-US')
    page = context.new_page()
    page.set_content('<main id="fixture"></main>')
    for name in ['standterm-messages', 'standterm-i18n', 'standterm-ssh-routes',
                 'standterm-ssh-login', 'standterm-ssh-node-auth', 'standterm-ssh-host-identity']:
        page.add_script_tag(path=str(fixture.ROOT / 'static' / 'js' / f'{name}.js'))
    page.evaluate("""locale => {
        window.fixtureTranslate = locale === null ? undefined : locale === 'missing' ? key => key
            : StandTermI18n.create(StandTermMessages, locale).t;
    }""", ui_language)
    return context, page


def translated(page, key, params=None):
    value = page.evaluate('({key, params}) => fixtureTranslate(key, params)', {'key': key, 'params': params or {}})
    assert value != key, f'Missing fixture translation: {key}'
    return value


def test_connection_labels_preserve_schema_values_and_start_payload(browser, url):
    for locale in ['en', 'zh-TW']:
        context, page = fixture.new_page(browser, url, ui_language=locale)
        try:
            page.click('#new-tab-btn')
            page.evaluate('() => window.terminalTest.captureSshStartsForTest()')
            page.evaluate("""() => window.terminalTest.setSshSessionState({version:2,revision:0,history:[],
                profiles:[{id:'locale-route',name:'<b>Disconnect</b>',startNodeId:'locale-jump'}],
                nodes:[{id:'locale-jump',endpoint:{host:'jump.test',port:'2221',username:'jump-user'},
                        authentication:{method:'password'},hostKeyAlias:'',nextNodeId:'locale-target'},
                       {id:'locale-target',endpoint:{host:'target.test',port:'2223',username:'target-user'},
                        authentication:{method:'password'},hostKeyAlias:'',nextNodeId:null}]})""")
            routes_fixture.show_ssh(page)
            page.evaluate("""() => {
                const policy = window.terminalTest.getTerminalPolicy();
                policy.force_connection = null;
                policy.connection_options.forEach(option => { option.allowed = true; });
                const ssh = policy.connection_options.find(option => option.connection_type === 'ssh');
                ssh.start_fields = [
                    {name:'host',value_type:'string',input_type:'text',default_value:'schema.test'},
                    {name:'port',value_type:'integer',input_type:'text',default_value:22},
                    {name:'username',value_type:'string',input_type:'text',default_value:'schema-user'},
                    {name:'password',value_type:'string',input_type:'password',secret:true}];
                const shell = policy.connection_options.find(option => option.connection_type === 'local_shell');
                shell.start_fields = [{name:'local_shell_kind',value_type:'enum',input_type:'select',default_value:'alpha',
                    options:[{value:'alpha',label:'Alpha Shell'},{value:'beta',label:'Beta Shell'}]}];
                const uart = policy.connection_options.find(option => option.connection_type === 'uart');
                uart.available_ports = [{device:'COM3',label:'COM3 (Windows)',backend:'windows'}];
                uart.start_fields = [{name:'serial_port',value_type:'string',input_type:'text',default_value:''},
                    {name:'baud_rate',value_type:'integer',input_type:'select',default_value:9600,
                     options:[{value:9600,label:'9600'},{value:115200,label:'115200'}]}];
                window.terminalTest.applyTerminalPolicy(policy);
            }""")
            expected = {'host': '主機', 'port': '連接埠', 'username': '使用者名稱', 'password': '密碼（選填）'} if locale == 'zh-TW' else {
                'host': 'Host', 'port': 'Port', 'username': 'Username', 'password': 'Password (optional)'}
            for field, label in expected.items():
                assert page.locator(f'#{field}').get_attribute('placeholder') == label
            assert ('直接連線' if locale == 'zh-TW' else 'Direct connect') in page.inner_text('#ssh-direct-heading')
            assert ('已儲存路徑' if locale == 'zh-TW' else 'Saved routes') in page.inner_text('#ssh-route-heading')
            assert page.locator('#ssh-save-session').locator('..').inner_text() == (
                '儲存連線設定' if locale == 'zh-TW' else 'Save connection profile')
            for field, value in {'host':'manual.test', 'port':'2222', 'username':'manual-user', 'password':'fixture-password'}.items():
                page.fill(f'#{field}', value)
            form = page.evaluate('() => window.terminalTest.getConnectionFormDataForTest()')
            assert all(form[key] == value for key, value in {
                'host':'manual.test', 'port':'2222', 'username':'manual-user', 'password':'fixture-password', 'connection_type':'ssh'}.items())
            prepared = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
            assert prepared['password'] == 'fixture-password'

            page.click('#ssh-route-heading')
            page.select_option('#ssh-route-picker', 'locale-route')
            assert '<b>Disconnect</b>' in page.locator('#ssh-route-picker option:checked').inner_text()
            assert page.locator('#ssh-route-pane b').count() == 0
            route = page.evaluate('() => window.terminalTest.prepareSshConnectionForTest()')
            assert [(node['host'], node['port'], node['username']) for node in route['route']] == [
                ('jump.test', '2221', 'jump-user'), ('target.test', '2223', 'target-user')]
            assert 'jump.test' in page.inner_text('#ssh-route-path') and 'target.test' in page.inner_text('#ssh-route-path')
            page.click('#ssh-direct-heading')
            restored_form = page.evaluate('() => window.terminalTest.getConnectionFormDataForTest()')
            assert restored_form == form, 'Switching route changed direct fields: ' + ', '.join(
                key for key in set(form) | set(restored_form) if form.get(key) != restored_form.get(key))
            page.evaluate("""() => {
                window.terminalTest.captureSshStartsForTest();
                window.terminalTest.clearEmitted();
                document.getElementById('connectBtn').textContent = 'Disconnect / Cancel connection';
            }""")
            page.click('#connectBtn')
            page.wait_for_function('() => window.terminalTest.getEmitted().some(item => item.event === "start_ssh")')
            starts = fixture.get_emitted(page, 'start_ssh')
            assert len(starts) == 1
            payload = starts[0]['args'][0]
            assert len(payload['route']) == 1
            assert payload['attempt_id'] and payload['route'][0]['node_id']
            assert payload == {
                **{key:form[key] for key in ['connection_type','terminal_id','host','port','username','host_key_alias']},
                'attempt_id':payload['attempt_id'],
                'route':[{**{key:form[key] for key in ['host','port','username','host_key_alias']},
                          'node_id':payload['route'][0]['node_id']}],
            }
            assert 'fixture-password' not in repr(starts), 'Diagnostics exposed a password'
            schema = page.evaluate("""() => {
                const set = (id, value, event) => {
                    const field = document.getElementById(id);
                    field.value = value; field.dispatchEvent(new Event(event, {bubbles:true}));
                };
                set('host','edited-schema.test','input');
                set('password','schema-password','input');
                window.terminalTest.setConnectionTypeForTest('uart');
                const uartOptions = [...document.getElementById('uart-port-select').options].map(item => [item.value,item.text]);
                set('uart-port-select','__manual__','change');
                set('uart-port','/dev/fixture-serial','input');
                set('uart-baud','115200','change');
                const uart = window.terminalTest.getConnectionFormDataForTest();
                window.terminalTest.setConnectionTypeForTest('local_shell');
                const shellOptions = [...document.getElementById('local-shell-kind').options].map(item => [item.value,item.text]);
                set('local-shell-kind','beta','change');
                const shell = window.terminalTest.getConnectionFormDataForTest();
                const policy = window.terminalTest.getTerminalPolicy();
                for (const option of policy.connection_options) {
                    for (const field of option.start_fields || []) {
                        if (field.name === 'host') field.default_value = 'changed-default.test';
                        if (field.name === 'baud_rate') field.default_value = 9600;
                        if (field.name === 'local_shell_kind') field.default_value = 'alpha';
                    }
                }
                window.terminalTest.applyTerminalPolicy(policy);
                const values = Object.fromEntries(['host','password','uart-port','uart-baud','local-shell-kind'].map(id => [id,document.getElementById(id).value]));
                return {uartOptions,shellOptions,uart,shell,values};
            }""")
            assert schema['uartOptions'] == [['COM3','COM3 (Windows)'],
                ['__manual__','手動輸入序列埠…' if locale == 'zh-TW' else 'Manual port…']]
            assert schema['shellOptions'] == [['alpha','Alpha Shell'],['beta','Beta Shell']]
            assert schema['uart'] == {'connection_type':'uart','terminal_id':form['terminal_id'],
                                      'serial_port':'/dev/fixture-serial','baud_rate':'115200'}
            assert schema['shell'] == {'connection_type':'local_shell','terminal_id':form['terminal_id'],'local_shell_kind':'beta'}
            assert schema['values'] == {'host':'edited-schema.test','password':'schema-password','uart-port':'/dev/fixture-serial',
                                        'uart-baud':'115200','local-shell-kind':'beta'}
        finally:
            fixture.close_context(context)


def test_node_key_translation_preserves_key_reference_and_endpoint(browser, url):
    for locale in [None, 'missing', 'zh-TW']:
        context, page = new_module_page(browser, locale)
        try:
            page.evaluate("""() => {
                window.keyEndpoint = {host:'key.test',port:'2222',username:'builder'};
                window.keyRecord = {kind:'credential',keyId:'fixture-key',targetKey:StandTermSshRoutes.endpointKey(keyEndpoint),
                    publicKeyOpenSsh:'ssh-ed25519 AAAA fixture',fingerprint:'SHA256:fixture'};
                window.copiedPublicKey = null;
                window.keyControl = StandTermSshNodeAuth({parent:document.getElementById('fixture'),role:'{role}<b>&',
                    authentication:{method:'password'},endpoint:() => keyEndpoint,profiles:[],savedKeys:[keyRecord],newKeys:new Map(),
                    keyAllowed:true,resetOnEndpointChange:true,createKey:() => {throw new Error('Unexpected key generation');},
                    copyPublicKey:value => {copiedPublicKey = value;},onBusy:() => {},onChange:() => {},translate:fixtureTranslate});
            }""")
            use_label = '{role}<b>&使用金鑰' if locale == 'zh-TW' else '{role}<b>& Use key'
            public_label = '{role}<b>&公鑰' if locale == 'zh-TW' else '{role}<b>& Public key'
            copy_label = translated(page, 'ssh.key.copy_public') if locale == 'zh-TW' else 'Copy public key'
            page.get_by_role('checkbox', name=use_label, exact=True).check()
            assert page.get_by_role('textbox', name=public_label, exact=True).input_value() == 'ssh-ed25519 AAAA fixture'
            assert page.get_by_role('textbox', name=public_label, exact=True).get_attribute('title') == 'SHA256:fixture'
            assert page.locator('#fixture b').count() == 0, 'Display role was interpreted as markup'
            expected = page.evaluate('() => ({method:"browser-key",keyRef:{kind:"credential",keyId:keyRecord.keyId,targetKey:keyRecord.targetKey}})')
            assert page.evaluate('() => keyControl.read()') == expected
            page.get_by_role('button', name=copy_label, exact=True).click()
            assert page.evaluate('() => copiedPublicKey') == 'ssh-ed25519 AAAA fixture'
            page.get_by_role('checkbox', name=use_label, exact=True).uncheck()
            assert page.evaluate('() => keyControl.read()') == {'method':'password'}
            page.get_by_role('checkbox', name=use_label, exact=True).check()
            page.evaluate("() => { keyEndpoint = {...keyEndpoint, host:'other.test'}; keyControl.update(); }")
            assert page.evaluate('() => keyControl.read()') == {'method':'password'}
            assert page.get_by_role('textbox', name=public_label, exact=True).input_value() == ''
            assert page.get_by_role('button', name=copy_label, exact=True).is_disabled()
        finally:
            fixture.close_context(context)


def test_host_identity_translation_preserves_confirmation_binding(browser, url):
    for locale in [None, 'missing', 'zh-TW']:
        context, page = new_module_page(browser, locale)
        try:
            page.evaluate("""() => {
                window.identityTarget = {host:'identity.test',port:'2222',username:'builder',host_key_alias:'fixture-alias'};
                window.identityRequests = []; window.identityPending = [];
                window.identityControl = StandTermSshHostIdentity({parent:document.getElementById('fixture'),
                    editorId:'fixture-editor',nodeId:'fixture-node',read:() => identityTarget,translate:fixtureTranslate,
                    request:payload => new Promise(resolve => {identityRequests.push(payload);identityPending.push(resolve);})});
            }""")
            check_label = translated(page, 'ssh.host_identity.check') if locale == 'zh-TW' else 'Check saved fingerprint'
            keep_label = translated(page, 'ssh.host_identity.keep') if locale == 'zh-TW' else 'Keep fingerprint'
            page.get_by_role('button', name=check_label, exact=True).click()
            request = page.evaluate('() => identityRequests[0]')
            assert {key:request[key] for key in ['operation','editor_id','node_id','host','port','host_key_alias']} == {
                'operation':'inspect','editor_id':'fixture-editor','node_id':'fixture-node',
                'host':'identity.test','port':'2222','host_key_alias':'fixture-alias'}
            page.evaluate("""() => identityPending.shift()({...identityRequests.at(-1),status:'confirm',action_id:'observed-fingerprint',
                message:'SHA256:fixture <b>Cancel connection</b>',question:'Keep this exact fingerprint?'})""")
            page.wait_for_function('text => document.activeElement?.textContent === text', arg=keep_label)
            assert page.locator('.ssh-host-identity-status').inner_text() == 'SHA256:fixture <b>Cancel connection</b>'
            assert page.locator('.ssh-host-identity-status b').count() == 0
            page.get_by_role('button', name=keep_label, exact=True).click()
            cancellation = page.evaluate('() => identityRequests.at(-1)')
            assert cancellation == {**request, 'request_id':cancellation['request_id'], 'operation':'cancel', 'action_id':'observed-fingerprint'}
            assert cancellation['request_id'] != request['request_id']
            page.evaluate("""() => {
                identityTarget = {...identityTarget,host:'edited.test'};
                identityControl.invalidate();
                identityPending.shift()({...identityRequests.at(-1),status:'confirm',action_id:'stale-fingerprint',
                    message:'STALE RESPONSE',question:'Forget it?'});
            }""")
            assert page.locator('.ssh-host-identity-confirm button').count() == 0
            assert 'STALE RESPONSE' not in page.inner_text('#fixture')
            assert page.evaluate('() => identityRequests.length') == 2
            page.get_by_role('button', name=check_label, exact=True).click()
            inspected = page.evaluate('() => identityRequests.at(-1)')
            assert inspected['host'] == 'edited.test'
            page.evaluate("""() => identityPending.shift()({...identityRequests.at(-1),status:'confirm',action_id:'current-fingerprint',
                message:'SHA256:current',question:'Forget this fingerprint?'})""")
            page.wait_for_function('text => document.activeElement?.textContent === text', arg=keep_label)
            forget_label = translated(page, 'ssh.host_identity.forget_now') if locale == 'zh-TW' else 'Forget now'
            page.get_by_role('button', name=forget_label, exact=True).click()
            confirmed = page.evaluate('() => identityRequests.at(-1)')
            assert confirmed == {**inspected, 'request_id':confirmed['request_id'], 'operation':'confirm', 'action_id':'current-fingerprint'}
            assert confirmed['request_id'] != inspected['request_id']
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
                for test in [test_connection_labels_preserve_schema_values_and_start_payload,
                             test_node_key_translation_preserves_key_reference_and_endpoint,
                             test_host_identity_translation_preserves_confirmation_binding]:
                    test(browser, url)
                    print(test.__name__ + ': ok', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
