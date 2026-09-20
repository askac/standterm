"""Check the real toolbar DOM with mocked native IPC, without starting Core."""

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools' / '.ms-playwright'))

from playwright.sync_api import sync_playwright


def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for locale in ('en', 'zh-TW'):
                page = browser.new_page(viewport={'width': 640, 'height': 100})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.add_init_script("""
                    window.fixture = {calls: [], result: true, fail: false};
                    window.desktopToolbar = {
                        onState: callback => { window.fixture.receive = callback; },
                        invoke: action => {
                            window.fixture.calls.push(action);
                            return window.fixture.fail ? Promise.reject(new Error('Private fixture error'))
                                : Promise.resolve(window.fixture.result);
                        },
                    };
                """)
                page.goto((ROOT / 'desktop' / 'toolbar.html').as_uri())
                original_svg = page.locator('#capture-tools').evaluate('el => [...el.querySelectorAll("svg")].map(svg => svg.outerHTML)')
                page.evaluate("locale => fixture.receive({locale, mac:false, state:'idle'})", locale)
                assert page.locator('html').get_attribute('lang') == locale
                assert page.evaluate("""locale => {
                    const t = StandTermDesktopI18n.create(locale).t;
                    return [...document.querySelectorAll('[data-i18n]')].every(el =>
                        el.textContent === t(el.dataset.i18n)) && ['title', 'aria-label'].every(attribute =>
                        [...document.querySelectorAll(`[data-i18n-${attribute}]`)].every(el =>
                            el.getAttribute(attribute) === t(el.getAttribute(`data-i18n-${attribute}`))));
                }""", locale)
                assert page.locator('#capture-tools').evaluate('el => [...el.querySelectorAll("svg")].map(svg => svg.outerHTML)') == original_svg
                assert page.evaluate("""() => [...document.querySelectorAll('#menus button, #edit-tools button, #capture-tools button')]
                    .filter(el => !el.hidden).every(el => {
                        const box = el.getBoundingClientRect(); return box.width > 0 && box.x >= 0 && box.right <= innerWidth;
                    })""")
                for state, key in [('recording', 'record_pause'), ('paused', 'record_resume')]:
                    page.evaluate("state => fixture.receive({state, label:'Fixture 00:04'})", state)
                    expected = page.evaluate("({locale,key}) => StandTermDesktopI18n.create(locale).t('desktop.toolbar.' + key)", dict(locale=locale, key=key))
                    assert page.locator('#pause').get_attribute('title') == expected
                    assert page.locator('#pause').get_attribute('aria-label') == expected
                    assert page.locator('#recording-status').text_content() == 'Fixture 00:04'
                page.evaluate("fixture.receive({state:'idle'}); fixture.result = false")
                page.click('#save')
                expected = page.evaluate("locale => StandTermDesktopI18n.create(locale).t('desktop.toolbar.action_unavailable')", locale)
                page.wait_for_function('text => document.getElementById("notice").textContent === text', arg=expected)
                page.evaluate('fixture.fail = true')
                page.click('[data-menu="standterm"]')
                expected = page.evaluate("locale => StandTermDesktopI18n.create(locale).t('desktop.toolbar.action_unconfirmed')", locale)
                page.wait_for_function('text => document.getElementById("notice").textContent === text', arg=expected)
                assert page.evaluate('fixture.calls') == ['ready', 'screenshot-file', 'menu:standterm']
                page.evaluate("fixture.receive({mac:true, notice:'Literal <b>& {value}', noticeId:1})")
                assert not page.locator('#menus').is_visible()
                assert page.locator('#notice').text_content() == 'Literal <b>& {value}'
                assert page.locator('#notice b').count() == 0
                page.evaluate("fixture.receive({locale:'invalid', mac:false, state:'idle'})")
                assert page.locator('html').get_attribute('lang') == 'en'
                assert page.locator('[data-menu="edit"]').text_content() == 'Edit'
                assert not errors, errors
                page.close()
                print(json.dumps({'locale': locale, 'toolbar_dom': 'passed', 'native_ipc': 'mocked'}))
        finally:
            browser.close()


if __name__ == '__main__':
    run()
