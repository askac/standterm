"""Check localized preference drafts, reset scope, and persistent footer guidance."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_browser_smoke as fixture
from settings_transfer_i18n_browser_smoke import message, new_page, snapshot


TABS = ['general','appearance','ssh-sessions','server','diagnostics']
CUSTOM_FONT = '"Fixture <b>& {font}", monospace'
ACCESS_MUTATIONS = {'agent_mode_set','agent_attach','agent_detach','agent_pause',
                    'agent_external_token_create','start_ssh','stop_ssh'}


def preferences(page):
    return page.evaluate("""async () => {
        const envelope = await window.terminalTest.createBrowserSettingsEnvelopeForTest();
        return window.terminalTest.decodeBrowserSettingsEnvelopeForTest(envelope).preferences;
    }""")


def access_state(page):
    return page.evaluate("""() => {
        const active = window.terminalTest.getActiveAgentState();
        return {socket:window.terminalTest.getSocketState(),policy:window.terminalTest.getTerminalPolicy(),
            agent:Object.fromEntries(['terminal_id','connected','session_id','viewer_id','agent_binding_id',
                'mode','mode_version','external_token'].map(key => [key,active[key]]))};
    }""")


def edit_preferences(page, next_locale):
    page.click('.settings-nav-item[data-tab="general"]')
    page.locator('#pref-copyOnSelect').check()
    page.locator('#pref-useCustomMenu').uncheck()
    page.select_option('#pref-urlClickAction', 'newtab')
    page.select_option('#pref-uiLanguage', next_locale)
    page.select_option('#pref-agentAccessMintMode', 'observe')
    page.locator('#pref-cjkWideAmbiguous').check()
    page.click('.settings-nav-item[data-tab="appearance"]')
    page.fill('#pref-fontFace', CUSTOM_FONT)
    page.locator('#pref-powerlineSymbols').uncheck()
    page.fill('#pref-fontSize', '22')
    page.select_option('#pref-fontWeight', '600')
    page.select_option('#pref-cursorStyle', 'bar')
    page.select_option('#pref-colorScheme', 'campbell')


def assert_no_access_mutations(page):
    assert not any(event['event'] in ACCESS_MUTATIONS for event in fixture.get_emitted(page)), \
        'A preference action changed access or started/stopped a connection'


def assert_ambiguous_character_width(page, width):
    page.evaluate('text => window.terminalTest.writeTerminalOutput(text)', '\x1b[2J\x1b[HA─B')
    expected = [{'chars':'A','width':1},{'chars':'─','width':width}]
    if width == 2:
        expected.append({'chars':'','width':0})
    expected.append({'chars':'B','width':1})
    for mirror in [False,True]:
        page.wait_for_function("""({mirror,expected}) => {
            const cells = window.terminalTest.getActiveTerminalBufferCellsForTest(0, mirror);
            return cells && JSON.stringify(cells.slice(0,expected.length)) === JSON.stringify(expected);
        }""", arg={'mirror':mirror,'expected':expected})


def test_close_discards_draft_and_save_applies_without_reload(browser, url):
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            before = snapshot(page)
            before_preferences = preferences(page)
            before_options = page.evaluate('() => window.terminalTest.getActiveTerminalOptions()')
            before_access = access_state(page)
            next_locale = 'zh-TW' if locale == 'en' else 'en'
            edit_preferences(page, next_locale)
            assert page.evaluate('() => window.terminalTest.getActiveTerminalOptions()') == before_options
            page.click('#settings-close')
            page.wait_for_selector('#settings-modal.open', state='hidden')
            page.click('#quick-settings')
            assert snapshot(page) == before
            assert preferences(page) == before_preferences
            assert page.evaluate('() => window.transferDocumentMarker') == 'original'
            assert page.input_value('#pref-uiLanguage') == locale
            assert page.locator('#pref-copyOnSelect').is_checked() is False
            assert page.locator('#pref-useCustomMenu').is_checked()
            assert page.input_value('#pref-urlClickAction') == 'overlay'
            assert page.input_value('#pref-agentAccessMintMode') == 'approval_pending'
            assert page.input_value('#pref-fontSize') == '18'
            assert page.input_value('#pref-fontWeight') == before_preferences['fontWeight']
            assert page.input_value('#pref-cursorStyle') == before_preferences['cursorStyle']
            assert page.input_value('#pref-colorScheme') == before_preferences['colorScheme']
            assert page.input_value('#pref-fontFace') == before_preferences['fontFace']
            assert page.locator('#pref-powerlineSymbols').is_checked()
            assert not page.locator('#pref-cjkWideAmbiguous').is_checked()
            assert_ambiguous_character_width(page, 1)
            assert access_state(page) == before_access

            edit_preferences(page, next_locale)
            page.evaluate('() => window.terminalTest.clearEmitted()')
            page.click('#settings-save')
            page.wait_for_selector('#settings-modal.open', state='hidden')
            saved = snapshot(page)
            expected = dict(before_preferences, uiLanguage=next_locale, copyOnSelect=True,
                            useCustomMenu=False, urlClickAction='newtab',agentAccessMintMode='observe',
                            fontSize=22,fontWeight='600',cursorStyle='bar',colorScheme='campbell',
                            fontFace=CUSTOM_FONT,powerlineSymbols=False,cjkWideAmbiguous=True)
            assert json.loads(saved['storage']['terminal.pref.v1']) == expected
            assert preferences(page) == expected
            options = page.evaluate('() => window.terminalTest.getActiveTerminalOptions()')
            for field in ['fontSize','fontWeight','cursorStyle','colorScheme']:
                assert options[field] == expected[field]
            if options['mirrorCursorStyle'] is not None:
                assert options['mirrorCursorStyle'] == 'bar'
            assert options['fontFamily'] == CUSTOM_FONT and options['mirrorFontFamily'] == CUSTOM_FONT
            assert_ambiguous_character_width(page, 2)
            assert saved['ssh'] == before['ssh'] and saved['key'] == key
            assert {name:value for name,value in saved['storage'].items() if name != 'terminal.pref.v1'} == {
                name:value for name,value in before['storage'].items() if name != 'terminal.pref.v1'}
            assert page.evaluate('() => window.transferDocumentMarker') == 'original'
            assert page.locator('html').get_attribute('lang') == locale
            assert access_state(page) == before_access
            assert_no_access_mutations(page)

            next_page = context.new_page()
            next_page.goto(fixture.debug_url(url), wait_until='domcontentloaded')
            next_page.wait_for_function('() => !!window.terminalTest')
            assert next_page.locator('html').get_attribute('lang') == next_locale
            assert preferences(next_page) == expected
            assert next_page.evaluate('() => window.terminalTest.getActiveTerminalOptions().fontSize') == 22
        finally:
            fixture.close_context(context)


def test_reset_reloads_only_preference_defaults(browser, url):
    baseline_context = browser.new_context(locale='en-US')
    try:
        baseline = baseline_context.new_page()
        baseline.goto(fixture.debug_url(url), wait_until='domcontentloaded')
        baseline.wait_for_function('() => !!window.terminalTest')
        defaults = preferences(baseline)
        assert defaults['uiLanguage'] == 'en'
    finally:
        fixture.close_context(baseline_context)
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            page.evaluate("() => localStorage.setItem('fixture.unrelated', 'Keep <b>& {value}')")
            before = snapshot(page)
            assert preferences(page) != defaults
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.click('#settings-reset')
            page.wait_for_function('() => !!window.terminalTest')
            after = snapshot(page)
            assert page.evaluate('() => performance.getEntriesByType("navigation")[0].type') == 'reload'
            assert page.evaluate('() => window.transferDocumentMarker === undefined')
            assert json.loads(after['storage']['terminal.pref.v1']) == defaults
            assert preferences(page) == defaults
            assert page.locator('html').get_attribute('lang') == 'en'
            assert after['ssh'] == before['ssh'] and after['key'] == key
            assert {name:value for name,value in after['storage'].items() if name != 'terminal.pref.v1'} == {
                name:value for name,value in before['storage'].items() if name != 'terminal.pref.v1'}
            assert page.evaluate('keyId => window.terminalTest.browserSshKeyRecordExistsForTest(keyId)', key['keyId'])
            assert_no_access_mutations(page)
        finally:
            fixture.close_context(context)


def assert_catalog_text(page, element):
    key = element.get_attribute('data-i18n')
    assert key, 'A localized settings element lacks its catalog key'
    assert element.inner_text() == message(page, key)


def assert_footer_fits(page):
    metrics = page.locator('.settings-footer').evaluate("""footer => {
        const box = document.querySelector('.settings-box').getBoundingClientRect();
        return [...footer.querySelectorAll('[data-i18n]')].map(element => {
            const rect = element.getBoundingClientRect();
            return {key:element.dataset.i18n,width:rect.width,height:rect.height,
                inside:rect.left >= Math.max(0,box.left)-1 && rect.right <= Math.min(innerWidth,box.right)+1
                    && rect.top >= Math.max(0,box.top)-1 && rect.bottom <= Math.min(innerHeight,box.bottom)+1,
                unclipped:element.scrollWidth <= element.clientWidth+1 && element.scrollHeight <= element.clientHeight+1};
        });
    }""")
    assert metrics and all(item['width'] > 0 and item['height'] > 0 and item['inside'] and item['unclipped']
                           for item in metrics), metrics


def test_navigation_labels_and_footer_guidance_fit_both_widths(browser, url):
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            nav = page.locator('.settings-nav-item')
            assert nav.evaluate_all('items => items.map(item => item.dataset.tab)') == TABS
            for index in range(nav.count()):
                assert_catalog_text(page, nav.nth(index))
            assert_catalog_text(page, page.locator('#settings-modal .settings-header h3'))
            for button_id in ['settings-save','settings-reset']:
                assert_catalog_text(page, page.locator('#' + button_id))
            close = page.locator('#settings-close')
            close_key = close.get_attribute('data-i18n-aria-label')
            assert close_key and close.get_attribute('aria-label') == message(page, close_key)
            hints = page.locator('.settings-footer #settings-save-hint, .settings-footer #settings-reset-hint')
            assert hints.count() == 2, 'Preference consequences are missing from the persistent footer'
            for index in range(hints.count()):
                assert_catalog_text(page, hints.nth(index))
            assert page.locator('#settings-save').get_attribute('aria-describedby') == 'settings-save-hint'
            assert page.locator('#settings-reset').get_attribute('aria-describedby') == 'settings-reset-hint'
            for width, height in [(1280,800),(480,800),(480,600)]:
                page.set_viewport_size({'width':width,'height':height})
                for tab in TABS:
                    page.click(f'.settings-nav-item[data-tab="{tab}"]')
                    assert page.locator('.settings-tab.active').get_attribute('id') == 'tab-' + tab
                    assert page.locator('.settings-nav-item.active').get_attribute('data-tab') == tab
                    for end in [False,True]:
                        page.locator('.settings-content').evaluate('(element,end) => {element.scrollTop = end ? element.scrollHeight : 0;}', end)
                        assert_footer_fits(page)
            assert_no_access_mutations(page)
        finally:
            fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for test in [test_close_discards_draft_and_save_applies_without_reload,
                             test_reset_reloads_only_preference_defaults,
                             test_navigation_labels_and_footer_guidance_fit_both_widths]:
                    test(browser, url)
                    print(test.__name__ + ': ok', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
