"""Exercise opt-in previews using the actual helper and shipped Core page."""

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse

import agent_browser_smoke as fixture
from terminal_images_browser_smoke import png


def set_preference(page, enabled, size='medium'):
    page.click('#quick-settings')
    page.locator('#pref-allowAgentImagePreview').set_checked(enabled)
    page.select_option('#pref-agentImagePreviewSize', size)
    page.click('#settings-save')


def run_case(browser, url, *, desktop=False, language='en'):
    context, page = fixture.new_page(browser, url)
    other_context, other = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    origin = urllib.parse.urlsplit(url)
    base_url = urllib.parse.urlunsplit((origin.scheme, origin.netloc, '', '', ''))
    endpoint = base_url + '/preview/image'
    payload = {'image_base64': base64.b64encode(png()).decode(), 'title': 'First preview'}
    try:
        if language != 'en':
            page.evaluate("language => { const p = JSON.parse(localStorage.getItem('terminal.pref.v1') || '{}'); p.uiLanguage = language; localStorage.setItem('terminal.pref.v1', JSON.stringify(p)); }", language)
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function('window.terminalTest?.getSocketState().connected')
        assert page.request.post(endpoint, data=payload).status == 409
        set_preference(page, True)
        deadline = time.monotonic() + 5
        while page.request.post(endpoint, data=payload).status == 409:
            assert time.monotonic() < deadline
            page.wait_for_timeout(30)
        page.wait_for_selector('#image-preview-panel:not([hidden])')
        assert other.locator('#image-preview-panel').is_hidden()
        assert page.evaluate('window.terminalTest.getActiveAgentState().mode') == 'disabled'
        for size, width in [('small', 240), ('large', 640), ('medium', 400)]:
            set_preference(page, True, size)
            assert page.locator('#image-preview-panel').bounding_box()['width'] == width
        jpeg = page.evaluate("""() => {
            const image = document.getElementById('image-preview-image');
            const canvas = document.createElement('canvas');
            canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
            canvas.getContext('2d').drawImage(image, 0, 0);
            return canvas.toDataURL('image/jpeg').split(',')[1];
        }""")
        previous = page.locator('#image-preview-image').get_attribute('src')
        assert page.request.post(endpoint, data={'image_base64': jpeg, 'title': 'JPEG preview'}).status == 200
        page.wait_for_function('previous => document.getElementById("image-preview-image").src !== previous', arg=previous)
        assert page.evaluate('document.getElementById("image-preview-image").naturalWidth') == 64
        page.set_viewport_size({'width': 360, 'height': 640})
        set_preference(page, True, 'large')
        assert page.locator('#image-preview-panel').bounding_box()['width'] <= 336
        page.set_viewport_size({'width': 1280, 'height': 800})

        page.locator('.terminal-pane.active .xterm-helper-textarea').focus()
        before = page.evaluate('({focus: document.activeElement.className, tabs: window.terminalTest.getTerminalTabsState()})')
        title = '<img src=x onerror=alert(1)>'
        with tempfile.TemporaryDirectory(prefix='standterm-preview-helper-') as directory:
            image = Path(directory) / 'preview.png'
            image.write_bytes(png())
            info = Path(directory) / 'standterm/current_agentinfo.json'
            info.parent.mkdir()
            info.write_text(json.dumps({'base_url': base_url}))
            for target in [['--url', base_url], ['--agentinfo', str(info)], ['--agentinfo', base_url + '/agentinfo'], []]:
                environment = dict(os.environ, XDG_RUNTIME_DIR=directory,
                                   HTTP_PROXY='http://127.0.0.1:1', NO_PROXY='')
                result = subprocess.run([str(fixture.PYTHON), str(fixture.ROOT / 'scripts/image_preview.py'),
                                         str(image), *target, '--title', title],
                                        capture_output=True, text=True, timeout=20, env=environment)
                assert result.returncode == 0, result.stderr + result.stdout
                assert json.loads(result.stdout)['status'] == 'sent'
        page.wait_for_function("title => document.getElementById('image-preview-title').textContent === title", arg=title)
        assert page.locator('#image-preview-title img').count() == 0
        assert page.evaluate('document.activeElement.className') == before['focus']
        assert page.evaluate('window.terminalTest.getTerminalTabsState()') == before['tabs']
        pixel = page.evaluate("""() => {
            const image = document.getElementById('image-preview-image');
            const canvas = document.createElement('canvas');
            canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
            const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
            return Array.from(ctx.getImageData(10, 10, 1, 1).data);
        }""")
        assert pixel == [255, 0, 0, 255], pixel
        page.screenshot(path=f'/tmp/standterm-image-preview-{language}-{"desktop" if desktop else "core"}.png')
        first_url = page.locator('#image-preview-image').get_attribute('src')
        assert first_url.startswith('blob:')
        page.click('#image-preview-close')
        assert page.locator('#image-preview-panel').is_hidden()
        assert page.locator('#image-preview-image').get_attribute('src') is None
        assert await_blob_released(page, first_url)
        assert page.request.post(endpoint, data=payload).status == 200
        page.wait_for_selector('#image-preview-panel:not([hidden])')

        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.terminalTest?.getSocketState().connected')
        deadline = time.monotonic() + 5
        while page.request.post(endpoint, data=payload).status == 409:
            assert time.monotonic() < deadline
            page.wait_for_timeout(30)
        page.wait_for_selector('#image-preview-panel:not([hidden])')
        assert page.locator('#image-preview-panel').get_attribute('data-size') == 'large'
        set_preference(page, False)
        assert page.locator('#image-preview-panel').is_hidden()
        deadline = time.monotonic() + 5
        while page.request.post(endpoint, data=payload).status != 409:
            assert time.monotonic() < deadline
            page.wait_for_timeout(30)
        assert not errors, errors
        print(f'Preview helper, opt-in, isolation, sizes, focus, close and reload ({language}, desktop={desktop}): PASS', flush=True)
    finally:
        fixture.close_context(context)
        fixture.close_context(other_context)


def await_blob_released(page, url):
    return page.evaluate('async url => { try { await fetch(url); return false; } catch (_) { return true; } }', url)


def main():
    for desktop in [False, True]:
        server, url = fixture.start_server(desktop=desktop)
        try:
            with fixture.load_playwright()[0]() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for language in (['en', 'zh-TW'] if not desktop else ['en']):
                        run_case(browser, url, desktop=desktop, language=language)
                finally:
                    browser.close()
        finally:
            fixture.stop_server(server)


if __name__ == '__main__':
    main()
