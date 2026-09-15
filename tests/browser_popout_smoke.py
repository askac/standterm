"""Verify the non-Document-PiP path with actual popup input and lifecycle."""
import argparse
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import agent_browser_smoke as fixture


def open_terminal(page):
    active = page.evaluate('() => window.terminalTest.getTerminalTabsState().activeTerminalId')
    page.evaluate('id => window.terminalTest.showContextMenuForTest(id)', active)
    assert 'Pop out' in page.locator('#pip-option').inner_text()
    with page.expect_popup() as opened:
        page.click('#pip-option')
    popup = opened.value
    popup.locator('.pip-terminal-host').wait_for()
    assert popup.url == 'about:blank'
    assert popup.evaluate('window.opener === null')
    return popup, active


def send_title(popup, title):
    popup.locator('.xterm-helper-textarea').focus()
    popup.keyboard.type(f"printf '\\033]2;{title}\\007'\n")
    popup.wait_for_function('title => document.querySelector(".pip-application-title").textContent === title', arg=title)


def wait_released(page, terminal_id=None):
    page.wait_for_function('() => !window.terminalTest.getFloatingWindowForTest()')
    if terminal_id:
        page.wait_for_function(
            'id => !window.terminalTest.getTerminalTabsState().tabs.find(t => t.id === id).inPip',
            arg=terminal_id,
        )


def select_terminal(page, terminal_id):
    page.click(f'.terminal-tab[data-terminal-id="{terminal_id}"]')


def navigate_and_wait_released(page, popup, terminal_id, destination=None, allow_close_quirk=False):
    try:
        with popup.expect_event('close', timeout=5000):
            popup.evaluate(
                'url => setTimeout(() => url ? location.assign(url) : location.reload(), 0)',
                destination,
            )
    except PlaywrightTimeoutError:
        if not allow_close_quirk:
            raise
        # Linux WPE may refuse script-close after navigation. Restoring and
        # releasing the original terminal is still required; report the gap.
        wait_released(page, terminal_id)
        assert popup.locator('.pip-terminal-host').count() == 0
        popup.close()
        print('LIMITATION: navigated popup restored its terminal but required browser close.', flush=True)
        return 1
    wait_released(page, terminal_id)
    return 0


def test_popup_input_files_and_cleanup(browser, url, allow_close_quirk=False):
    print('Popup smoke: opening the connected Core page.', flush=True)
    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    navigation_close_gaps = 0
    try:
        page.evaluate("Object.defineProperty(window, 'documentPictureInPicture', {value: undefined, configurable: true})")
        page.click('#new-tab-btn')
        page.click('#connectBtn')
        page.wait_for_function('() => window.terminalTest.getActiveAgentState()?.connected === true')
        popup, active = open_terminal(page)
        popup.on('pageerror', lambda error: errors.append(str(error)))
        send_title(popup, 'POPUP_INPUT_OK')
        print('Popup smoke: child input passed.', flush=True)
        old_size = popup.locator('.pip-title-meta').inner_text()
        popup.set_viewport_size({'width': 500, 'height': 300})
        popup.wait_for_function('old => document.querySelector(".pip-title-meta").textContent !== old', arg=old_size)
        # A trusted click occurs inside the child. It must not create another popup.
        count = len(context.pages)
        popup.locator('.pip-sftp-button').click()
        popup.locator('.sftp-pip-title').wait_for()
        assert len(context.pages) == count
        assert popup.locator('.sftp-pip-title').inner_text() == 'StandTerm - Files'
        print('Popup smoke: trusted Files transition passed.', flush=True)
        page.wait_for_function('id => !window.terminalTest.getTerminalTabsState().tabs.find(t => t.id === id).inPip', arg=active)
        popup.close()
        wait_released(page, active)
        select_terminal(page, active)
        # Native close restores an interactive terminal to its parent document.
        page.locator('.terminal-pane.active .xterm-helper-textarea').focus()
        page.keyboard.type("printf '\\033]2;RESTORED_INPUT_OK\\007'\n")
        page.wait_for_function('() => document.getElementById("terminal-title").textContent === "RESTORED_INPUT_OK"')
        popup, _ = open_terminal(page)
        navigation_close_gaps += navigate_and_wait_released(page, popup, active, allow_close_quirk=allow_close_quirk)
        select_terminal(page, active)
        popup, _ = open_terminal(page)
        parsed = urlsplit(url)
        navigation_close_gaps += navigate_and_wait_released(
            page, popup, active, f'{parsed.scheme}://{parsed.netloc}/robots.txt', allow_close_quirk,
        )
        select_terminal(page, active)
        popup, _ = open_terminal(page)
        popup.close()
        wait_released(page, active)
        select_terminal(page, active)

        # Blocked admission keeps the terminal visible, then a new click retries.
        print('Popup smoke: close, reload and navigation restored terminal state.', flush=True)
        page.evaluate("""() => {
            window.__popupOriginalOpen = window.open;
            window.__popupAlerts = [];
            window.alert = value => window.__popupAlerts.push(value);
            window.open = () => null;
            window.terminalTest.showContextMenuForTest(window.terminalTest.getTerminalTabsState().activeTerminalId);
        }""")
        page.click('#pip-option')
        page.wait_for_function('() => window.__popupAlerts.length === 1')
        assert 'Allow pop-up windows' in page.evaluate('window.__popupAlerts[0]')
        wait_released(page, active)
        page.evaluate('() => { window.open = window.__popupOriginalOpen; }')
        popup, _ = open_terminal(page)
        send_title(popup, 'RETRY_INPUT_OK')
        print('Popup smoke: blocked popup retry passed.', flush=True)
        popup.close()
        wait_released(page, active)
        select_terminal(page, active)
        assert len(context.pages) == 1, [(item.url, item.is_closed()) for item in context.pages]

        # Reentrant handlers in the same trusted click must share one admission.
        page.evaluate("""() => {
            window.__popupOpenCount = 0;
            window.open = (...args) => {
                window.__popupOpenCount++;
                return window.__popupOriginalOpen(...args);
            };
            document.getElementById('pip-option').addEventListener('click', () => {
                document.getElementById('pip-option').click();
                document.getElementById('sftp-status-btn').click();
            }, {once: true});
        }""")
        popup, _ = open_terminal(page)
        assert page.evaluate('window.__popupOpenCount') == 1
        assert len(context.pages) == 2, [(item.url, item.is_closed()) for item in context.pages]
        popup.close()
        wait_released(page, active)
        select_terminal(page, active)
        page.evaluate('() => { window.open = window.__popupOriginalOpen; }')

        popup, _ = open_terminal(page)
        with popup.expect_event('close'):
            page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest')
        assert not errors, errors
        return navigation_close_gaps
    finally:
        fixture.close_context(context)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', choices=['chromium', 'webkit'], default='chromium')
    parser.add_argument('--executable')
    parser.add_argument('--allow-navigation-close-quirk', action='store_true',
                        help='Report a navigated Linux WPE popup requiring browser close instead of failing.')
    args = parser.parse_args()
    server, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            options = {'headless': True}
            if args.executable:
                options['executable_path'] = args.executable
            browser = getattr(playwright, args.browser).launch(**options)
            try:
                gaps = test_popup_input_files_and_cleanup(browser, url, args.allow_navigation_close_quirk)
                print(f'{args.browser} popup input, Files and restore: PASS; navigation auto-close gaps: {gaps}')
            finally:
                browser.close()
    finally:
        fixture.stop_server(server)
