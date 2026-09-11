"""IME anchor PoC tests using bundled xterm and synthetic Chromium composition.

These checks do not replace Windows/macOS IME candidate-window acceptance.
"""
import json
from pathlib import Path

from agent_browser_smoke import load_playwright

ROOT = Path(__file__).resolve().parents[1]


def fixture(browser, enabled=True, hidden=False, alternate=False):
    page = browser.new_page(viewport={'width': 1000, 'height': 650})
    page.set_content('<div id="terminal"></div>')
    page.add_style_tag(path=str(ROOT / 'static/css/xterm.css'))
    page.add_script_tag(path=str(ROOT / 'static/js/xterm.js'))
    page.add_script_tag(path=str(ROOT / 'static/js/standterm-ime-anchor-poc.js'))
    page.evaluate("""async ({enabled, hidden, alternate}) => {
        window.term = new Terminal({cols: 80, rows: 24, fontSize: 16, allowProposedApi: true});
        term.open(document.getElementById('terminal')); term.focus();
        window.originalPositioner = term._core._compositionHelper.updateCompositionElements;
        if (enabled) { window.poc = new StandTermImeAnchorPoc(); term.loadAddon(poc); }
        window.write = text => new Promise(resolve => term.write(text, resolve));
        window.inputs = []; term.onData(data => inputs.push(data));
        if (alternate) await write('\x1b[?1049h');
        await write('\x1b[?25h\x1b[3;2H' + (hidden ? '\x1b[?25l' : '') + '\x1b[18;5H');
        window.sample = () => {
            const view = document.querySelector('.composition-view');
            const area = term.textarea;
            const cell = term._core._renderService.dimensions.css.cell;
            return {x: term.buffer.active.cursorX, y: term.buffer.active.cursorY,
                composing: term._core._compositionHelper.isComposing,
                composition: [view.style.left, view.style.top],
                textarea: [area.style.left, area.style.top],
                cell: [cell.width, cell.height], inputs: [...inputs]};
        };
    }""", {'enabled': enabled, 'hidden': hidden, 'alternate': alternate})
    page.wait_for_timeout(80)
    return page, page.context.new_cdp_session(page)


def compose(page, cdp):
    cdp.send('Input.imeSetComposition', {'text': 'ㄓㄨ', 'selectionStart': 2, 'selectionEnd': 2})
    page.wait_for_timeout(50)
    state = page.evaluate('sample()')
    assert state['composing'], 'Synthetic composition did not start'
    return state


def redraw_case(browser, enabled, hidden=False, alternate=False):
    page, cdp = fixture(browser, enabled, hidden, alternate)
    try:
        initial = compose(page, cdp)
        positions = page.evaluate("""async () => {
            const samples = [];
            const observer = new MutationObserver(() => samples.push(sample()));
            observer.observe(term.textarea, {attributes: true, attributeFilter: ['style']});
            observer.observe(document.querySelector('.composition-view'), {attributes: true, attributeFilter: ['style']});
            for (let i = 0; i < 4; i++) {
                await write('\x1b[?25l\x1b[2;60Hspinner');
                await new Promise(resolve => setTimeout(resolve, 30));
                await write('\x1b[18;5H\x1b[?25h');
                await new Promise(resolve => setTimeout(resolve, 40));
            }
            observer.disconnect(); return samples;
        }""")
        moved = any(s['composition'] != initial['composition'] or s['textarea'] != initial['textarea'] for s in positions)
        assert moved != enabled, {'enabled': enabled, 'hidden': hidden, 'alternate': alternate, 'moved': moved}
        assert page.evaluate('inputs') == [], 'Redraw sent input during composition'
        print(json.dumps({'case': 'split-redraw', 'poc': enabled, 'initially_hidden': hidden,
                          'alternate_buffer': alternate, 'moved': moved}), flush=True)
    finally:
        page.close()


def test_commit_cancel_and_next_composition(browser):
    page, cdp = fixture(browser)
    try:
        compose(page, cdp)
        page.evaluate("write('\\x1b[2;60Hspinner')")
        page.wait_for_timeout(60)
        cdp.send('Input.insertText', {'text': '中'})
        page.wait_for_timeout(60)
        state = page.evaluate('sample()')
        assert not state['composing'] and state['inputs'] == ['中'], state
        assert float(state['textarea'][1][:-2]) == state['cell'][1], state
        page.evaluate("write('\\x1b[8;10H')")
        next_state = compose(page, cdp)
        assert float(next_state['textarea'][1][:-2]) == 7 * next_state['cell'][1], next_state
        cdp.send('Input.imeSetComposition', {'text': '', 'selectionStart': 0, 'selectionEnd': 0})
        page.wait_for_timeout(60)
        assert page.evaluate('inputs') == ['中'], 'Cancel sent composition text'
    finally:
        page.close()


def test_scroll_font_resize_and_buffer_fallback(browser):
    page, cdp = fixture(browser)
    try:
        initial = compose(page, cdp)
        page.evaluate("write('\\x1b[24;1H\\r\\n')")
        page.wait_for_timeout(80)
        scrolled = page.evaluate('sample()')
        assert float(scrolled['textarea'][1][:-2]) == 16 * scrolled['cell'][1], scrolled
        page.evaluate('term.scrollLines(-1)')
        page.wait_for_timeout(80)
        assert page.evaluate('sample().textarea') == initial['textarea'], 'Viewport scroll lost the input line'
        page.evaluate('term.options.fontSize = 20')
        page.wait_for_timeout(120)
        enlarged = page.evaluate('sample()')
        assert float(enlarged['textarea'][1][:-2]) == 17 * enlarged['cell'][1], enlarged
        page.evaluate("term.scrollToBottom(); write('\\x1b[2;60Hspinner')")
        page.wait_for_timeout(80)
        page.evaluate('term.resize(70, 24)')
        page.wait_for_timeout(100)
        resized = page.evaluate('sample()')
        assert resized['composing'] and resized['inputs'] == [], resized
        assert float(resized['textarea'][1][:-2]) == resized['y'] * resized['cell'][1], resized
        page.evaluate("write('\\x1b[?1049h\\x1b[5;7H')")
        page.wait_for_timeout(100)
        switched = page.evaluate('sample()')
        assert float(switched['textarea'][1][:-2]) == 4 * switched['cell'][1], switched
        assert switched['inputs'] == [], switched
    finally:
        page.close()


def test_noncomposing_and_disposal(browser):
    page, cdp = fixture(browser)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.evaluate("write('\\x1b[6;8H')")
        page.wait_for_timeout(60)
        idle = page.evaluate('sample()')
        assert float(idle['textarea'][1][:-2]) == 5 * idle['cell'][1], idle
        compose(page, cdp)
        page.evaluate('poc.dispose()')
        assert page.evaluate('term._core._compositionHelper.updateCompositionElements === originalPositioner')
        assert page.evaluate('term.markers.length') == 0, 'Anchor marker leaked'
        page.evaluate("write('\\x1b[2;60Hspinner')")
        page.wait_for_timeout(60)
        state = page.evaluate('sample()')
        assert float(state['textarea'][1][:-2]) == state['cell'][1], state
        assert state['inputs'] == [], state
        page.evaluate('term.dispose()')
        page.wait_for_timeout(60)
        assert not errors, errors
    finally:
        page.close()


def test_active_anchor_fallbacks(browser):
    for trigger in ["write('\\x1b[?1049h')", 'term.markers[0].dispose()',
                    "write('\\x1b[24;1H' + '\\r\\n'.repeat(24))"]:
        page, cdp = fixture(browser)
        try:
            compose(page, cdp)
            assert page.evaluate('term.markers.length') == 1
            page.evaluate(trigger)
            page.wait_for_timeout(80)
            page.evaluate("write('\\x1b[5;7H')")
            page.wait_for_timeout(80)
            state = page.evaluate('sample()')
            assert state['composing'] and state['inputs'] == [], state
            assert float(state['textarea'][1][:-2]) == 4 * state['cell'][1], state
            assert page.evaluate('term.markers.length') == 0, 'Released anchor marker leaked'
            # A fallback lasts until this composition ends; the next gets a fresh anchor.
            cdp.send('Input.imeSetComposition', {'text': '', 'selectionStart': 0, 'selectionEnd': 0})
            page.wait_for_timeout(60)
            fresh = compose(page, cdp)
            page.evaluate("write('\\x1b[2;60Hspinner')")
            page.wait_for_timeout(80)
            assert page.evaluate('sample().textarea') == fresh['textarea'], trigger
            assert page.evaluate('inputs') == [], trigger
        finally:
            page.close()


def test_terminal_disposal_with_pending_update(browser):
    page, cdp = fixture(browser)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        compose(page, cdp)
        page.evaluate("""() => {
            window.savedHelper = term._core._compositionHelper;
            window.savedElement = term.element;
            window.savedMarker = term.markers[0];
            savedHelper.updateCompositionElements();
            term.dispose();
        }""")
        page.wait_for_timeout(100)
        assert page.evaluate('savedHelper.updateCompositionElements === originalPositioner')
        assert page.evaluate('savedMarker.isDisposed')
        assert page.evaluate('savedElement.dataset.imeAnchorPoc === undefined')
        assert page.evaluate('inputs') == [], 'Disposal submitted composition text'
        assert not errors, errors
    finally:
        page.close()


def test_unsupported_api_keeps_native_positioner(browser):
    page, _ = fixture(browser, enabled=False)
    try:
        page.evaluate("""() => {
            term.options.allowProposedApi = false;
            term.loadAddon(new StandTermImeAnchorPoc());
        }""")
        assert page.evaluate('term._core._compositionHelper.updateCompositionElements === originalPositioner')
        assert page.evaluate('term.element.dataset.imeAnchorPoc === undefined')
        page.evaluate('term.dispose()')
    finally:
        page.close()


def main():
    with load_playwright()[0]() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for enabled, hidden, alternate in [(False, False, False), (True, False, False),
                                                (True, True, False), (True, True, True)]:
                redraw_case(browser, enabled, hidden, alternate)
            for test in [test_commit_cancel_and_next_composition, test_scroll_font_resize_and_buffer_fallback,
                         test_noncomposing_and_disposal, test_active_anchor_fallbacks,
                         test_terminal_disposal_with_pending_update, test_unsupported_api_keeps_native_positioner]:
                test(browser)
                print(test.__name__ + ': ok', flush=True)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
