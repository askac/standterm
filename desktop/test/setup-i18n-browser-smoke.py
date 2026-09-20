"""Render actual setup initialization, progress and cancellation scripts in Chromium."""

import argparse
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools' / '.ms-playwright'))

from playwright.sync_api import sync_playwright


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='node', help='Node 22.12+ or an Electron executable in Node mode')
    args = parser.parse_args()
    result = subprocess.run([args.node, str(ROOT / 'desktop/test/setup-i18n-fixture.cjs')], cwd=ROOT,
                            env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'},
                            capture_output=True, text=True, check=True, timeout=30)
    snapshots = json.loads(result.stdout)
    setup_url = (ROOT / 'desktop/setup.html').as_uri()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for snapshot in snapshots:
                page = browser.new_page(viewport={'width': 700, 'height': 500})
                requests = []
                errors = []
                page.on('request', lambda request: requests.append(request.url))
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(setup_url)
                page.evaluate(snapshot['init'])
                assert page.locator('html').get_attribute('lang') == snapshot['locale']
                assert page.title() == snapshot['title']
                assert page.locator('h1').text_content() == snapshot['heading']
                assert page.locator('progress').get_attribute('aria-label') == snapshot['aria']
                assert page.locator('progress').get_attribute('value') is None
                for element in ('requirements', 'scope', 'detail', 'closeHint'):
                    assert page.locator('#' + element).text_content() == snapshot[element]
                assert page.locator('#stage').text_content() == snapshot['starting']
                assert page.locator('#stage').get_attribute('role') == 'status'
                assert page.locator('#stage').get_attribute('aria-live') == 'polite'
                if snapshot['mode'] == 'wsl':
                    assert snapshot['raw'] in page.locator('#requirements').text_content()
                assert 'sudo' in page.locator('#scope').text_content()
                for frame in snapshot['progress']:
                    page.evaluate(frame['script'])
                    assert page.locator('#stage').text_content() == frame['expected']
                page.evaluate(snapshot['cancel'])
                assert page.locator('#stage').text_content() == snapshot['canceling']
                assert page.locator('script, img, iframe, a').count() == 0
                assert page.evaluate('typeof window.injected') == 'undefined'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.evaluate('document.documentElement.scrollHeight <= innerHeight')
                csp = page.locator('meta[http-equiv="Content-Security-Policy"]').get_attribute('content')
                assert "default-src 'none'" in csp
                assert requests == [setup_url], requests
                assert not errors, errors
                page.close()
                print(json.dumps({'locale': snapshot['locale'], 'mode': snapshot['mode'],
                                  'setup_dom': 'passed', 'native_processes': 'mocked'}))
        finally:
            browser.close()


if __name__ == '__main__':
    run()
