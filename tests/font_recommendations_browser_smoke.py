"""Verify font geometry, draft selection, and independent preview cleanup."""
import json
import math

import agent_browser_smoke as fixture
from settings_transfer_i18n_browser_smoke import message


CARDS = '#font-recommendation-results .font-recommendation-card'
MEASURE = '#font-recommendation-measure'


def open_appearance(page):
    page.click('#quick-settings')
    page.click('.settings-nav-item[data-tab="appearance"]')


def measure(page):
    page.click(MEASURE)
    page.wait_for_function("!document.getElementById('font-recommendation-measure').disabled")


def runtime(page):
    return page.evaluate('() => window.terminalTest.getActiveTerminalOptions()')


def storage(page):
    return page.evaluate("localStorage.getItem('terminal.pref.v1')")


def assert_no_terminal_mutations(page):
    assert not [event for event in fixture.get_emitted(page)
                if event['event'] in {'resize', 'ssh_input', 'start_ssh', 'agent_mode_set'}]


def test_live_geometry_and_explicit_save(browser, url):
    for locale in ['en', 'zh-TW']:
        context, page = fixture.new_page(browser, url, ui_language=locale)
        try:
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            baseline = runtime(page)
            stored = storage(page)
            size = page.locator('#terminal-size').inner_text()
            cols, rows = map(int, size.split('x'))
            screen = page.locator('.terminal-pane.active .xterm-screen').bounding_box()
            open_appearance(page)
            fixture.clear_emitted(page)
            measure(page)
            current = json.loads(page.locator(CARDS + '[data-font-index="0"]').get_attribute('data-metrics'))
            assert (current['cols'], current['rows']) == (cols, rows)
            assert math.isclose(current['cellWidth'], screen['width'] / cols, abs_tol=0.01)
            assert math.isclose(current['cellHeight'], screen['height'] / rows, abs_tol=0.01)
            assert current['capacity'] == cols * rows
            assert math.isclose(current['aspect'], (screen['height'] / rows) / (screen['width'] / cols), abs_tol=0.01)
            assert page.locator('#font-recommendation-measure').inner_text() == message(page, 'settings.fonts.measure')
            page.wait_for_selector('#font-recommendation-preview .xterm')
            assert page.locator('.font-measurement-host').count() == 0
            assert runtime(page) == baseline and storage(page) == stored
            assert_no_terminal_mutations(page)
            # Display text must not participate in selecting or ordering candidates.
            page.locator('#font-recommendation-goal option[value="aspect"]').evaluate(
                'element => {element.textContent = "Display <b>& {capacity}";}')
            page.select_option('#font-recommendation-goal', 'aspect')
            cards = page.locator(CARDS + '[data-metrics]').evaluate_all(
                'items => items.map(item => JSON.parse(item.dataset.metrics))')
            assert abs(cards[0]['aspect'] - 2) <= min(abs(item['aspect'] - 2) for item in cards) + 1e-9
            candidate = page.locator(CARDS + '[data-font-index="1"]')
            selected_font = candidate.locator(':scope > div').first.inner_text()
            candidate.locator('[data-action="use"]').click()
            assert page.input_value('#pref-fontFace') == selected_font
            assert runtime(page) == baseline and storage(page) == stored
            assert page.locator('#font-recommendation-preview .xterm').count() == 0
            assert_no_terminal_mutations(page)
            page.click('#settings-close')
            open_appearance(page)
            assert page.input_value('#pref-fontFace') == baseline['fontFamily'].removeprefix('"StandTerm Powerline Symbols", ')
            measure(page)
            page.locator(CARDS + '[data-font-index="1"] [data-action="use"]').click()
            page.click('#settings-save')
            applied = runtime(page)
            assert applied['fontFamily'] == '"StandTerm Powerline Symbols", ' + selected_font
            assert applied['mirrorFontFamily'] == applied['fontFamily']
            assert json.loads(storage(page))['fontFace'] == selected_font
            assert page.locator('.font-measurement-host').count() == 0
            assert not errors, errors
        finally:
            fixture.close_context(context)


def test_missing_font_invalidation_and_cancellation(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        baseline = runtime(page)
        stored = storage(page)
        open_appearance(page)
        missing = '"StandTerm Missing <b>& {font}", monospace'
        page.fill('#pref-fontFace', missing)
        page.evaluate('() => { window.queryLocalFonts = () => { throw new Error("Unexpected permission request"); }; }')
        fixture.clear_emitted(page)
        measure(page)
        current = page.locator(CARDS + '[data-font-index="0"]')
        assert current.get_attribute('data-metrics'), 'Fallback geometry must remain measurable'
        assert current.locator(':scope > div').first.inner_text() == missing
        assert current.locator('b').count() == 0
        assert 'Font availability unverified.' in page.locator('#font-recommendation-status').inner_text()
        assert runtime(page) == baseline and storage(page) == stored
        assert_no_terminal_mutations(page)
        page.fill('#pref-fontSize', '20')
        assert page.locator(CARDS).count() == 0
        assert page.locator('#font-recommendation-preview .xterm').count() == 0
        measure(page)
        page.set_viewport_size({'width': 480, 'height': 600})
        page.wait_for_function("document.querySelectorAll('.font-recommendation-card').length === 0")
        measure(page)
        assert page.locator(CARDS + '[data-metrics]').count() > 0
        bounds = page.locator('#tab-appearance').evaluate(
            'element => ({client:element.clientWidth, scroll:element.scrollWidth})')
        assert bounds['scroll'] <= bounds['client'] + 1, bounds
        # Leave font loading unresolved, then close; no late result may reopen the preview.
        page.evaluate('() => {document.fonts.load = () => new Promise(() => {});}')
        page.click(MEASURE)
        assert page.locator(MEASURE).is_disabled()
        page.click('#settings-close')
        page.wait_for_function("document.querySelectorAll('.font-measurement-host').length === 0")
        open_appearance(page)
        assert page.locator(CARDS).count() == 0 and not page.locator(MEASURE).is_disabled()
        assert storage(page) == stored and runtime(page) == baseline
    finally:
        fixture.close_context(context)


def test_failed_measurements_keep_current_font(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        baseline = runtime(page)
        open_appearance(page)
        fixture.clear_emitted(page)
        measure(page)
        expected = json.loads(page.locator(CARDS + '[data-font-index="0"]').get_attribute('data-metrics'))
        # Reproduce deferred offscreen rendering; stale screen geometry must not get a score.
        style = page.add_style_tag(content='.font-measurement-host {left:-100000px !important;}')
        measure(page)
        for metrics in page.locator(CARDS + '[data-metrics]').evaluate_all(
                'items => items.map(item => JSON.parse(item.dataset.metrics))'):
            assert math.isclose(metrics['cellWidth'], expected['cellWidth'], abs_tol=0.01)
            assert math.isclose(metrics['cellHeight'], expected['cellHeight'], abs_tol=0.01)
        style.evaluate('element => element.remove()')
        page.evaluate('() => {document.fonts.load = () => Promise.reject(new Error("Fixture font load failed"));}')
        measure(page)
        assert page.locator(CARDS).count() >= 3
        assert page.locator(CARDS + ' button').count() == 0
        assert page.locator('#font-recommendation-status').inner_text() == message(page, 'settings.fonts.failed')
        assert page.locator('.font-measurement-host').count() == 0
        assert runtime(page) == baseline
        assert_no_terminal_mutations(page)
    finally:
        fixture.close_context(context)


def test_dom_renderer_and_fractional_dpr(browser, url):
    context = browser.new_context(viewport={'width': 1280, 'height': 800}, device_scale_factor=1.25)
    context.route('**/xterm-addon-webgl.js', lambda route: route.fulfill(
        content_type='application/javascript',
        body='window.WebglAddon = {WebglAddon: class {constructor() {throw new Error("Fixture WebGL unavailable");}}};'))
    page = context.new_page()
    try:
        page.goto(fixture.debug_url(url), wait_until='domcontentloaded')
        page.add_style_tag(content='#debug-hud, #payload-log {display:none !important;}')
        page.wait_for_selector('#connectBtn:not([disabled])')
        page.click('#connectBtn')
        page.wait_for_function('window.terminalTest.getActiveAgentState()?.connected === true')
        assert runtime(page)['renderer'] == 'dom'
        open_appearance(page)
        measure(page)
        assert page.locator(CARDS + '[data-metrics]').count() >= 3
        assert 'dom; DPR 1.25' in page.locator('#font-recommendation-status').inner_text()
        page.wait_for_selector('#font-recommendation-preview .xterm')
        page.click('#settings-close')
        assert page.locator('#font-recommendation-preview .xterm').count() == 0
    finally:
        fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for test in [test_live_geometry_and_explicit_save,
                             test_missing_font_invalidation_and_cancellation,
                             test_failed_measurements_keep_current_font,
                             test_dom_renderer_and_fractional_dpr]:
                    test(browser, url)
                    print(f'{test.__name__}: PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
