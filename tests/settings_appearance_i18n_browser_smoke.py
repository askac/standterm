"""Check localized preference labels and palette previews against typed values."""
import agent_browser_smoke as fixture
from settings_preferences_i18n_browser_smoke import preferences
from settings_transfer_i18n_browser_smoke import message, new_page, snapshot


SELECT_VALUES = {
    'pref-uiLanguage':['en','zh-TW'],
    'pref-urlClickAction':['overlay','popup','newtab'],
    'pref-agentAccessMintMode':['observe','approval_pending','direct_active'],
    'pref-colorScheme':['ibm5153','campbell','campbellPowershell','vintage','oneHalfDark',
                       'oneHalfLight','solarizedDark','solarizedLight','tangoDark','tangoLight'],
    'pref-fontWeight':['normal','500','600','bold'],
    'pref-cursorStyle':['block','underline','bar'],
}
THEME_NAMES = ['IBM 5153 (CGA Classic)','Campbell','Campbell PowerShell','Vintage','One Half Dark',
               'One Half Light','Solarized Dark','Solarized Light','Tango Dark','Tango Light']
COLORS = ['black','red','green','yellow','blue','magenta','cyan','white']
PALETTES = {
    'ibm5153':['#000000','#aa0000','#00aa00','#aa5500','#0000aa','#aa00aa','#00aaaa','#aaaaaa',
               '#555555','#ff5555','#55ff55','#ffff55','#5555ff','#ff55ff','#55ffff','#ffffff'],
    'campbell':['#0C0C0C','#C50F1F','#13A10E','#C19C00','#0037DA','#881798','#3A96DD','#CCCCCC',
                '#767676','#E74856','#16C60C','#F9F1A5','#3B78FF','#B4009E','#61D6D6','#F2F2F2'],
}


def assert_palette(page, scheme):
    palette = page.locator('.settings-theme-palette')
    assert palette.locator(':scope > span:not(.settings-theme-swatch)').all_text_contents() == [
        message(page, 'settings.appearance.palette_normal'), message(page, 'settings.appearance.palette_bright')]
    swatches = palette.locator('.settings-theme-swatch')
    assert swatches.count() == 16
    for index, hex_value in enumerate(PALETTES[scheme]):
        swatch = swatches.nth(index)
        color = message(page, 'settings.appearance.color_' + COLORS[index % 8])
        label = message(page, 'settings.appearance.swatch_bright' if index >= 8 else 'settings.appearance.swatch',
                        {'color':color,'hex':hex_value})
        assert swatch.get_attribute('role') == 'img'
        assert swatch.get_attribute('title') == label and swatch.get_attribute('aria-label') == label
        rgb = ', '.join(str(int(hex_value[offset:offset + 2], 16)) for offset in [1,3,5])
        assert swatch.evaluate('element => element.style.backgroundColor') == 'rgb(' + rgb + ')'


def assert_translated_attributes(page, tab):
    elements = page.locator(f'#tab-{tab} [data-i18n], #tab-{tab} [data-i18n-title], #tab-{tab} [data-i18n-aria-label], #tab-{tab} [data-i18n-label]')
    for index in range(elements.count()):
        element = elements.nth(index)
        for catalog_attribute, attribute in [('data-i18n','textContent'),('data-i18n-title','title'),
                                             ('data-i18n-aria-label','aria-label'),('data-i18n-label','label')]:
            key = element.get_attribute(catalog_attribute)
            if key:
                actual = element.text_content() if attribute == 'textContent' else element.get_attribute(attribute)
                assert actual == message(page, key), (key, actual)


def test_translated_controls_and_palette_keep_typed_values(browser, url):
    for locale in ['en','zh-TW']:
        context, page, key = new_page(browser, url, locale)
        try:
            before = snapshot(page)
            runtime = page.evaluate('() => window.terminalTest.getActiveTerminalOptions()')
            stored_preferences = preferences(page)
            for selector, expected in SELECT_VALUES.items():
                assert page.locator('#' + selector + ' option').evaluate_all('items => items.map(item => item.value)') == expected
            assert page.locator('#pref-colorScheme option').all_text_contents() == THEME_NAMES
            for tab in ['general','appearance']:
                page.click(f'.settings-nav-item[data-tab="{tab}"]')
                labels = page.locator(f'#tab-{tab} .settings-row label[for]')
                for index in range(labels.count()):
                    assert labels.nth(index).get_attribute('data-i18n'), 'A preference label lost localization or its control binding'
                assert_translated_attributes(page, tab)
            for scheme in PALETTES:
                if scheme == 'campbell':
                    page.locator('#pref-colorScheme option[value="campbell"]').evaluate(
                        'element => {element.textContent = "Display <b>& {scheme}";}')
                page.select_option('#pref-colorScheme', scheme)
                assert_palette(page, scheme)
                assert page.input_value('#pref-colorScheme') == scheme
                assert page.evaluate('() => window.terminalTest.getActiveTerminalOptions()') == runtime
                assert preferences(page) == stored_preferences
                assert snapshot(page) == before
            assert page.locator('#pref-colorScheme b').count() == 0
            page.locator('#pref-colorScheme option[value="campbell"]').evaluate('element => {element.textContent = "Campbell";}')
            entered_values = page.locator('#tab-general input[id^="pref-"], #tab-general select, #tab-appearance input, #tab-appearance select').evaluate_all(
                'items => items.map(item => ({id:item.id,value:item.value,checked:item.checked}))')
            page.set_viewport_size({'width':480,'height':600})
            for tab in ['general','appearance']:
                page.click(f'.settings-nav-item[data-tab="{tab}"]')
                labels = page.locator(f'#tab-{tab} .settings-row label[for]')
                for index in range(labels.count()):
                    label = labels.nth(index)
                    label.scroll_into_view_if_needed()
                    metrics = label.evaluate("""label => {
                        const field = document.getElementById(label.htmlFor);
                        const a = label.getBoundingClientRect(), b = field.getBoundingClientRect();
                        const row = label.closest('.settings-row').getBoundingClientRect();
                        return {id:label.htmlFor,labelFits:label.scrollWidth <= label.clientWidth+1,
                            inRow:a.left >= row.left-1 && a.right <= row.right+1 && b.left >= row.left-1 && b.right <= row.right+1,
                            separate:a.right <= b.left+1 || a.bottom <= b.top+1 || b.bottom <= a.top+1};
                    }""")
                    assert metrics['labelFits'] and metrics['inRow'] and metrics['separate'], metrics
            assert page.locator('#tab-general input[id^="pref-"], #tab-general select, #tab-appearance input, #tab-appearance select').evaluate_all(
                'items => items.map(item => ({id:item.id,value:item.value,checked:item.checked}))') == entered_values
        finally:
            fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                test_translated_controls_and_palette_keep_typed_values(browser, url)
                print('test_translated_controls_and_palette_keep_typed_values (en/zh-TW): PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
