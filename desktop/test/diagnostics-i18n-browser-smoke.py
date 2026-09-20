"""Render the actual scriptless diagnostics HTML in both Desktop languages."""

import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'tools' / '.ms-playwright'))

from playwright.sync_api import sync_playwright


SNAPSHOTS = r"""
const { statusHtml } = require('./desktop/diagnostics-window.cjs');
const { create } = require('./desktop/i18n.js');
const raw = '/tmp/<img src="https://example.com/private" onerror="alert(1)">&{path}';
const events = [{ event: 'backend_ready', mode: 'wsl', code: 'ECONNREFUSED',
  literal: '</pre><script>window.injected = true</script>&' }];
const snapshots = ['en', 'zh-TW'].map(locale => {
  const i18n = create(locale);
  return { locale, raw, eventText: events.map(event => JSON.stringify(event)).join('\n'),
    title: i18n.t('desktop.diagnostics.title'),
    windowTitle: i18n.t('desktop.diagnostics.window_title'),
    label: i18n.t('desktop.diagnostics.profile_directory'),
    emptyText: i18n.t('desktop.diagnostics.no_events'),
    html: statusHtml([[i18n.t('desktop.diagnostics.profile_directory'), raw]], events, i18n),
    emptyHtml: statusHtml([], [], i18n) };
});
console.log(JSON.stringify(snapshots));
"""


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='node', help='Node 22.12+ or an Electron executable in Node mode')
    args = parser.parse_args()
    result = subprocess.run([args.node, '-e', SNAPSHOTS], cwd=ROOT,
                            env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'},
                            capture_output=True, text=True, check=True, timeout=30)
    snapshots = json.loads(result.stdout)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for snapshot in snapshots:
                page = browser.new_page(viewport={'width': 640, 'height': 720})
                requests = []
                page.on('request', lambda request: requests.append(request.url))
                page.goto('data:text/html,' + quote(snapshot['html'], safe=''))
                assert page.locator('html').get_attribute('lang') == snapshot['locale']
                assert page.title() == snapshot['windowTitle']
                assert page.locator('h1').text_content() == snapshot['title']
                assert page.locator('th').text_content() == snapshot['label']
                assert page.locator('td').text_content() == snapshot['raw']
                assert page.locator('pre').text_content() == snapshot['eventText']
                assert page.locator('script, img, iframe, a').count() == 0
                assert page.evaluate('typeof window.injected') == 'undefined'
                csp = page.locator('meta[http-equiv="Content-Security-Policy"]').get_attribute('content')
                assert "default-src 'none'" in csp
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.goto('data:text/html,' + quote(snapshot['emptyHtml'], safe=''))
                assert page.locator('pre').text_content() == snapshot['emptyText']
                assert not any(url.startswith(('http:', 'https:', 'file:')) for url in requests), requests
                page.close()
                print(json.dumps({'locale': snapshot['locale'], 'diagnostics_dom': 'passed', 'events': 'literal'}))
        finally:
            browser.close()


if __name__ == '__main__':
    run()
