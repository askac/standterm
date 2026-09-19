"""Check browser access, device recovery, clipboard and terminal chrome in both locales."""
import argparse
import json
from urllib.parse import urlparse

import agent_browser_smoke as fixture
from settings_transfer_i18n_browser_smoke import message


RAW = 'Fixture <b>& {detail}'
ACCESS_URL = 'http://fixture.invalid:2222/?token=raw_%3Cb%3E%26&value={token}'
LOCAL_URL = 'http://localhost:2222/?token=local_raw'


def status(page, selector, key, params=None):
    expected = message(page, key, params)
    page.wait_for_function('({selector,expected}) => document.querySelector(selector).textContent === expected',
                           arg=dict(selector=selector, expected=expected))


def translated(page, selector):
    for element in page.locator(selector).all():
        for source, target in [('data-i18n',None),('data-i18n-title','title'),('data-i18n-aria-label','aria-label')]:
            key = element.get_attribute(source)
            if key:
                actual = element.text_content() if target is None else element.get_attribute(target)
                assert actual == message(page, key), (key, actual)


def open_tab(page, tab):
    if not page.locator('#settings-modal').evaluate("el => el.classList.contains('open')"):
        page.click('#quick-settings')
    page.click(f'.settings-nav-item[data-tab="{tab}"]')


def clipboard(page, fallback=False):
    page.evaluate("""fallback => {
        window.fixtureClipboard = {writes:[],fallbacks:[],fallback};
        Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
            writeText:text => new Promise((resolve,reject) => {
                Object.assign(window.fixtureClipboard, {resolve,reject});
                window.fixtureClipboard.writes.push(text);
            })
        }});
        document.execCommand = command => {
            window.fixtureClipboard.fallbacks.push({command,text:document.activeElement.value});
            return window.fixtureClipboard.fallback;
        };
    }""", fallback)


def settle_clipboard(page, success):
    page.evaluate("success => success ? window.fixtureClipboard.resolve() : window.fixtureClipboard.reject(new Error('Fixture clipboard denied'))", success)


def assert_narrow(page, selectors):
    page.set_viewport_size({'width':480,'height':600})
    for selector in selectors:
        element = page.locator(selector)
        element.scroll_into_view_if_needed()
        metrics = element.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return {id:element.id,left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom,
                textFits:element.tagName === 'TEXTAREA' || element.scrollWidth <= element.clientWidth+1};
        }""")
        assert metrics['left'] >= 0 and metrics['right'] <= 480 and metrics['top'] >= 0 and metrics['bottom'] <= 600, metrics
        assert metrics['textFits'], metrics


def test_access_url_reveal_and_async_clipboard(browser, url, locale):
    context, page = fixture.new_page(browser, url, locale)
    requests = []
    page.route('**/access-url', lambda route: (
        requests.append(route.request.method),
        route.fulfill(json=dict(status='ok', access_url=ACCESS_URL, localhost_access_url=LOCAL_URL))))
    try:
        open_tab(page, 'server')
        assert requests == []
        assert not page.locator('#server-access-url').is_visible()
        translated(page, '#server-access-copy-btn, #server-access-show-btn')
        dialogs = []
        page.once('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.click('#server-access-show-btn')
        assert requests == [] and not page.locator('#server-access-url').is_visible()
        assert dialogs == [message(page, 'browser.server_access.confirm_show')]
        page.clock.install()
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#server-access-show-btn')
        status(page, '#server-access-status', 'browser.server_access.shown')
        assert page.locator('#server-access-url').text_content() == ACCESS_URL + '\n' + LOCAL_URL
        assert requests == ['GET'] and 'token=' not in page.url
        assert page.locator('#server-access-url b').count() == 0
        page.clock.fast_forward(30001)
        assert not page.locator('#server-access-url').is_visible()
        assert page.locator('#server-access-url').text_content() == ''
        for success, fallback in [(True,False),(False,True),(False,False)]:
            clipboard(page, fallback)
            page.click('#server-access-copy-btn')
            page.wait_for_function('() => window.fixtureClipboard.writes.length === 1')
            assert page.locator('#server-access-copy-btn').is_disabled()
            assert page.inner_text('#server-access-status') != message(page, 'browser.server_access.copied')
            settle_clipboard(page, success)
            status(page, '#server-access-status', 'browser.server_access.copied' if success or fallback else 'browser.server_access.copy_failed')
            assert not page.locator('#server-access-copy-btn').is_disabled()
            result = page.evaluate('() => ({writes:fixtureClipboard.writes,fallbacks:fixtureClipboard.fallbacks})')
            assert result['writes'] == [ACCESS_URL]
            assert result['fallbacks'] == ([] if success else [dict(command='copy',text=ACCESS_URL)])
            assert not page.locator('#server-access-url').is_visible()
        assert requests == ['GET'] * 4
        assert_narrow(page, ['#server-access-copy-btn','#server-access-show-btn'])
    finally:
        fixture.close_context(context)


def test_platform_requests_preserve_credential_and_hostname_scope(browser, url, locale):
    context, page = fixture.new_page(browser, url, locale)
    calls = []
    failures = {}
    state = dict(status='ok', available=True, configured_credentials=0, armed_credentials=0, rp_id=RAW)
    creation = dict(ceremony_id='register_<b>& {id}', challenge='AAE',
                    user=dict(id='AgM',name='raw-user',displayName=RAW), rp=dict(id='localhost',name=RAW),
                    pubKeyCredParams=[dict(type='public-key',alg=-7)], excludeCredentials=[dict(type='public-key',id='BAU')])
    assertion = dict(ceremony_id='arm_<b>& {id}', challenge='Bgc', rpId='localhost',
                     allowCredentials=[dict(type='public-key',id='AAE')])

    def route_recovery(route):
        path = urlparse(route.request.url).path
        body = route.request.post_data_json if route.request.post_data else None
        calls.append(dict(path=path,method=route.request.method,body=body))
        if path in failures:
            route.fulfill(status=400, json=dict(status='failed',message=failures[path],error_code='fixture_raw'))
            return
        if path.endswith('/status'):
            result = state
        elif path.endswith('/register/options'):
            result = dict(status='ok',public_key=creation)
        elif path.endswith('/authenticate/options'):
            result = dict(status='ok',public_key=assertion)
        else:
            if path.endswith('/register/complete'):
                state.update(configured_credentials=1, armed_credentials=1)
            if path.endswith('/credentials/remove'):
                state.update(configured_credentials=0, armed_credentials=0)
            result = dict(status='ok')
        route.fulfill(json=result)

    page.route('**/session-recovery/**', route_recovery)
    page.evaluate("""() => {
        window.fixtureCredentialCalls = [];
        const bytes = () => new Uint8Array([0,1]).buffer;
        const credential = response => ({id:'AAE',rawId:bytes(),type:'public-key',
            authenticatorAttachment:'platform',getClientExtensionResults:() => ({fixture:true}),response});
        Object.defineProperty(navigator, 'credentials', {configurable:true,value:{
            create:async options => {
                window.fixtureCredentialCalls.push({kind:'create',options});
                return credential({clientDataJSON:bytes(),attestationObject:bytes(),getTransports:() => ['internal']});
            },
            get:async options => {
                window.fixtureCredentialCalls.push({kind:'get',options});
                return credential({clientDataJSON:bytes(),authenticatorData:bytes(),signature:bytes(),userHandle:bytes()});
            }
        }});
    }""")
    try:
        open_tab(page, 'server')
        page.wait_for_function("() => !document.getElementById('platform-recovery-register').disabled")
        assert page.locator('#platform-recovery-arm').is_disabled()
        assert page.locator('#platform-recovery-remove').is_disabled()
        translated(page, '#platform-recovery-register, #platform-recovery-arm, #platform-recovery-remove')
        failures['/session-recovery/register/options'] = RAW + ' register'
        page.click('#platform-recovery-register')
        page.wait_for_function("() => !document.getElementById('platform-recovery-register').disabled")
        assert page.inner_text('#platform-recovery-status') == RAW + ' register'
        assert page.evaluate('() => fixtureCredentialCalls.length') == 0
        failures.clear()
        calls.clear()
        page.click('#platform-recovery-register')
        page.wait_for_function("() => !document.getElementById('platform-recovery-arm').disabled")
        status(page, '#platform-recovery-status', 'browser.platform.status', {'configured':1,'armed':1,'rp_id':RAW})
        page.click('#platform-recovery-arm')
        page.wait_for_function("() => fixtureCredentialCalls.length === 2 && !document.getElementById('platform-recovery-arm').disabled")
        captured = page.evaluate("""() => fixtureCredentialCalls.map(({kind,options}) => ({kind,
            hasCeremony:Object.hasOwn(options.publicKey,'ceremony_id'),challenge:Array.from(options.publicKey.challenge),
            id:Array.from(kind === 'create' ? options.publicKey.user.id : options.publicKey.allowCredentials[0].id),
            rp:kind === 'create' ? options.publicKey.rp : options.publicKey.rpId}))""")
        assert captured == [dict(kind='create',hasCeremony=False,challenge=[0,1],id=[2,3],rp=creation['rp']),
                            dict(kind='get',hasCeremony=False,challenge=[6,7],id=[0,1],rp='localhost')], captured
        posts = [call for call in calls if call['method'] == 'POST']
        assert [call['path'] for call in posts] == ['/session-recovery/register/options','/session-recovery/register/complete',
                                                  '/session-recovery/authenticate/options','/session-recovery/authenticate/complete']
        for index, source, response in [(1,creation,dict(clientDataJSON='AAE',attestationObject='AAE',transports=['internal'])),
                                       (3,assertion,dict(clientDataJSON='AAE',authenticatorData='AAE',signature='AAE',userHandle='AAE'))]:
            assert posts[index]['body'] == dict(ceremony_id=source['ceremony_id'],credential=dict(
                id='AAE',rawId='AAE',type='public-key',authenticatorAttachment='platform',
                clientExtensionResults=dict(fixture=True),response=response))
        assert posts[0]['body'] is None and posts[2]['body'] is None
        dialogs = []
        page.once('dialog', lambda dialog: (dialogs.append(dialog.message),dialog.dismiss()))
        page.click('#platform-recovery-remove')
        assert dialogs == [message(page, 'browser.platform.confirm_revoke')]
        assert not any(call['path'].endswith('/credentials/remove') for call in calls)
        failures['/session-recovery/credentials/remove'] = RAW + ' revoke'
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#platform-recovery-remove')
        page.wait_for_function("() => !document.getElementById('platform-recovery-remove').disabled")
        assert page.inner_text('#platform-recovery-status') == RAW + ' revoke'
        assert state['configured_credentials'] == 1
        failures.clear()
        calls.clear()
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#platform-recovery-remove')
        page.wait_for_function("() => document.getElementById('platform-recovery-arm').disabled")
        removed = [call for call in calls if call['path'].endswith('/credentials/remove')]
        assert removed == [dict(path='/session-recovery/credentials/remove',method='POST',body=None)]
        assert page.evaluate('() => fixtureCredentialCalls.length') == 2
        state.update(available=False,message=RAW)
        open_tab(page, 'server')
        page.wait_for_function('raw => document.getElementById("platform-recovery-status").textContent === raw', arg=RAW)
        assert page.locator('#platform-recovery-status b').count() == 0
        assert_narrow(page, ['#platform-recovery-register','#platform-recovery-arm','#platform-recovery-remove'])
    finally:
        fixture.close_context(context)


def test_diagnostics_copy_waits_and_preserves_raw_records(browser, url, locale):
    context, page = fixture.new_page(browser, url, locale)
    try:
        page.add_init_script("""sessionStorage.setItem('standterm-connection-diagnostics-v1', JSON.stringify([
            {at:'2026-09-20T01:02:03Z',event:'fixture.raw_event',launcher_session_id:'raw-session',
             details:{message:%s,reason:'https://secret.invalid/?token=never-export'}}]));""" % json.dumps(RAW))
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => window.terminalTest?.getSocketState().connected')
        open_tab(page, 'diagnostics')
        translated(page, '#connection-diagnostics-copy, #connection-diagnostics-clear, #connection-diagnostics-log')
        for success in [True,False]:
            clipboard(page, False)
            before = page.input_value('#connection-diagnostics-log')
            entries = page.evaluate('() => window.terminalTest.getConnectionDiagnostics()')
            assert RAW in before and 'fixture.raw_event' in before
            assert '[2026-09-20T01:02:03.000Z] [session=raw-session]' in before
            assert before.startswith('StandTerm connection diagnostics\nLauncher Session ID:')
            assert '\nBrowser time zone:' in before
            assert 'never-export' not in before and 'secret.invalid' not in before
            assert '[url redacted]' in before
            page.click('#connection-diagnostics-copy')
            page.wait_for_function('() => fixtureClipboard.writes.length === 1')
            status(page, '#connection-diagnostics-status', 'browser.diagnostics.copying')
            settle_clipboard(page, success)
            status(page, '#connection-diagnostics-status', 'browser.diagnostics.copied' if success else 'browser.diagnostics.copy_failed',
                   {'count':len(entries)} if success else None)
            assert page.evaluate('() => fixtureClipboard.writes') == [before]
            assert page.evaluate('() => window.terminalTest.getConnectionDiagnostics()') == entries
        assert_narrow(page, ['#connection-diagnostics-clear','#connection-diagnostics-copy','#connection-diagnostics-log'])
        page.click('#connection-diagnostics-clear')
        assert page.evaluate('() => window.terminalTest.getConnectionDiagnostics()') == []
        status(page, '#connection-diagnostics-status', 'browser.diagnostics.count', {'count':0})
    finally:
        fixture.close_context(context)


def test_paste_chrome_keeps_payload_and_terminal_target(browser, url, locale):
    context, page = fixture.new_page(browser, url, locale)
    try:
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        fixture.attach_agent(page)
        raw = RAW + '\nsecond line'
        for approve in [False,True]:
            fixture.clear_emitted(page)
            page.evaluate('text => window.terminalTest.startPasteReview(text)', raw)
            page.wait_for_selector('#paste-review-modal.open')
            assert page.input_value('#paste-review-preview') == raw
            target = page.evaluate('() => window.terminalTest.getTerminalTabsState().tabs.find(item => item.id === "main")')
            assert page.inner_text('#paste-review-meta') == message(page, 'browser.paste.metadata', {'target':target['title'],'lines':2})
            translated(page, '#paste-review-modal [data-i18n], #paste-review-preview, #quick-settings, #new-tab-btn')
            assert page.locator('#paste-review-modal b').count() == 0
            assert_narrow(page, ['#paste-review-cancel','#paste-review-approve','#paste-review-preview'])
            button = page.locator('#paste-review-' + ('approve' if approve else 'cancel'))
            button.evaluate("element => {element.textContent = 'Display <b>& {action}';}")
            button.click()
            events = fixture.get_emitted(page, 'ssh_input')
            assert [event['args'][0] for event in events] == ([dict(terminal_id='main',data=raw)] if approve else [])
            page.wait_for_function('() => window.terminalTest.activeTerminalHasFocus()')
            if not approve:
                page.locator('#paste-review-cancel').evaluate("element => {element.textContent = StandTermI18n.create(StandTermMessages,document.documentElement.lang).t(element.dataset.i18n);}")
        fixture.set_agent_mode(page, 'direct', 'direct_active')
        assert_narrow(page, ['#new-tab-btn','#agent-pause-btn','#agent-toggle-btn','#quick-settings'])
    finally:
        fixture.close_context(context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', action='store_true', help='Run the existing Files and terminal PiP cases in both locales.')
    args = parser.parse_args()
    tests = ([fixture.test_terminal_pip_hides_selected_tab_and_keeps_background_tab,
              fixture.test_sftp_status_actions_and_terminal_pip_transition,
              fixture.test_sftp_send_context_action_is_limited_to_connected_ssh_tabs] if args.files else [
        test_access_url_reveal_and_async_clipboard,
        test_platform_requests_preserve_credential_and_hostname_scope,
        test_diagnostics_copy_waits_and_preserves_raw_records,
        test_paste_chrome_keeps_payload_and_terminal_target,
        fixture.test_toolbar_pause_targets_main_tab_not_panel_override,
        fixture.test_clipboard_paste_targets_and_native_review,
        fixture.test_agent_panel_status_gates_and_external_hint,
    ])
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for locale in ['en','zh-TW']:
                    for test in tests:
                        test(browser, url, locale)
                        print(f'{test.__name__} ({locale}): PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
