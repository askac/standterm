"""Exercise Desktop layouts with live shells, pane actions, and resize events."""

import agent_browser_smoke as fixture


def tabs(page):
    return page.evaluate('window.terminalTest.getTerminalTabsState()')


def pane(page, terminal_id):
    return page.locator(f'.terminal-pane[data-terminal-id="{terminal_id}"]')


def layout(page, action):
    assert page.evaluate('action => window.standtermUi.run(action, window.standtermUi.snapshot().terminalId)', action)
    page.wait_for_function('() => [...document.querySelectorAll(".terminal-pane.split-pane")].every(p => p.clientWidth > 0)')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def connect_tab(page):
    page.click('#new-tab-btn')
    page.click('#connectBtn')
    page.wait_for_function('window.terminalTest.getActiveAgentState().connected')
    return tabs(page)['activeTerminalId']


def auto_width(page, enabled):
    page.locator('#tab-more > summary').click()
    page.locator('#terminal-auto-width').set_checked(enabled)
    page.locator('#tab-more > summary').click()
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def open_actions(page, terminal_id):
    pane(page, terminal_id).locator('.terminal-pane-select').click()
    page.locator('#tab-more > summary').click()
    page.locator('#terminal-more-actions').wait_for(state='visible')


def assert_mirrors(page):
    for state in tabs(page)['tabs']:
        assert (state['cols'], state['rows']) == (state['mirrorCols'], state['mirrorRows'])


def test_all_tabs_layout_focus_and_resize(browser, url):
    for locale in ['en', 'zh-TW']:
        context, page = fixture.new_page(browser, url, ui_language=locale)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            second = connect_tab(page)
            third = connect_tab(page)
            original = tabs(page)
            fixture.clear_emitted(page)
            layout(page, 'layoutVertical')
            assert page.locator('.terminal-pane.split-pane:visible').count() == 3
            assert page.locator('.terminal-splitter').count() == 2
            assert page.locator('.terminal-pane-more').count() == 0
            assert page.locator('#tab-more > summary').count() == 1
            assert page.locator('#terminal-tabs [data-terminal-action]:visible').count() == 0
            assert page.locator('#quick-settings').is_visible()
            boxes = [pane(page, item['id']).bounding_box() for item in original['tabs']]
            assert boxes[0]['x'] < boxes[1]['x'] < boxes[2]['x']
            assert all(abs(box['y'] - boxes[0]['y']) < 1 for box in boxes)
            for state in tabs(page)['tabs']:
                assert state['cols'] < original['tabs'][-1]['cols']
                assert pane(page, state['id']).locator('.terminal-pane-id').inner_text() == state['id']
            assert_mirrors(page)
            resized = {event['args'][0]['terminal_id'] for event in fixture.get_emitted(page, 'resize')}
            assert resized == {'main', second, third}
            assert not [event for event in fixture.get_emitted(page)
                        if event['event'] in {'start_ssh', 'close_terminal', 'agent_mode_set', 'agent_attach'}]

            # Real keyboard input and toolbar actions must follow their own pane.
            for terminal_id in ['main', second]:
                target = pane(page, terminal_id)
                target.locator('.xterm-helper-textarea').focus()
                fixture.clear_emitted(page)
                page.keyboard.type(f"printf '\\033]2;LAYOUT_{terminal_id}\\007'\n")
                page.wait_for_function('title => document.getElementById("terminal-title").textContent === title',
                                       arg=f'LAYOUT_{terminal_id}')
                inputs = fixture.get_emitted(page, 'ssh_input')
                assert inputs and all(event['args'][0]['terminal_id'] == terminal_id for event in inputs)
                assert tabs(page)['activeTerminalId'] == terminal_id
                assert target.locator('.terminal-pane-focus').is_visible()
            open_actions(page, 'main')
            assert page.locator('#terminal-more-actions [data-source-id="quick-settings"]').count() == 0
            assert page.locator('#terminal-more-actions [data-source-id="transfer-queue-btn"]').count() == 0
            page.locator('#terminal-more-actions [data-source-id="agent-toggle-btn"]').click()
            assert tabs(page)['agentPanelTerminalId'] == 'main'
            assert page.locator('#agent-panel').is_visible()
            page.click('#agent-panel-close-btn')
            open_actions(page, 'main')
            page.evaluate('window.staleLayoutAction = document.querySelector("#terminal-more-actions [data-source-id=agent-toggle-btn]")')
            pane(page, second).locator('.terminal-pane-select').click()
            assert not page.locator('#tab-more').evaluate('element => element.open')
            page.evaluate('window.staleLayoutAction.click()')
            assert not page.locator('#agent-panel').is_visible()
            open_actions(page, second)
            assert second in page.locator('#terminal-more-actions strong').inner_text()
            page.keyboard.press('Escape')

            splitter = page.locator('.terminal-splitter').first
            bounds = splitter.bounding_box()
            before = pane(page, 'main').bounding_box()['width']
            page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 50)
            page.mouse.down()
            page.mouse.move(bounds['x'] + 75, bounds['y'] + 50, steps=5)
            page.mouse.up()
            assert pane(page, 'main').bounding_box()['width'] > before + 50
            assert_mirrors(page)
            layout(page, 'layoutHorizontal')
            boxes = [pane(page, item['id']).bounding_box() for item in original['tabs']]
            assert boxes[0]['y'] < boxes[1]['y'] < boxes[2]['y']
            assert all(abs(box['x'] - boxes[0]['x']) < 1 for box in boxes)
            assert pane(page, 'main').locator('.terminal-pane-quick').is_visible()
            open_actions(page, 'main')
            for source_id in ['agent-access-mint-btn', 'agent-toggle-btn', 'sftp-status-btn']:
                assert page.locator(f'#terminal-more-actions [data-source-id="{source_id}"]').count() == 0
            assert page.locator('#terminal-more-actions [data-source-id="agent-connect-btn"]').is_visible()
            page.keyboard.press('Escape')

            # Pause remains directly available on the exact pane while its main-toolbar button is hidden.
            fixture.attach_agent(page)
            fixture.emit_socket(page, 'agent_mode_set', {'terminal_id': 'main', 'mode': 'direct'})
            fixture.wait_for_agent(page, "state.mode === 'direct_active'")
            assert not page.locator('#agent-pause-btn').is_visible()
            assert pane(page, 'main').locator('.terminal-pane-pause').is_visible()
            assert not pane(page, second).locator('.terminal-pane-pause').is_visible()
            fixture.clear_emitted(page)
            pane(page, 'main').locator('.terminal-pane-pause').click()
            fixture.wait_for_agent(page, "state.mode === 'paused'")
            assert [event['args'][0]['terminal_id'] for event in fixture.get_emitted(page, 'agent_pause')] == ['main']

            before = boxes[0]['height']
            page.locator('.terminal-splitter').first.focus()
            page.keyboard.press('ArrowDown')
            assert pane(page, 'main').bounding_box()['height'] > before
            layout(page, 'layoutFull')
            assert page.locator('.terminal-pane:visible').count() == 1
            assert page.locator('.terminal-pane-heading:visible').count() == 0
            assert page.locator('.terminal-splitter').count() == 0
            assert page.locator('#agent-access-mint-btn').is_visible()
            assert page.locator('#agent-toggle-btn').is_visible()
            page.locator('#tab-more > summary').click()
            assert not page.locator('#terminal-more-actions').is_visible()
            page.locator('#tab-more > summary').click()
            assert [item['instanceId'] for item in tabs(page)['tabs']] == [item['instanceId'] for item in original['tabs']]
            assert_mirrors(page)
            assert not errors, errors
        finally:
            fixture.close_context(context)


def test_fixed_columns_and_narrow_pane_actions(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        second = connect_tab(page)
        original = {item['id']: item['cols'] for item in tabs(page)['tabs']}
        auto_width(page, False)
        layout(page, 'layoutVertical')
        for item in tabs(page)['tabs']:
            assert item['cols'] == original[item['id']]
            assert pane(page, item['id']).locator('.terminal-viewport').evaluate('e => e.scrollWidth > e.clientWidth')
        page.set_viewport_size({'width': 480, 'height': 620})
        page.wait_for_function('document.getElementById("terminal").clientWidth === 480')
        for item in tabs(page)['tabs']:
            assert item['cols'] == original[item['id']]
            target = pane(page, item['id'])
            assert target.locator('.terminal-pane-select').is_visible()
            assert not target.locator('.terminal-pane-quick').is_visible()
            assert target.locator('.terminal-pane-heading').evaluate('e => e.scrollWidth <= e.clientWidth')
            open_actions(page, item['id'])
            menu = page.locator('#tab-more-actions')
            box = menu.bounding_box()
            assert 0 <= box['x'] and box['x'] + box['width'] <= 480
            assert 0 <= box['y'] and box['y'] + box['height'] <= 620
            assert item['id'] in menu.locator('strong').inner_text()
            page.keyboard.press('Escape')
        assert_mirrors(page)
        page.set_viewport_size({'width': 1280, 'height': 800})
        layout(page, 'layoutHorizontal')
        assert all(item['cols'] == original[item['id']] for item in tabs(page)['tabs'])
        auto_width(page, True)
        layout(page, 'layoutVertical')
        assert all(item['cols'] < original[item['id']] for item in tabs(page)['tabs'])
        assert_mirrors(page)
        assert page.evaluate('JSON.parse(localStorage.getItem("terminal.pref.v1")).splitAutoFitWidth') is True
        assert tabs(page)['activeTerminalId'] == second
    finally:
        fixture.close_context(context)


def test_layout_tab_lifecycle_and_popout(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        for action in ['layoutVertical', 'layoutHorizontal']:
            assert not page.evaluate('action => window.standtermUi.snapshot().actions[action]', action)
            assert not page.evaluate('action => window.standtermUi.run(action, window.standtermUi.snapshot().terminalId)', action)
        second = connect_tab(page)
        layout(page, 'layoutVertical')
        third = connect_tab(page)
        assert page.locator('.terminal-pane.split-pane:visible').count() == 3
        page.locator(f'.terminal-tab[data-terminal-id="{second}"] .tab-close').click()
        assert page.locator('.terminal-pane.split-pane:visible').count() == 2
        assert page.locator('.terminal-splitter').count() == 1
        # Test the popup fallback used when Document PiP is unavailable.
        page.evaluate("Object.defineProperty(window, 'documentPictureInPicture', {value: undefined, configurable: true})")
        open_actions(page, third)
        with page.expect_popup() as opened:
            page.locator('#terminal-more-actions [data-source-id="pip-option"]').click()
        popup = opened.value
        popup.locator('.pip-terminal-host .xterm').wait_for()
        assert page.evaluate('window.standtermUi.snapshot().layout') == 'single'
        assert page.locator('.terminal-pane:visible').count() == 1
        assert page.locator('.terminal-splitter').count() == 0
        for action in ['layoutVertical', 'layoutHorizontal']:
            assert not page.evaluate('action => window.standtermUi.snapshot().actions[action]', action)
        assert not popup.locator('.terminal-pane-heading').is_visible()
        popup.close()
        page.wait_for_function('window.standtermUi.snapshot().actions.layoutVertical')
        assert page.evaluate('window.standtermUi.snapshot().layout') == 'single'
        layout(page, 'layoutHorizontal')
        assert page.locator('.terminal-splitter').count() == 1
        assert_mirrors(page)
        assert not page.evaluate("window.standtermUi.run('layoutVertical', 'stale-terminal')")
        page.locator(f'.terminal-tab[data-terminal-id="{third}"] .tab-close').click()
        assert page.evaluate('window.standtermUi.snapshot().layout') == 'single'
        assert page.locator('.terminal-splitter').count() == 0
        assert page.locator('#agent-toggle-btn').is_visible()
        for action in ['layoutVertical', 'layoutHorizontal']:
            assert not page.evaluate('action => window.standtermUi.snapshot().actions[action]', action)
        connect_tab(page)
        layout(page, 'layoutVertical')
        assert page.locator('.terminal-pane.split-pane:visible').count() == 2
    finally:
        fixture.close_context(context)


def main():
    sync_playwright, _, _ = fixture.load_playwright()
    proc, url = fixture.start_server(desktop=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for test in [test_all_tabs_layout_focus_and_resize, test_fixed_columns_and_narrow_pane_actions,
                             test_layout_tab_lifecycle_and_popout]:
                    test(browser, url)
                    print(f'{test.__name__}: ok', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)
    proc, url = fixture.start_server()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = fixture.new_page(browser, url)
            try:
                connect_tab(page)
                assert page.locator('#terminal-auto-width').count() == 0
                for action in ['layoutFull', 'layoutVertical', 'layoutHorizontal']:
                    assert not page.evaluate('action => window.standtermUi.run(action, window.standtermUi.snapshot().terminalId)', action)
                assert page.locator('.terminal-pane:visible').count() == 1
                print('browser_keeps_single_tab: ok', flush=True)
            finally:
                fixture.close_context(context)
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
