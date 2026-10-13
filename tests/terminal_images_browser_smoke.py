"""Exercise experimental shell images through the shipped browser terminal."""

import base64
from pathlib import Path
import shlex
import struct
import tempfile
import zlib

import agent_browser_smoke as fixture


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 32, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff\x00\x00' * 64) * 32))
            + chunk(b'IEND', b''))


def iip(data=None, width='8', height='4', size=None):
    data = png() if data is None else data
    size = len(data) if size is None else size
    return f'\x1b]1337;File=size={size};width={width};height={height};preserveAspectRatio=0;inline=1:' + base64.b64encode(data).decode() + '\x07'


SIXEL = '\x1bPq"1;1;48;24#1;2;100;0;0#1!48~-!48~-!48~-!48~\x1b\\'


def images(page):
    return page.evaluate('window.terminalTest.getTerminalImagesForTest()')


def write(page, output):
    page.evaluate('text => window.terminalTest.writeTerminalOutput(text)', output)
    drain(page)


def drain(page):
    page.evaluate("""() => Promise.race([
        window.terminalTest.captureTerminalIoForTest(),
        new Promise((resolve, reject) => setTimeout(() => reject(new Error('Terminal output stalled')), 5000))
    ])""")


def assert_text(page, text):
    for mirror in [False, True]:
        assert page.evaluate("""args => Array.from({length: 8}, (_, row) =>
            window.terminalTest.getActiveTerminalBufferCellsForTest(row, args.mirror)
                .map(cell => cell.chars).join('')).join('\\n').includes(args.text)
        """, {'mirror': mirror, 'text': text}), (mirror, text)


def set_images(page, enabled):
    page.click('#quick-settings')
    page.locator('#pref-enableShellImages').set_checked(enabled)
    page.click('#settings-save')
    drain(page)


def wait_connected(page):
    page.wait_for_function('window.terminalTest?.getActiveAgentState()?.connected === true')
    drain(page)


def red_extent(page, result):
    assert result['status'] == 'ok', result
    return page.evaluate("""async result => {
        const image = new Image();
        const loaded = new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; });
        image.src = 'data:image/png;base64,' + result.image_base64;
        await loaded;
        const canvas = document.createElement('canvas');
        canvas.width = image.width;
        canvas.height = image.height;
        const context = canvas.getContext('2d');
        context.drawImage(image, 0, 0);
        const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
        const points = [];
        for (let i = 0; i < data.length; i += 4) {
            if (data[i] > 240 && data[i + 1] < 10 && data[i + 2] < 10 && data[i + 3] > 240)
                points.push([i / 4 % canvas.width, Math.floor(i / 4 / canvas.width)]);
        }
        if (!points.length) return null;
        return { width: Math.max(...points.map(p => p[0])) - Math.min(...points.map(p => p[0])) + 1,
            height: Math.max(...points.map(p => p[1])) - Math.min(...points.map(p => p[1])) + 1 };
    }""", result)


def assert_red(page):
    page.wait_for_function("""() => {
        const canvas = document.querySelector('.terminal-pane.active .xterm-image-layer');
        if (!canvas) return false;
        const data = canvas.getContext('2d').getImageData(2, 2, 1, 1).data;
        return data[0] > 240 && data[1] < 10 && data[2] < 10 && data[3] > 240;
    }""")
    active_id = page.evaluate('window.terminalTest.getTerminalTabsState().activeTerminalId')
    state = next(state for state in images(page) if state['id'] == active_id)
    assert state['usage'] > 0 and state['mirrorUsage'] > 0, state
    assert state['cursor'] == state['mirrorCursor'], state


def test_shell_images_and_preferences(browser, url):
    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.wait_for_function('window.terminalTest.getTerminalImagesForTest()[0].enabled')
        with tempfile.TemporaryDirectory(prefix='standterm-image-smoke-') as directory:
            path = Path(directory) / 'image.txt'
            path.write_text('\x1bc' + iip())
            page.evaluate("text => window.terminalTest.emitSocket('ssh_input', {terminal_id:'main',data:text})",
                          'cat ' + shlex.quote(str(path)) + '\r')
            assert_red(page)
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        write(page, '\x1bc' + SIXEL)
        assert_red(page)

        page.click('#quick-settings')
        assert page.locator('#pref-enableShellImages').is_checked()
        assert page.locator('#pref-remoteImages').is_disabled()
        assert not page.locator('#pref-remoteImages').is_checked()
        page.uncheck('#pref-enableShellImages')
        page.click('#settings-close')
        assert images(page)[0]['enabled'], 'Closing settings applied an unsaved preference'
        page.click('#quick-settings')
        assert page.locator('#pref-enableShellImages').is_checked()
        page.uncheck('#pref-enableShellImages')
        page.click('#settings-save')
        page.wait_for_function('!window.terminalTest.getTerminalImagesForTest()[0].enabled')
        assert page.locator('.xterm-image-layer').count() == 0
        write(page, '\x1bc' + iip() + SIXEL + 'AFTER_DISABLED')
        assert images(page)[0]['usage'] == 0 and images(page)[0]['mirrorUsage'] == 0
        assert page.evaluate("JSON.parse(localStorage.getItem('terminal.pref.v1')).enableShellImages") is False
        page.click('#quick-settings')
        page.check('#pref-enableShellImages')
        page.click('#settings-save')
        page.wait_for_function('window.terminalTest.getTerminalImagesForTest()[0].enabled')
        write(page, '\x1bc' + iip())
        assert_red(page)

        for connection_type in ['ssh', 'telnet', 'uart']:
            page.evaluate("""connection_type => window.terminalTest.handleSshOutput({
                terminal_id:'main', message_type:'ssh_connected', connection_type,
                terminal_label:'Local Shell'
            })""", connection_type)
            page.wait_for_function('!window.terminalTest.getTerminalImagesForTest()[0].enabled')
            write(page, '\x1bc' + iip() + SIXEL)
            assert images(page)[0]['usage'] == 0 and images(page)[0]['mirrorUsage'] == 0
        assert not errors, errors
    finally:
        fixture.close_context(context)


def test_invalid_and_chunked_images(browser, url):
    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        drain(page)
        oversized = bytearray(png()[:33])
        oversized[16:24] = struct.pack('>II', 10000, 10000)
        cases = [iip(png()[:33]), iip(b'not an image'), iip(bytes(oversized)),
                 iip(size=8 * 1024 * 1024 + 1), iip().replace(':iVBOR', ':@@@@@')]
        for sequence in cases:
            write(page, '\x1bc' + sequence + 'AFTER_INVALID')
            assert_text(page, 'AFTER_INVALID')
            assert images(page)[0]['usage'] == 0 and images(page)[0]['mirrorUsage'] == 0
        for sequence in [iip(), SIXEL]:
            write(page, '\x1bc')
            for offset in range(0, len(sequence), 7):
                page.evaluate('text => window.terminalTest.writeTerminalOutput(text)', sequence[offset:offset + 7])
            drain(page)
            assert_red(page)
        write(page, '\r\nAFTER_VALID')
        assert_text(page, 'AFTER_VALID')
        assert not errors, errors
    finally:
        fixture.close_context(context)


def test_agent_images_preserve_partial_cell_edges(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        fixture.attach_agent(page)
        write(page, '\x1bc' + iip(width='19px', height='9px'))
        assert_red(page)
        foreground = page.evaluate('window.terminalTest.buildViewportRenderResult()')
        assert foreground['source'] == 'visible_xterm_dom', foreground
        front_extent = red_extent(page, foreground)
        assert front_extent and abs(front_extent['width'] - 19) <= 1 and abs(front_extent['height'] - 9) <= 1, front_extent
        page.click('#new-tab-btn')
        # New output for an inactive tab must decode without a visible renderer.
        page.evaluate("text => window.terminalTest.handleSshOutput({terminal_id: 'main', message_type: 'terminal', data: text})", '\x1bc')
        page.wait_for_function('window.terminalTest.getTerminalImagesForTest()[0].mirrorUsage === 0')
        page.evaluate("text => window.terminalTest.handleSshOutput({terminal_id: 'main', message_type: 'terminal', data: text})",
                      iip(width='19px', height='9px'))
        page.wait_for_function('window.terminalTest.getTerminalImagesForTest()[0].mirrorUsage > 0')
        background = page.evaluate("window.terminalTest.buildViewportRenderResultForTerminal('main')")
        assert background['source'] == 'terminal_mirror_canvas', background
        back_extent = red_extent(page, background)
        assert back_extent, background
        for dimension, count in [('width', 'cols'), ('height', 'rows')]:
            scale = ((background['pixel_' + dimension] / background[count])
                     / (foreground['pixel_' + dimension] / foreground[count]))
            assert abs(back_extent[dimension] - front_extent[dimension] * scale) <= 2, (front_extent, back_extent, scale)
    finally:
        fixture.close_context(context)


def test_image_preferences_reload_import_reset(browser, url):
    for locale in ['en', 'zh-TW']:
        context, page = fixture.new_page(browser, url)
        try:
            page.click('#quick-settings')
            page.select_option('#pref-uiLanguage', locale)
            page.click('#settings-save')
            if locale != 'en':
                page.reload(wait_until='domcontentloaded')
                wait_connected(page)
            set_images(page, False)
            envelope = page.evaluate('window.terminalTest.createBrowserSettingsEnvelopeForTest()')
            imported = page.evaluate('value => window.terminalTest.parseBrowserSettingsEnvelopeForTest(value)', envelope)
            assert imported['preferences']['enableShellImages'] is False
            assert 'remoteImages' not in imported['preferences']
            page.reload(wait_until='domcontentloaded')
            wait_connected(page)
            assert not images(page)[0]['enabled']
            set_images(page, True)
            page.once('dialog', lambda dialog: dialog.accept())
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.evaluate('value => window.terminalTest.importBrowserSettingsEnvelopeForTest(value)', envelope)
            wait_connected(page)
            assert not images(page)[0]['enabled']
            page.click('#quick-settings')
            assert not page.locator('#pref-enableShellImages').is_checked()
            assert page.locator('#pref-remoteImages').is_disabled()
            assert not page.locator('#pref-remoteImages').is_checked()
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.click('#settings-reset')
            wait_connected(page)
            assert images(page)[0]['enabled']
        finally:
            fixture.close_context(context)


def test_missing_image_addon_keeps_terminal_working(browser, url):
    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.route('**/static/js/xterm-addon-image.js', lambda route: route.abort())
        page.reload(wait_until='domcontentloaded')
        wait_connected(page)
        assert not images(page)[0]['enabled']
        write(page, '\x1bcTEXT_WITHOUT_IMAGE_ADDON')
        assert_text(page, 'TEXT_WITHOUT_IMAGE_ADDON')
        assert not errors, errors
    finally:
        fixture.close_context(context)


def test_legacy_image_decoder_releases_urls(browser, url):
    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        drain(page)
        page.evaluate("""() => {
            window.createImageBitmap = undefined;
            window.imageUrls = new Set();
            const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
            URL.createObjectURL = blob => { const url = create(blob); imageUrls.add(url); return url; };
            URL.revokeObjectURL = url => { imageUrls.delete(url); revoke(url); };
        }""")
        write(page, '\x1bc' + iip())
        assert_red(page)
        assert page.evaluate('imageUrls.size') == 0
        write(page, '\x1bc' + iip(png()[:33]) + 'AFTER_LEGACY_ERROR')
        assert_text(page, 'AFTER_LEGACY_ERROR')
        assert page.evaluate('imageUrls.size') == 0
        assert not errors, errors
    finally:
        fixture.close_context(context)


def test_toggle_and_close_during_image_decode(browser, url):
    import terminal_layout_browser_smoke as layout

    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        drain(page)
        page.evaluate("""() => {
            const decode = window.createImageBitmap.bind(window);
            window.pendingImages = [];
            window.createImageBitmap = (...args) => new Promise(resolve => {
                pendingImages.push(() => decode(...args).then(image => { resolve(image); }));
            });
        }""")
        page.evaluate('text => window.terminalTest.writeTerminalOutput(text)', '\x1bc' + iip())
        page.wait_for_function('pendingImages.length === 2')
        page.click('#quick-settings')
        page.uncheck('#pref-enableShellImages')
        page.click('#settings-save')
        page.evaluate('Promise.all(pendingImages.splice(0).map(release => release()))')
        drain(page)
        assert not images(page)[0]['enabled']
        assert page.locator('.xterm-image-layer').count() == 0
        set_images(page, True)

        page.evaluate("""() => {
            const Addon = ImageAddon.ImageAddon;
            window.closedImageAddons = [];
            ImageAddon.ImageAddon = class extends Addon {
                constructor(options) { super(options); closedImageAddons.push(this); }
            };
            const close = ImageBitmap.prototype.close;
            window.closedBitmaps = 0;
            ImageBitmap.prototype.close = function () { closedBitmaps++; return close.call(this); };
        }""")
        page.evaluate('window.terminalTest.resumeTerminalIoForTest()')
        second = layout.connect_tab(page)
        drain(page)
        page.evaluate('text => window.terminalTest.writeTerminalOutput(text)', '\x1bc' + iip())
        page.wait_for_function('pendingImages.length === 2')
        page.locator(f'.terminal-tab[data-terminal-id="{second}"] .tab-close').click()
        assert len(images(page)) == 1
        page.evaluate('Promise.all(pendingImages.splice(0).map(release => release()))')
        page.wait_for_function('closedBitmaps === 2')
        assert page.evaluate('closedImageAddons.length === 2 && closedImageAddons.every(addon => addon.storageUsage === 0)')
        write(page, '\x1bcAFTER_CLOSED_IMAGE_TAB')
        assert_text(page, 'AFTER_CLOSED_IMAGE_TAB')
        assert not errors, errors
    finally:
        fixture.close_context(context)


def test_images_across_tabs_layouts_and_reconnect(browser, url):
    import terminal_layout_browser_smoke as layout

    context, page = fixture.new_page(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        second = layout.connect_tab(page)
        drain(page)
        assert all(state['enabled'] for state in images(page))
        for terminal_id in ['main', second]:
            page.evaluate('id => window.terminalTest.switchTerminalForTest(id)', terminal_id)
            write(page, '\x1bc' + iip())
            assert_red(page)
        for action in ['layoutVertical', 'layoutHorizontal', 'layoutFull']:
            layout.layout(page, action)
            page.set_viewport_size({'width': 960, 'height': 680})
            drain(page)
            assert_red(page)
            layout.assert_mirrors(page)
        write(page, '\x1b[?1049h\x1b[H' + SIXEL)
        assert_red(page)
        write(page, '\x1b[?1049l')
        assert_red(page)
        write(page, '\r\n' * 100)
        page.evaluate('window.terminalTest.scrollVisibleTerminal(-100)')
        assert_red(page)
        set_images(page, False)
        assert all(not state['enabled'] for state in images(page))
        page.evaluate('window.terminalTest.resumeTerminalIoForTest()')
        third = layout.connect_tab(page)
        drain(page)
        assert all(not state['enabled'] for state in images(page))
        set_images(page, True)
        assert all(state['enabled'] for state in images(page))
        write(page, '\x1bc' + iip())
        assert_red(page)
        page.evaluate("id => window.terminalTest.handleSshOutput({terminal_id: id, message_type: 'ssh_closed'})", third)
        drain(page)
        assert not next(state for state in images(page) if state['id'] == third)['enabled']
        page.evaluate("id => window.terminalTest.handleSshOutput({terminal_id: id, message_type: 'ssh_connected', connection_type: 'local_shell'})", third)
        drain(page)
        write(page, '\x1bc' + SIXEL)
        assert_red(page)
        page.locator(f'.terminal-tab[data-terminal-id="{third}"] .tab-close').click()
        assert len(images(page)) == 2
        assert not errors, errors
    finally:
        fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for test in [test_shell_images_and_preferences, test_invalid_and_chunked_images,
                             test_agent_images_preserve_partial_cell_edges,
                             test_image_preferences_reload_import_reset, test_missing_image_addon_keeps_terminal_working,
                             test_legacy_image_decoder_releases_urls, test_toggle_and_close_during_image_decode]:
                    test(browser, url)
                    print(f'{test.__name__}: PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)
    proc, url = fixture.start_server(desktop=True)
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                test_images_across_tabs_layouts_and_reconnect(browser, url)
                print('test_images_across_tabs_layouts_and_reconnect: PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
