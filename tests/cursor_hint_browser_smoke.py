"""Check the optional cursor hint against real xterm rendering and preferences."""
import agent_browser_smoke as fixture


HINT = '.terminal-pane.active .terminal-cursor-hint'
AREA = '.terminal-pane.active .xterm-helper-textarea'


def open_appearance(page):
    page.click('#quick-settings')
    page.click('.settings-nav-item[data-tab="appearance"]')


def write(page, text):
    page.evaluate('text => window.terminalTest.writeTerminalOutput(text)', text)


def expect_position(page, column, row, triangle=False):
    page.wait_for_function("""({column, row, triangle}) => {
        const hint = document.querySelector('.terminal-pane.active .terminal-cursor-hint');
        const screen = hint.parentElement;
        const tab = window.terminalTest.getTerminalTabsState().tabs.find(tab => tab.active);
        return !hint.hidden && getComputedStyle(hint).display !== 'none'
            && Math.abs(parseFloat(hint.style.left) - (triangle ? column * parseFloat(screen.style.width) / tab.cols - 5 : Math.max(0, column * parseFloat(screen.style.width) / tab.cols - 3))) < 0.1
            && Math.abs(parseFloat(hint.style.top) - Math.max(0, (row + (triangle ? 1 : 0)) * parseFloat(screen.style.height) / tab.rows - (triangle ? 0 : 7))) < 0.1;
    }""", arg={'column': column, 'row': row, 'triangle': triangle})


def test_cursor_hint_preferences_and_geometry(browser, url):
    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.evaluate('window.terminalTest.captureTerminalIoForTest()')
        assert not page.locator(HINT).is_visible(), 'Cursor hint must default to off'
        open_appearance(page)
        assert not page.is_checked('#pref-cursorHint')
        assert page.is_disabled('#pref-cursorHintColor')
        swatches = page.locator('#cursor-hint-palette button')
        assert swatches.count() == 16
        assert swatches.evaluate_all('items => items.every(item => item.disabled)')
        page.check('#pref-cursorHint')
        swatches.nth(14).click()
        assert page.input_value('#pref-cursorHintColor') == '#00ffff'
        assert swatches.nth(14).get_attribute('aria-pressed') == 'true'
        page.fill('#pref-cursorHintColor', '#ff5500')
        assert page.locator('#cursor-hint-palette button[aria-pressed="true"]').count() == 0
        page.uncheck('#pref-cursorBlink')
        page.click('#settings-save')
        page.locator(AREA).focus()
        write(page, '\x1b[2J\x1b[?25h\x1b[4;9H')
        expect_position(page, 8, 3)
        assert page.locator(HINT).evaluate("e => getComputedStyle(e).backgroundColor") == 'rgb(255, 85, 0)'
        assert page.locator(HINT).get_attribute('aria-hidden') == 'true'
        assert page.locator(HINT).evaluate("e => getComputedStyle(e).pointerEvents") == 'none'
        assert page.locator(HINT).evaluate("e => getComputedStyle(e).opacity") == '0.9'
        assert page.locator(HINT).evaluate("e => getComputedStyle(e).backgroundImage") == 'none'
        assert page.locator(HINT).evaluate("e => getComputedStyle(e).boxShadow") == 'none'
        assert not page.evaluate('window.terminalTest.getActiveTerminalOptions().cursorBlink')
        # The hint stays visible while the normal cursor is steady or blinking.
        page.wait_for_timeout(1200)
        assert page.locator(HINT).is_visible()
        write(page, 'AB')
        expect_position(page, 10, 3)
        write(page, '\x1b[?25l')
        page.wait_for_function("document.querySelector('.terminal-pane.active .terminal-cursor-hint').hidden")
        write(page, '\x1b[?25h\x1b[?1049h\x1b[8;5H')
        expect_position(page, 4, 7)
        write(page, '\x1b[?1049l')
        expect_position(page, 10, 3)

        page.set_viewport_size({'width': 1050, 'height': 720})
        expect_position(page, 10, 3)
        open_appearance(page)
        assert not page.locator(HINT).is_visible(), 'Settings must not leave a focused hint behind'
        page.fill('#pref-fontSize', '20')
        page.check('#pref-cursorBlink')
        page.click('#settings-save')
        page.locator(AREA).focus()
        expect_position(page, 10, 3)
        assert page.evaluate('window.terminalTest.getActiveTerminalOptions().cursorBlink')
        page.wait_for_timeout(1200)
        assert page.locator(HINT).is_visible()

        write(page, '\r\n' * 100)
        page.wait_for_timeout(100)
        page.evaluate('window.terminalTest.scrollVisibleTerminal(-1000)')
        page.wait_for_function("document.querySelector('.terminal-pane.active .terminal-cursor-hint').hidden")
        page.evaluate('window.terminalTest.scrollVisibleTerminal(1000)')
        page.wait_for_selector(HINT, state='visible')
        write(page, '\x1b[H')
        expect_position(page, 0, 0)

        page.click('#new-tab-btn')
        page.wait_for_function('window.terminalTest.getTerminalTabsState().tabs.length === 2')
        page.locator(AREA).focus()
        write(page, '\x1b[?25h\x1b[3;4H')
        expect_position(page, 3, 2)
        assert page.locator('.terminal-cursor-hint:visible').count() == 1
        assert page.locator(HINT).evaluate("e => getComputedStyle(e).backgroundColor") == 'rgb(255, 85, 0)'
        page.locator('.terminal-tab.active .tab-close').click()
        page.wait_for_function('window.terminalTest.getTerminalTabsState().tabs.length === 1')
        assert page.locator('.terminal-cursor-hint').count() == 1, 'Closed tab retained its hint'

        open_appearance(page)
        page.select_option('#pref-cursorHintStyle', 'triangle')
        page.click('#settings-save')
        page.locator(AREA).focus()
        write(page, '\x1b[4;9H')
        expect_position(page, 8, 3, triangle=True)
        assert page.locator(HINT).get_attribute('data-style') == 'triangle'
        write(page, '\x1b[6;20H')
        expect_position(page, 19, 5, triangle=True)
        # The upward tip meets the cursor's lower-left corner, including screen edges.
        last_row = page.evaluate('window.terminalTest.getTerminalTabsState().tabs.find(tab => tab.active).rows')
        write(page, f'\x1b[{last_row};1H')
        expect_position(page, 0, last_row - 1, triangle=True)
        assert page.locator(HINT).evaluate("""hint => {
            const bounds = hint.getBoundingClientRect();
            const viewport = hint.closest('.terminal-viewport').getBoundingClientRect();
            return bounds.left >= viewport.left && bounds.bottom <= viewport.bottom;
        }"""), 'Triangle clipped at the first column or last row'

        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('!!window.terminalTest')
        open_appearance(page)
        assert page.is_checked('#pref-cursorHint')
        assert page.input_value('#pref-cursorHintStyle') == 'triangle'
        assert page.input_value('#pref-cursorHintColor') == '#ff5500'
        page.uncheck('#pref-cursorHint')
        page.click('#settings-save')
        page.locator(AREA).focus()
        assert not page.locator(HINT).is_visible(), 'Disabling the hint did not apply immediately'
        assert not errors, errors
    finally:
        fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                test_cursor_hint_preferences_and_geometry(browser, url)
                print('test_cursor_hint_preferences_and_geometry: PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
