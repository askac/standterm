"""Check localized runtime settings against typed schemas and single-setting writes."""
import json
from urllib.parse import urlparse

import agent_browser_smoke as fixture
from settings_transfer_i18n_browser_smoke import message


RAW_LABEL = 'Plugin <b>& {setting}'
RAW_OPTION = 'future_<b>& {value}'
RAW_ERROR = 'Backend <b>& {detail}'
DIGEST = 'ab' * 32


def snapshot(allowed=True):
    def item(key, kind, **extra):
        return dict(setting_key=key, label=RAW_LABEL + ' ' + key, value_type=kind,
                    risk_level='low', apply_scope='live', mutable=True,
                    required_capability='settings_update_low_risk', **extra)

    schema = [item('plugin.enabled', 'boolean'),
              item('plugin.count', 'integer', min_value=1, max_value=50),
              item('plugin.mode', 'enum', allowed_values=['plain', RAW_OPTION]),
              item('plugin.locked', 'boolean')]
    schema[1]['apply_scope'] = 'next_connection'
    schema[2]['apply_scope'] = 'restart'
    schema.extend([
        dict(setting_key='plugin.medium', label=RAW_LABEL, risk_level='medium',
             apply_scope='live', mutable=False),
        dict(setting_key='plugin.high', label=RAW_LABEL, risk_level='high',
             apply_scope='restart', mutable=False),
        dict(setting_key='plugin.future', label=RAW_LABEL,
             risk_level='future_<b>& {risk}', apply_scope='future_<b>& {scope}', mutable=False),
    ])
    return dict(status='ok', read_only=not allowed, settings_version=37,
                settings_schema_digest=DIGEST, settings_schema=dict(core=[], plugins=schema),
                effective_settings=dict(runtime_name='Fixture <b>& {runtime}',
                    default_connection_type='plugin_raw', force_connection_type=None, https_enabled=True,
                    connection_types=[dict(connection_type='plugin_raw', label=RAW_LABEL, allowed=True),
                                      dict(connection_type='needs_grant', authorization_available=True),
                                      dict(connection_type='denied_raw', allowed=False)]),
                mutable_settings={'plugin.enabled':dict(value=False), 'plugin.count':dict(value=7),
                                  'plugin.mode':dict(value='plain'), 'plugin.locked':dict(value=True, locked=True)},
                capabilities={name:dict(allowed=value) for name, value in {
                    'settings_view':True, 'settings_update_low_risk':allowed,
                    'settings_update_high_risk':False, 'settings_auth_manage':False}.items()})


def new_page(browser, url, locale, allowed=True):
    context = browser.new_context(viewport={'width':1280, 'height':800}, locale='en-US')
    page = context.new_page()
    requests = []
    page.on('request', lambda request: requests.append(urlparse(request.url).path))
    # Intercept only settings requests; the app still uses its real Socket.IO connection.
    page.add_init_script("""(() => {
        localStorage.setItem('terminal.pref.v1', JSON.stringify({uiLanguage:%s}));
        const fixture = window.serverSettingsFixture = {snapshot:%s, requests:[]};
        let factory;
        Object.defineProperty(window, 'io', {
            configurable:true, get:() => factory,
            set:value => {factory = new Proxy(value, {apply(target, receiver, args) {
                const socket = Reflect.apply(target, receiver, args);
                const emit = socket.emit;
                fixture.receive = (event, data) => socket.onevent({data:[event, data]});
                socket.emit = function(event, ...args) {
                    if (['settings_snapshot_request','settings_admin_grant_request','settings_update_request'].includes(event)) {
                        fixture.requests.push({event,args:structuredClone(args)});
                        if (event === 'settings_snapshot_request') {
                            queueMicrotask(() => fixture.receive('settings_snapshot', structuredClone(fixture.snapshot)));
                        } else if (event === 'settings_admin_grant_request') {
                            queueMicrotask(() => fixture.receive('settings_admin_grant', {
                                status:'ok', grant_id:'fixture-grant', expires_at:Date.now()/1000+120
                            }));
                        }
                        return this;
                    }
                    return emit.call(this, event, ...args);
                };
                return socket;
            }});}
        });
    })();""" % (json.dumps(locale), json.dumps(snapshot(allowed))))
    page.goto(fixture.debug_url(url), wait_until='domcontentloaded')
    page.wait_for_function('() => window.terminalTest?.getSocketState().connected === true')
    page.add_style_tag(content='#debug-hud, #payload-log {display:none !important;}')
    page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
    open_server(page)
    return context, page, requests


def open_server(page):
    if not page.locator('#settings-modal').evaluate("element => element.classList.contains('open')"):
        page.click('#quick-settings')
    page.click('.settings-nav-item[data-tab="server"]')
    page.wait_for_function("() => document.getElementById('server-settings-version').textContent === '37'")


def receive(page, event, data):
    page.evaluate('({event,data}) => window.serverSettingsFixture.receive(event,data)', dict(event=event, data=data))


def writes(page):
    return page.evaluate("() => window.serverSettingsFixture.requests.filter(item => item.event !== 'settings_snapshot_request')")


def control(page, key, tag='.server-setting-input'):
    return page.locator(f'#server-settings-mutable-controls {tag}[data-setting-key="{key}"]')


def status(page, key):
    assert page.inner_text('#server-settings-status') == message(page, 'settings.server.' + key)


def test_schema_permissions_and_raw_values(browser, url, locale):
    context, page, requests = new_page(browser, url, locale)
    try:
        status(page, 'writable')
        for element in page.locator('#tab-server [data-i18n^="settings.server."]').all():
            if element.get_attribute('id') == 'server-settings-status':
                continue
            assert element.text_content() == message(page, element.get_attribute('data-i18n'))
        assert page.inner_text('#server-settings-runtime') == 'Fixture <b>& {runtime}'
        assert page.inner_text('#server-settings-default-connection') == 'plugin_raw'
        assert page.inner_text('#server-settings-forced-connection') == message(page, 'settings.server.none')
        assert page.inner_text('#server-settings-https') == message(page, 'settings.server.enabled')
        assert control(page, 'plugin.enabled').get_attribute('type') == 'checkbox'
        assert control(page, 'plugin.count').get_attribute('type') == 'number'
        assert control(page, 'plugin.count').get_attribute('min') == '1'
        assert control(page, 'plugin.count').get_attribute('max') == '50'
        assert control(page, 'plugin.mode').locator('option').all_text_contents() == ['plain', RAW_OPTION]
        assert control(page, 'plugin.mode').locator('option').evaluate_all('items => items.map(item => item.value)') == ['plain', RAW_OPTION]
        for index, item in enumerate(snapshot()['settings_schema']['plugins']):
            row = page.locator(f'#server-settings-schema li[data-setting-key="{item["setting_key"]}"]')
            assert row.locator('span').first.text_content() == item['label'] + ' (' + item['setting_key'] + ')'
            expected = [message(page, 'settings.server.risk_' + item['risk_level']) if index < 6 else item['risk_level'],
                        message(page, 'settings.server.scope_' + item['apply_scope']) if index < 6 else item['apply_scope'],
                        message(page, 'settings.server.mutable' if item['mutable'] else 'settings.server.schema_read_only')]
            assert row.locator('.settings-badge').inner_text() == ' / '.join(expected)
        assert page.locator('#server-settings-connections li > span:first-child').all_text_contents() == [RAW_LABEL,'needs_grant','denied_raw']
        assert page.locator('#server-settings-connections .settings-badge').all_text_contents() == [
            message(page, 'settings.server.' + key) for key in ['allowed','authorization_available','denied']]
        assert page.locator('#tab-server b').count() == 0
        assert control(page, 'plugin.locked').is_disabled()
        assert control(page, 'plugin.locked', 'button').is_disabled()
        control(page, 'plugin.locked', 'button').evaluate('element => element.click()')
        assert writes(page) == []

        receive(page, 'settings_snapshot', snapshot(False))
        status(page, 'read_only')
        assert page.inner_text('#server-cap-settings-update-low') == message(page, 'settings.server.denied')
        for key in ['plugin.enabled','plugin.count','plugin.mode','plugin.locked']:
            assert control(page, key).is_disabled() and control(page, key, 'button').is_disabled()
        # Exercise the capability guard independently of native disabled-button suppression.
        control(page, 'plugin.enabled', 'button').evaluate("element => element.dispatchEvent(new Event('click'))")
        status(page, 'update_denied')
        assert writes(page) == []
        assert '/access-url' not in requests
        assert page.locator('#server-access-url').text_content() == ''

        page.set_viewport_size({'width':480,'height':600})
        for row in page.locator('#server-settings-mutable-controls .settings-row').all():
            row.scroll_into_view_if_needed()
            metrics = row.evaluate("""row => {
                const label = row.querySelector('label'), control = row.querySelector('.settings-control');
                const a = label.getBoundingClientRect(), b = control.getBoundingClientRect(), r = row.getBoundingClientRect();
                return {key:row.dataset.settingKey, noOverflow:row.scrollWidth <= row.clientWidth+1,
                    labelFits:label.scrollWidth <= label.clientWidth+1,
                    contained:a.left >= r.left-1 && a.right <= r.right+1 && b.left >= r.left-1 && b.right <= r.right+1,
                    separate:a.right <= b.left+1 || a.bottom <= b.top+1};
            }""")
            assert metrics['noOverflow'] and metrics['labelFits'] and metrics['contained'] and metrics['separate'], metrics
        assert writes(page) == []
        receive(page, 'settings_snapshot', dict(status='failed', message=RAW_ERROR))
        assert page.inner_text('#server-settings-status') == RAW_ERROR
        assert page.locator('#server-settings-mutable-controls').text_content() == ''
        assert page.inner_text('#server-settings-version') == '--'
        receive(page, 'settings_snapshot', dict(status='failed'))
        status(page, 'unavailable')
    finally:
        fixture.close_context(context)


def test_apply_keeps_typed_values_and_grant_binding(browser, url, locale):
    context, page, requests = new_page(browser, url, locale)
    try:
        control(page, 'plugin.count').fill('11')
        page.click('#settings-save')
        assert writes(page) == [], 'Footer Save submitted a runtime setting'
        open_server(page)
        control(page, 'plugin.enabled').check()
        control(page, 'plugin.count').fill('23')
        control(page, 'plugin.mode').select_option(RAW_OPTION)
        control(page, 'plugin.mode').locator('option').last.evaluate("element => {element.textContent = 'Display-only <b>& {value}';}")
        for index, (key, value) in enumerate([('plugin.enabled',True),('plugin.count',23),('plugin.mode',RAW_OPTION)]):
            assert control(page, key, 'button').inner_text() == message(page, 'settings.server.apply')
            control(page, key, 'button').click()
            page.wait_for_function("count => window.serverSettingsFixture.requests.filter(item => item.event === 'settings_update_request').length === count", arg=index+1)
            status(page, 'applying')
            emitted = writes(page)
            grants = [item for item in emitted if item['event'] == 'settings_admin_grant_request']
            assert grants == [dict(event='settings_admin_grant_request', args=[dict(capability='settings_update_low_risk')])]
            updates = [item for item in emitted if item['event'] == 'settings_update_request']
            assert len(updates) == index+1
            payload = updates[-1]['args'][0]
            request_id = payload.pop('request_id')
            assert request_id.startswith('settings-') and request_id[9:].isdigit()
            assert payload == dict(setting_key=key, value=value, expected_version=37,
                                   expected_schema_digest=DIGEST, grant_id='fixture-grant')
            assert type(payload['value']) is type(value)
            receive(page, 'settings_update_result', dict(status='ok', request_id=request_id))
            status(page, 'applied')
        before = writes(page)
        for result, expected in [(dict(status='failed',message=RAW_ERROR,error_code='ignored'),RAW_ERROR),
                                 (dict(status='failed',error_code='raw_<b>& {code}'),'raw_<b>& {code}'),
                                 (dict(status='failed'),message(page, 'settings.server.update_failed'))]:
            receive(page, 'settings_update_result', result)
            assert page.inner_text('#server-settings-status') == expected
            assert page.locator('#server-settings-status b').count() == 0
            assert writes(page) == before
        assert '/access-url' not in requests
    finally:
        fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for locale in ['en','zh-TW']:
                    for test in [fixture.test_settings_server_tab_loads_readonly_snapshot,
                                 test_schema_permissions_and_raw_values,
                                 test_apply_keeps_typed_values_and_grant_binding]:
                        test(browser, url, locale)
                        print(f'{test.__name__} ({locale}): PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
