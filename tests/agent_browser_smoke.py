import base64
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
PLAYWRIGHT_BROWSERS_DIR = ROOT / 'tools' / '.ms-playwright'
TERMINAL_ID = 'main'
SETUP_HINT = (
    'Setup hint: run '
    f'PLAYWRIGHT_BROWSERS_PATH={PLAYWRIGHT_BROWSERS_DIR} '
    f'{PYTHON} -m pip install -r requirements-dev.txt && '
    f'PLAYWRIGHT_BROWSERS_PATH={PLAYWRIGHT_BROWSERS_DIR} '
    f'{PYTHON} -m playwright install chromium'
)


class SmokeFailure(AssertionError):
    pass


def fail(message):
    raise SmokeFailure(message)


def check(condition, message):
    if not condition:
        fail(message)


def load_playwright():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(PLAYWRIGHT_BROWSERS_DIR))
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(f'Python Playwright is not installed. {SETUP_HINT}') from exc
    return sync_playwright, PlaywrightError, PlaywrightTimeoutError


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def debug_url(access_url):
    separator = '&' if '?' in access_url else '?'
    return f'{access_url}{separator}debug=1'


def start_server():
    port = find_free_port()
    session_recovery_dir = tempfile.mkdtemp(prefix='standterm-session-recovery-browser-smoke-')
    env = os.environ.copy()
    env.update({
        'STANDTERM_HOST': '127.0.0.1',
        'STANDTERM_PORT': str(port),
        'STANDTERM_DISABLE_AUTO_HTTPS': '1',
        'STANDTERM_DISABLE_AGENTINFO_CURRENT': '1',
        'STANDTERM_ASYNC_MODE': 'threading',
        'STANDTERM_ACCESS_UI': 'off',
        'STANDTERM_OPERATOR_OBSERVATION_DIR': tempfile.mkdtemp(prefix='standterm-observation-smoke-'),
        'STANDTERM_SESSION_RECOVERY_STORE': str(Path(session_recovery_dir) / 'credentials.json'),
    })
    proc = subprocess.Popen(
        [str(PYTHON), 'app.py', '--force-connection', 'local-shell'],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines = []
    output_queue = queue.Queue()

    def read_output():
        if not proc.stdout:
            return
        for line in proc.stdout:
            lines.append(line.rstrip())
            output_queue.put(line)

    thread = threading.Thread(target=read_output, daemon=True)
    thread.start()

    access_url = None
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('StandTerm server exited early:\n' + '\n'.join(lines[-40:]))
        try:
            line = output_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        match = re.search(r'Access URL: (https?://\S+)', line)
        if match:
            access_url = match.group(1)
            break
    if not access_url:
        stop_server(proc)
        raise RuntimeError('Timed out waiting for StandTerm access URL:\n' + '\n'.join(lines[-40:]))

    deadline = time.time() + 10
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(access_url, timeout=1) as response:
                if response.status == 200:
                    return proc, access_url
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)

    stop_server(proc)
    raise RuntimeError(f'Timed out waiting for StandTerm HTTP readiness: {last_error}')


def stop_server(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def new_page(browser, access_url, ui_language=None):
    context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US')
    page = context.new_page()
    if ui_language is not None:
        preferences = json.dumps({'uiLanguage': ui_language})
        page.add_init_script(f"localStorage.setItem('terminal.pref.v1', JSON.stringify({preferences}));")
    page.goto(debug_url(access_url), wait_until='domcontentloaded')
    # Keep debug-only overlays from covering controls during normal UI tests.
    page.add_style_tag(content='#debug-hud, #payload-log { display: none !important; }')
    page.wait_for_function('() => !!window.terminalTest', timeout=10000)
    page.wait_for_function(
        "() => window.terminalTest.getSocketState().connected === true",
        timeout=10000,
    )
    page.wait_for_selector('#connectBtn:not([disabled])', timeout=10000)
    page.click('#connectBtn')
    page.wait_for_function(
        '() => window.terminalTest.getActiveAgentState()?.connected === true',
        timeout=10000,
    )
    return context, page


def test_access_required_page_accepts_token_login(browser, access_url, ui_language='en'):
    parsed = urllib.parse.urlparse(access_url)
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    base_url = urllib.parse.urlunparse(parsed._replace(query='', fragment=''))
    login_url = debug_url(base_url)
    context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US')
    page = context.new_page()
    page.add_init_script("localStorage.setItem('terminal.pref.v1', JSON.stringify(" + json.dumps({'uiLanguage': ui_language}) + "));")
    zh = ui_language == 'zh-TW'
    pending_login = []
    page.route('**/login', lambda route: pending_login.append(route))
    try:
        page.goto(login_url, wait_until='domcontentloaded')
        page.wait_for_selector('#access-token', timeout=5000)
        check(page.inner_text('h1') == ('需要 StandTerm 存取權限' if zh else 'StandTerm access required'),
              'initial gate title did not match its language')
        check(page.locator('html').get_attribute('lang') == ui_language, 'initial gate did not apply the saved language')
        check(page.get_by_label('存取權杖' if zh else 'Access token').count() == 1, 'initial token field lacks its accessible label')
        check(page.inner_text('button[type="submit"]') == ('使用存取權杖' if zh else 'Use access token'),
              'initial gate confused access-token login with browser authorization')
        check(page.inner_text('#access-recovery-button') == ('使用裝置驗證' if zh else 'Verify with device'),
              'initial device verification action did not match its language')
        instructions = page.inner_text('main > p:first-of-type')
        check('Access URL' in instructions and ('啟動器' if zh else 'launcher') in instructions,
              'initial access instructions did not identify the launcher Access URL')
        hint = page.inner_text('main')
        for phrase in (['主機名稱', '仍有效的工作階段', '啟用', '重新啟動', '目前的存取權杖'] if zh else
                       ['registered for this hostname', 'enabled for a still-valid session', 'After StandTerm restarts', 'current access token']):
            check(phrase in hint, f'initial recovery hint omitted eligibility or restart detail: {phrase}')
        pending_device = []
        page.route('**/session-recovery/authenticate/options', lambda route: pending_device.append(route))
        page.click('#access-recovery-button')
        device_pending_text = '等待裝置驗證…' if zh else 'Waiting for device verification…'
        page.wait_for_function('text => document.getElementById("access-login-status").textContent === text', arg=device_pending_text)
        check(page.locator('#access-recovery-button').is_disabled(), 'device verification allowed repeated submission while pending')
        check(len(pending_device) == 1, 'device verification did not issue one options request')
        pending_device.pop().fulfill(status=400, content_type='application/json',
                                     body=json.dumps({'status': 'error', 'message': 'Device fixture unavailable.'}))
        page.wait_for_function('() => !document.getElementById("access-recovery-button").disabled')
        check(page.inner_text('#access-recovery-button') == ('使用裝置驗證' if zh else 'Verify with device'),
              'failed device verification did not retain its localized reset label')
        check(page.inner_text('#access-login-status') == 'Device fixture unavailable.',
              'device error fallback did not preserve the server diagnostic')
        page.fill('#access-token', 'agt_not_a_launcher_access_token')
        page.click('button[type="submit"]')
        pending_text = '正在驗證存取權杖…' if zh else 'Checking access token…'
        page.wait_for_function('text => document.getElementById("access-login-status").textContent === text', arg=pending_text)
        check(len(pending_login) == 1, 'initial login did not issue one pending request')
        pending_login.pop().continue_()
        rejected_text = '存取權杖未通過驗證。' if zh else 'Access token was not accepted.'
        page.wait_for_function('text => document.getElementById("access-login-status").textContent === text', arg=rejected_text)
        check(page.locator('#connectBtn').count() == 0, 'an Agent token bypassed the initial access gate')
        page.fill('#access-token', token)
        page.click('button[type="submit"]')
        page.wait_for_function('text => document.getElementById("access-login-status").textContent === text', arg=pending_text)
        check(len(pending_login) == 1, 'initial token retry did not submit one request')
        pending_login.pop().continue_()
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        check('token=' not in page.url, 'token login left the access token in the URL')
        check(page.locator('#connectBtn').count() == 1, 'token login did not render the app controls')
        check(
            page.evaluate("() => window.terminalTest.hasRememberedAccessToken()") is True,
            'manually entered token was not remembered for recovery',
        )
    finally:
        close_context(context)


def test_initial_access_login_falls_back_without_localization_or_javascript(browser, access_url):
    parsed = urllib.parse.urlparse(access_url)
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    base_url = urllib.parse.urlunparse(parsed._replace(query='', fragment=''))
    localization_assets = re.compile(r'/static/js/standterm-(?:i18n|messages)\.js(?:\?.*)?$')
    for javascript_enabled in [False, True]:
        context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US',
                                      java_script_enabled=javascript_enabled)
        page = context.new_page()
        try:
            if javascript_enabled:
                page.add_init_script("localStorage.setItem('terminal.pref.v1', JSON.stringify({uiLanguage: 'zh-TW'}));")
                page.route(localization_assets, lambda route: route.abort())
            response = page.goto(debug_url(base_url), wait_until='domcontentloaded')
            check(response.status == 401, 'fallback fixture did not start without an authenticated session')
            check(page.inner_text('h1') == 'StandTerm access required', 'fallback did not retain the English initial gate')
            check(page.inner_text('button[type="submit"]') == 'Use access token', 'fallback lost the token submission label')
            check(page.locator('#access-login-form').get_attribute('method') == 'post', 'fallback changed the login method')
            check(page.locator('#access-login-form').get_attribute('action') == '/login', 'fallback changed the login endpoint')
            if javascript_enabled:
                page.unroute(localization_assets)
            page.fill('#access-token', token)
            page.click('button[type="submit"]')
            page.wait_for_selector('#connectBtn', state='attached', timeout=10000)
            check(context.request.get(base_url).status == 200, 'fallback login did not establish an authenticated session')
            check('token=' not in page.url, 'fallback login exposed its access token in the URL')
        finally:
            close_context(context)


def test_browser_authorization_gate_hides_connection_controls(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    context = browser.new_context(viewport={'width': 390, 'height': 844}, locale='en-US')
    page = context.new_page()
    page.add_init_script("localStorage.setItem('terminal.pref.v1', JSON.stringify(" + json.dumps({'uiLanguage': ui_language}) + "));")
    zh = ui_language == 'zh-TW'
    try:
        page.goto(debug_url(access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        page.wait_for_selector('#connectBtn:not([disabled])', timeout=10000)
        page.wait_for_timeout(500)
        page.evaluate(
            """() => {
                const policy = window.terminalTest.getTerminalPolicy();
                policy.browser_authorization = {
                    available: true,
                    authorized: false,
                    required_for: ['ssh', 'local_shell', 'uart']
                };
                policy.connection_options.forEach(option => { option.allowed = false; });
                window.terminalTest.applyTerminalPolicy(policy);
            }"""
        )
        page.wait_for_selector('#browser-auth-box', state='visible', timeout=5000)
        state = page.evaluate(
            """() => {
                const controls = document.getElementById('controls').getBoundingClientRect();
                const actions = document.getElementById('browser-auth-actions').getBoundingClientRect();
                return {
                    title: document.querySelector('#controls h2').innerText,
                    sessionId: document.getElementById('launcher-session-id').innerText,
                    launcherId: LAUNCHER_INSTANCE_ID,
                    warningPresent: !!document.getElementById('browser-auth-warning'),
                    authorizationTitle: document.getElementById('browser-auth-title').innerText,
                    message: document.getElementById('browser-auth-message').innerText,
                    connectionDisplay: getComputedStyle(document.getElementById('connection-form')).display,
                    sshVisible: document.getElementById('ssh-fields').getClientRects().length > 0,
                    actionsInsideControls: actions.left >= controls.left && actions.right <= controls.right
                };
            }"""
        )
        check(state['title'] == 'StandTerm', 'authorization gate does not show the StandTerm product name')
        check(isinstance(state['launcherId'], str) and bool(state['launcherId']), 'authorization gate launcher session ID is empty')
        check(state['sessionId'] == message(page, 'browser.chrome.session_id', {'id':state['launcherId']}),
              'authorization gate does not show the complete localized launcher session ID')
        check(state['warningPresent'] is False, 'authorization gate retained the decorative warning')
        check(state['authorizationTitle'] == ('需要瀏覽器授權' if zh else 'Browser authorization required'),
              'browser authorization title did not match its language')
        check(state['message'] == ('貼上瀏覽器授權網址以繼續。' if zh else 'Paste a browser authorization URL to continue.'),
              'authorization gate first-use hint is missing')
        check(page.inner_text('#browser-auth-url-submit') == ('授權瀏覽器' if zh else 'Authorize browser'),
              'browser authorization submit action did not name its purpose')
        check(state['connectionDisplay'] == 'none', 'authorization gate left connection controls visible')
        check(state['sshVisible'] is False, 'authorization gate left SSH fields visible')
        check(state['actionsInsideControls'] is True, 'authorization gate actions overflow the controls panel')

        original_url = page.url
        page.fill('#browser-auth-url-input', 'not a URL')
        page.click('#browser-auth-url-submit')
        check(page.locator('#browser-auth-url-input').evaluate('input => input.validity.typeMismatch'),
              'authorization URL input lost native URL validation')
        check(page.url == original_url and page.inner_text('#browser-auth-url-error') == '',
              'native validation navigated or invoked the custom URL parser')
        invalid_inputs = [
            ('', '請輸入完整的瀏覽器授權網址。' if zh else 'Enter a complete browser authorization URL.'),
            ('ftp://example.test/?authorize=fixture', '瀏覽器授權網址必須使用 HTTP 或 HTTPS。' if zh else 'Browser authorization URLs must use HTTP or HTTPS.'),
            (access_url, '此網址缺少一次性瀏覽器授權碼。' if zh else 'This URL has no one-time browser authorization code.'),
            ('https://example.test/?token=agt_not_browser_authorization', '此網址缺少一次性瀏覽器授權碼。' if zh else 'This URL has no one-time browser authorization code.'),
        ]
        for value, expected in invalid_inputs:
            page.fill('#browser-auth-url-input', value)
            check(page.inner_text('#browser-auth-url-error') == '', 'editing an authorization URL did not reset its validation error')
            page.click('#browser-auth-url-submit')
            check(page.inner_text('#browser-auth-url-error') == expected, 'authorization URL validation used the wrong message')
            check(page.url == original_url, 'malformed authorization input navigated away from the gate')
            check(page.evaluate('() => window.terminalTest.getTerminalPolicy().browser_authorization.authorized') is False,
                  'malformed authorization input changed the grant state')

        page.click('#browser-auth-help-btn')
        page.wait_for_selector('#browser-auth-help-modal.open', timeout=5000)
        check(
            ('自動檢查授權檔' if zh else 'checks for the file automatically') in page.locator('.browser-auth-help-body').inner_text(),
            'manual authorization help does not explain automatic checking',
        )
        page.click('#browser-auth-help-close')
        check(
            page.locator('#browser-auth-help-modal').get_attribute('aria-hidden') == 'true',
            'manual authorization help did not close',
        )
        parsed = urllib.parse.urlparse(access_url)
        authorization_url = urllib.parse.urlunparse(parsed._replace(query='authorize=browser-smoke-fixture', fragment=''))
        page.route(authorization_url, lambda route: route.fulfill(status=204))
        page.fill('#browser-auth-url-input', authorization_url)
        with page.expect_request(authorization_url):
            page.click('#browser-auth-url-submit', no_wait_after=True)
        check(page.inner_text('#browser-auth-message') == ('正在開啟授權網址…' if zh else 'Opening authorization URL…'),
              'syntactically valid authorization URL did not show the pending navigation state')
        check(page.evaluate('() => window.terminalTest.getTerminalPolicy().browser_authorization.authorized') is False,
              'opening an authorization URL claimed authorization before server confirmation')
    finally:
        close_context(context)


def close_context(context):
    try:
        context.close()
    except Exception:
        pass


def test_server_unavailable_waits_for_reconnect(browser, access_url, ui_language='en'):
    context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US')
    page = context.new_page()
    page.add_init_script("localStorage.setItem('terminal.pref.v1', JSON.stringify(" + json.dumps({'uiLanguage': ui_language}) + "));")
    zh = ui_language == 'zh-TW'
    try:
        page.goto(debug_url(access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        initial_state = page.evaluate("() => window.terminalTest.getSocketState()")
        check(initial_state['retriesContinuously'] is True, 'socket reconnect attempts are still bounded')

        context.set_offline(True)
        page.evaluate("() => window.terminalTest.closeSocketTransportForTest()")
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().serverConnectionState === 'unavailable'",
            timeout=5000,
        )
        unavailable = page.evaluate(
            """() => ({
                socketStatus: document.getElementById('socketStatus').innerText,
                message: document.getElementById('server-availability-message').innerText,
                messageDisplay: document.getElementById('server-availability-message').style.display,
                connectionFormDisplay: getComputedStyle(document.getElementById('connection-form')).display,
                connectDisabled: document.getElementById('connectBtn').disabled
            })"""
        )
        check(unavailable['socketStatus'] == ('無法連線至 StandTerm（正在重連）' if zh else 'Cannot reach StandTerm (reconnecting)'),
              'socket status did not identify server unavailability')
        check(('此頁面會持續嘗試重新連線' if zh else 'This page will keep trying to reconnect') in unavailable['message'],
              'server unavailable guidance did not explain automatic recovery')
        check(('目前啟動器提供的存取權杖' if zh else 'access token from its current launcher') in unavailable['message'],
              'reconnect guidance did not distinguish the current launcher token after a restart')
        check(unavailable['messageDisplay'] == 'block', 'server unavailable guidance was not visible')
        check(unavailable['connectionFormDisplay'] == 'none', 'connection picker remained visible while the server was unavailable')
        check(unavailable['connectDisabled'] is True, 'terminal connect button remained enabled while the server was unavailable')
        check(page.locator('#server-retry-now').is_visible(), 'Retry Now was not visible with the disconnect warning')
        check(page.inner_text('#server-retry-now') == ('立即重新連線' if zh else 'Reconnect now'),
              'reconnect action did not match its language')
        page.click('#server-retry-now')
        check(
            page.locator('#server-retry-now').inner_text() == ('正在重新連線…' if zh else 'Reconnecting…'),
            'Retry Now did not trigger an immediate reconnect attempt',
        )
        check(
            any(
                event['event'] == 'socket.retry_now'
                for event in page.evaluate('() => window.terminalTest.getConnectionDiagnostics()')
            ),
            'Retry Now did not record an explicit reconnect attempt',
        )
        page.wait_for_function('text => document.getElementById("server-retry-now").innerText === text',
                               arg='立即重新連線' if zh else 'Reconnect now')
        check(page.locator('#server-retry-now').is_enabled(), 'pending retry did not reset its control')

        context.set_offline(False)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        recovered = page.evaluate(
            """() => ({
                serverState: window.terminalTest.getSocketState().serverConnectionState,
                messageDisplay: document.getElementById('server-availability-message').style.display,
                connectionFormDisplay: getComputedStyle(document.getElementById('connection-form')).display,
                connectDisabled: document.getElementById('connectBtn').disabled
            })"""
        )
        check(recovered['serverState'] == 'available', 'server state did not recover after reconnect')
        check(recovered['messageDisplay'] == 'none', 'server unavailable guidance remained visible after reconnect')
        check(recovered['connectionFormDisplay'] == 'block', 'connection picker did not return after reconnect')
        check(recovered['connectDisabled'] is False, 'terminal connect button did not recover after reconnect')
        check(page.inner_text('#socketStatus') == ('已連線' if zh else 'Connected'), 'reconnected socket status did not match its language')
    finally:
        close_context(context)


def test_retry_now_resubscribes_after_socket_disconnect(browser, access_url):
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    try:
        page.goto(debug_url(access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )

        page.evaluate("() => window.terminalTest.disconnectSocketForTest()")
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().serverConnectionState === 'unavailable'",
            timeout=5000,
        )
        page.click('#server-retry-now')
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=5000,
        )
        check(
            page.evaluate("() => window.terminalTest.getSocketState().serverConnectionState") == 'available',
            'Retry Now did not restore the Socket.IO namespace subscription',
        )
    finally:
        close_context(context)


def test_invalid_session_reconnect_prompts_for_current_token(browser, access_url, ui_language='en'):
    parsed = urllib.parse.urlparse(access_url)
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US')
    page = context.new_page()
    page.add_init_script("localStorage.setItem('terminal.pref.v1', JSON.stringify(" + json.dumps({'uiLanguage': ui_language}) + "));")
    zh = ui_language == 'zh-TW'
    pending_login = []
    page.route('**/login', lambda route: pending_login.append(route))
    try:
        page.goto(debug_url(access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        context.clear_cookies()
        page.evaluate("() => window.terminalTest.disconnectSocketForTest()")
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().serverConnectionState === 'unavailable'",
            timeout=5000,
        )
        page.evaluate("() => window.terminalTest.connectSocketForTest()")
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().serverConnectionState === 'session_required'",
            timeout=10000,
        )
        page.wait_for_selector('#session-recovery-modal.open', timeout=5000)
        recovery = page.evaluate(
            """() => ({
                serverState: window.terminalTest.getSocketState().serverConnectionState,
                title: document.querySelector('#session-recovery-modal h3').innerText,
                detail: document.querySelector('#session-recovery-modal p').innerText,
                message: document.getElementById('session-recovery-message').innerText
            })"""
        )
        check(recovery['serverState'] == 'session_required', 'invalid session did not use the structured session-required state')
        check(recovery['title'] == ('恢復 StandTerm 存取' if zh else 'Restore StandTerm access'), 'session recovery title is incorrect')
        for phrase in (['主機名稱', '仍有效的工作階段', '啟用', '重新啟動', '目前的存取權杖'] if zh else
                       ['registered for this hostname', 'enabled for a still-valid session', 'After StandTerm restarts', 'current access token']):
            check(phrase in recovery['detail'], f'recovery hint omitted eligibility or restart detail: {phrase}')
        check(recovery['message'] == ('請輸入目前 StandTerm 啟動器提供的存取權杖。' if zh else 'Enter the access token from the current StandTerm launcher.'),
              'session recovery did not request the current launcher token')
        check(page.get_by_label('存取權杖' if zh else 'Access token', exact=True).count() == 1,
              'recovery token input lacks its accessible name')
        check(page.inner_text('#session-recovery-form button[type="submit"]') == ('使用存取權杖' if zh else 'Use access token'),
              'recovery submit action did not identify launcher access')
        check(page.inner_text('#session-recovery-platform') == ('使用裝置驗證' if zh else 'Verify with device'),
              'recovery device action did not match its language')
        check(page.inner_text('#session-recovery-remembered-token') == ('使用已儲存的存取權杖' if zh else 'Use saved access token'),
              'saved-token action did not match its language')

        page.click('#session-recovery-remembered-token')
        saved_pending_text = '正在驗證已儲存的存取權杖…' if zh else 'Checking saved access token…'
        page.wait_for_function('text => document.getElementById("session-recovery-message").textContent === text', arg=saved_pending_text)
        check(page.locator('#session-recovery-remembered-token').is_disabled(), 'saved-token request did not disable repeated submission')
        check(len(pending_login) == 1, 'saved-token recovery did not issue one request')
        pending_login.pop().fulfill(status=401, body='')
        rejected_text = '存取權杖未通過驗證。' if zh else 'Access token was not accepted.'
        page.wait_for_function('text => document.getElementById("session-recovery-message").textContent === text', arg=rejected_text)
        check(page.locator('#session-recovery-remembered-token').is_enabled(), 'rejected saved token did not reset its control')
        check(page.inner_text('#session-recovery-remembered-token') == ('使用已儲存的存取權杖' if zh else 'Use saved access token'),
              'saved-token control lost its localized reset label')

        page.fill('#session-recovery-token', 'agt_not_a_launcher_access_token')
        page.click('#session-recovery-form button[type="submit"]')
        pending_text = '正在驗證存取權杖…' if zh else 'Checking access token…'
        page.wait_for_function('text => document.getElementById("session-recovery-message").textContent === text', arg=pending_text)
        check(len(pending_login) == 1, 'recovery did not submit one pending token request')
        pending_login.pop().continue_()
        rejected_text = '存取權杖未通過驗證。' if zh else 'Access token was not accepted.'
        page.wait_for_function('text => document.getElementById("session-recovery-message").textContent === text', arg=rejected_text)
        check(page.locator('#session-recovery-modal.open').is_visible(), 'an Agent token bypassed session recovery')
        check(page.evaluate('() => window.terminalTest.getSocketState().serverConnectionState') == 'session_required',
              'rejected token changed the structured recovery state')

        page.fill('#session-recovery-token', token)
        page.click('#session-recovery-form button[type="submit"]')
        page.wait_for_function('text => document.getElementById("session-recovery-message").textContent === text', arg=pending_text)
        check(len(pending_login) == 1, 'recovery retry did not submit one pending token request')
        pending_login.pop().continue_()
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        check(
            page.evaluate("() => window.terminalTest.getSocketState().serverConnectionState") == 'available',
            'valid current token did not restore the server connection',
        )
        check(page.locator('#session-recovery-modal.open').count() == 0, 'successful recovery did not close its dialog')
        check(page.inner_text('#session-recovery-message') == '', 'successful recovery retained a pending or rejected message')
        check(page.input_value('#session-recovery-token') == '', 'successful recovery retained the entered access token')
    finally:
        close_context(context)


def test_browser_access_and_recovery_in_traditional_chinese(browser, access_url):
    test_access_required_page_accepts_token_login(browser, access_url, ui_language='zh-TW')
    test_browser_authorization_gate_hides_connection_controls(browser, access_url, ui_language='zh-TW')
    test_server_unavailable_waits_for_reconnect(browser, access_url, ui_language='zh-TW')
    test_invalid_session_reconnect_prompts_for_current_token(browser, access_url, ui_language='zh-TW')


def test_platform_passkey_recovers_live_session_without_access_token(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    parsed = urllib.parse.urlparse(access_url)
    localhost_access_url = urllib.parse.urlunparse(parsed._replace(
        netloc=f'localhost:{parsed.port}',
    ))
    base_url = urllib.parse.urlunparse(parsed._replace(
        netloc=f'localhost:{parsed.port}',
        query='',
        fragment='',
    ))
    context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US')
    page = context.new_page()
    page.add_init_script("localStorage.setItem('terminal.pref.v1', JSON.stringify(" + json.dumps({'uiLanguage':ui_language}) + "));")
    cdp = context.new_cdp_session(page)
    try:
        cdp.send('WebAuthn.enable')
        authenticator = cdp.send('WebAuthn.addVirtualAuthenticator', {
            'options': {
                'protocol': 'ctap2',
                'ctap2Version': 'ctap2_1',
                'transport': 'internal',
                'hasResidentKey': True,
                'hasUserVerification': True,
                'isUserVerified': True,
                'automaticPresenceSimulation': True,
            },
        })
        page.goto(debug_url(localhost_access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        page.wait_for_selector('#connectBtn:not([disabled])', timeout=10000)
        page.click('#connectBtn')
        page.wait_for_function(
            '() => window.terminalTest.getActiveAgentState()?.connected === true',
            timeout=10000,
        )

        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="server"]')
        page.wait_for_function(
            "expected => document.getElementById('platform-recovery-status').innerText === expected",
            arg=message(page, 'browser.platform.status', {'configured':0,'armed':0,'rp_id':'localhost'}),
            timeout=5000,
        )
        page.click('#platform-recovery-register')
        page.wait_for_function(
            "expected => document.getElementById('platform-recovery-status').innerText === expected",
            arg=message(page, 'browser.platform.status', {'configured':1,'armed':1,'rp_id':'localhost'}),
            timeout=10000,
        )
        registered_credentials = cdp.send('WebAuthn.getCredentials', {
            'authenticatorId': authenticator['authenticatorId'],
        }).get('credentials', [])
        check(len(registered_credentials) == 1, 'virtual platform authenticator did not retain the recovery credential')
        check(
            'localhost' in page.locator('#platform-recovery-status').inner_text(),
            'platform recovery did not bind the passkey to the localhost RP ID',
        )

        context.clear_cookies()
        page.goto(debug_url(base_url), wait_until='domcontentloaded')
        page.wait_for_selector('#access-recovery-button', timeout=5000)
        check(
            page.locator('#access-token').is_visible(),
            'access-required page did not retain the access-token fallback',
        )
        page.click('#access-recovery-button')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        page.wait_for_function(
            "() => window.terminalTest.getTerminalTabsState().tabs.length === 1",
            timeout=5000,
        )
        page.wait_for_function(
            '() => window.terminalTest.getActiveAgentState()?.connected === true',
            timeout=10000,
        )
        recovered = page.evaluate(
            """() => ({
                url: window.location.href,
                tabs: window.terminalTest.getTerminalTabsState().tabs,
                connected: window.terminalTest.getActiveAgentState()?.connected
            })"""
        )
        check('token=' not in recovered['url'], 'platform recovery exposed an access token in the URL')
        check(recovered['connected'] is True, 'platform recovery did not restore the live terminal bridge')
    finally:
        try:
            cdp.send('WebAuthn.disable')
        except Exception:
            pass
        close_context(context)


def js_arg_object(event_name, payload):
    return {'event_name': event_name, 'payload': payload}


def emit_socket(page, event_name, payload):
    page.evaluate(
        """args => window.terminalTest.emitSocket(args.event_name, args.payload)""",
        js_arg_object(event_name, payload),
    )


def set_privacy(page, privacy_state):
    page.evaluate('privacyState => window.terminalTest.setPrivacy(privacyState)', privacy_state)


def clear_emitted(page):
    page.evaluate('() => window.terminalTest.clearEmitted()')


def get_emitted(page, event_name=None):
    emitted = page.evaluate('() => window.terminalTest.getEmitted()')
    if event_name is None:
        return emitted
    return [entry for entry in emitted if entry.get('event') == event_name]


def active_agent_state(page):
    return page.evaluate('() => window.terminalTest.getActiveAgentState()')


def wait_for_agent(page, predicate, timeout=10000):
    page.wait_for_function(
        """source => {
            const state = window.terminalTest.getActiveAgentState();
            return !!state && Function('state', `return (${source});`)(state);
        }""",
        arg=predicate,
        timeout=timeout,
    )
    return active_agent_state(page)


def wait_for_last_action_error(page, error_code):
    return wait_for_agent(
        page,
        f"state.last_action && state.last_action.errorCode === '{error_code}'",
    )


def attach_agent(page):
    emit_socket(page, 'agent_attach', {'terminal_id': TERMINAL_ID})
    return wait_for_agent(page, "state.mode === 'observe'")


def test_toolbar_pause_targets_main_tab_not_panel_override(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language)
    try:
        attach_agent(page)
        set_agent_mode(page, 'direct', 'direct_active')
        page.click('#new-tab-btn')
        other_id = page.evaluate('() => window.terminalTest.getTerminalTabsState().activeTerminalId')
        page.click('#connectBtn')
        page.wait_for_function('() => window.terminalTest.getActiveAgentState()?.connected === true')
        emit_socket(page, 'agent_attach', {'terminal_id': other_id})
        page.wait_for_function("() => window.terminalTest.getActiveAgentState()?.mode === 'observe'")
        page.evaluate('id => window.terminalTest.switchTerminalForTest(id)', TERMINAL_ID)
        page.evaluate('id => window.terminalTest.setAgentPanelTargetForTest(id)', other_id)
        page.set_viewport_size({'width': 640, 'height': 600})
        for selector in ['#new-tab-btn', '#agent-pause-btn', '#agent-toggle-btn', '#quick-settings']:
            bounds = page.locator(selector).bounding_box()
            check(bounds is not None and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 640,
                  f'{selector} is clipped in the compact toolbar')
        page.evaluate('() => window.terminalTest.clearEmitted()')
        page.click('#agent-pause-btn')
        wait_for_agent(page, "state.mode === 'paused'")
        events = page.evaluate('() => window.terminalTest.getEmitted()')
        paused = [event['args'][0]['terminal_id'] for event in events if event['event'] == 'agent_pause']
        check(paused == [TERMINAL_ID], 'main-toolbar Pause targeted the overridden Agent Panel')
        check(page.evaluate('id => window.terminalTest.getAgentStateForTest(id).mode', other_id) == 'observe',
              'main-toolbar Pause changed the other terminal')
        page.click('#agent-toggle-btn')
        tabs = page.evaluate('() => window.terminalTest.getTerminalTabsState()')
        check(tabs['agentPanelTerminalId'] == TERMINAL_ID, 'main Agent button retained the panel override')
        check(page.locator('#agent-panel').is_visible(), 'main Agent button hid the overridden panel instead of opening the main panel')
    finally:
        close_context(context)


def test_agent_mint_quick_action_applies_saved_permission(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        placement = page.evaluate(
            """() => ({
                firstTool: document.querySelector('#terminal-tools > :first-child')?.id,
                text: document.getElementById('agent-access-mint-btn').innerText,
                disabled: document.getElementById('agent-access-mint-btn').disabled,
                panelVisible: document.getElementById('agent-panel').classList.contains('visible')
            })"""
        )
        check(placement == {
            'firstTool': 'agent-access-mint-btn',
            'text': '🤖 Authorize agent',
            'disabled': False,
            'panelVisible': False,
        }, 'Agent Mint was not the ready leftmost right-side action')

        page.click('#quick-settings')
        check(page.locator('#pref-agentAccessMintMode').input_value() == 'direct_active',
              'Agent Mint permission did not default to Full + Mint')
        page.click('#settings-close')

        clear_emitted(page)
        page.click('#agent-access-mint-btn')
        page.wait_for_function(
            """() => {
                const state = window.terminalTest.getActiveAgentState();
                return state?.mode === 'direct_active' && state?.external_token?.status === 'active';
            }""",
            timeout=10000,
        )
        minted = active_agent_state(page)
        check(minted['mode'] == 'direct_active', 'one-click Agent Mint did not apply Full permission')
        check(minted['external_token']['idleTimeoutMultiplier'] == 1,
              'one-click Agent Mint did not mint the standard token lifetime')
        check(page.locator('#agent-access-mint-btn').inner_text() == '🤖 Authorize agent',
              'Agent Mint action did not return to its ready label')
        mode_events = [
            entry['args'][0] for entry in get_emitted(page, 'agent_mode_set')
            if entry['args'] and entry['args'][0].get('terminal_id') == TERMINAL_ID
        ]
        check(mode_events == [{'terminal_id': TERMINAL_ID, 'mode': 'direct_active'}],
              'Agent Mint did not make one structured Full permission request')

        emit_socket(page, 'agent_mode_set', {'terminal_id': TERMINAL_ID, 'mode': 'disabled'})
        wait_for_agent(page, "state.mode === 'disabled'")
        page.click('#quick-settings')
        page.select_option('#pref-agentAccessMintMode', 'approval_pending')
        page.click('#settings-save')
        clear_emitted(page)
        page.click('#agent-access-mint-btn')
        page.wait_for_function(
            """() => {
                const state = window.terminalTest.getActiveAgentState();
                return state?.mode === 'approval_pending' && state?.external_token?.status === 'active';
            }""",
            timeout=10000,
        )
        check('Authorize Approval required access' in page.locator('#agent-access-mint-btn').get_attribute('title'),
              'Agent Mint title did not reflect the saved permission')
    finally:
        close_context(context)


def test_agent_language_preview_preserves_access_and_applies_on_next_page(browser, access_url):
    context, page = new_page(browser, access_url, ui_language='zh-TW')
    token_requests = []
    page.on('request', lambda request: token_requests.append(request.url)
            if request.method == 'POST' and urllib.parse.urlparse(request.url).path == '/agent/external/token'
            else None)
    try:
        check(page.locator('html').get_attribute('lang') == 'zh-TW', 'saved language did not set the document language')
        check(page.inner_text('#agent-access-mint-btn') == '🤖 授權 Agent', 'authorization action was not localized')
        check(page.inner_text('#agent-connect-btn') == 'Agent 連線', 'connection action was not localized')
        clear_emitted(page)
        page.click('#agent-access-mint-btn')
        wait_for_agent(page, "state.mode === 'direct_active' && state.external_token?.status === 'active'")
        check(len(token_requests) == 1, 'localized authorization did not create exactly one token')
        check([entry['args'][0] for entry in get_emitted(page, 'agent_mode_set')] == [
            {'terminal_id': TERMINAL_ID, 'mode': 'direct_active'}
        ], 'localized authorization changed the permission protocol value')
        check('--token' in active_agent_state(page)['external_token']['command'],
              'localized authorization did not preserve the generated command')

        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible')
        modes = page.locator('[data-agent-mode]').evaluate_all(
            "buttons => buttons.map(button => ({value: button.dataset.agentMode, label: button.innerText}))"
        )
        check(modes == [
            {'value': 'observe', 'label': '唯讀'},
            {'value': 'approval_pending', 'label': '需核准'},
            {'value': 'direct_active', 'label': '直接輸入'},
        ], 'localized permission labels changed their protocol values')
        check(page.locator('#agent-mode-controls').get_attribute('aria-label') == 'Agent 權限',
              'permission selector accessible name was not localized')
        check(page.inner_text('#agent-external-token-btn') == '更新權杖', 'active token action was not localized')
        check('檔案複製仍需核准' in page.inner_text('#agent-permission-hint'),
              'localized direct input omitted file-copy approval')
        page.click('#agent-panel-close-btn')

        page.evaluate('''() => Object.defineProperty(navigator, 'clipboard', {
            configurable: true, value: {writeText: async text => {window.copiedAgentText = text;}}
        })''')
        page.click('#agent-connect-btn')
        page.wait_for_selector('#agent-connect-copy:not([disabled])')
        check(page.get_by_role('dialog', name='Agent 連線').is_visible(), 'connection dialog accessible name was not localized')
        check(page.locator('#agent-connect-url').get_attribute('aria-label') == 'Agent 資訊網址',
              'connection URL accessible name was not localized')
        check(page.locator('#agent-connect-info').get_attribute('aria-label') == 'Agent 連線指引',
              'connection prompt accessible name was not localized')
        check(page.inner_text('#agent-connect-copy') == '複製連線指引', 'connection copy action was not localized')
        check('main: 等待 Agent' in page.text_content('#agent-connect-activity'),
              'localized connection activity did not distinguish a grant from Agent activity')
        prompt = page.input_value('#agent-connect-info')
        check('Run discover, then hello' in prompt, 'display language translated the machine-facing connection prompt')
        page.click('#agent-connect-copy')
        page.wait_for_function('text => window.copiedAgentText === text', arg=prompt)
        check('已複製' in page.inner_text('#agent-connect-message'), 'clipboard result was not localized')
        page.click('#agent-connect-close')

        page.evaluate('() => { window.languagePreviewSentinel = true; }')
        before = active_agent_state(page)
        socket_before = page.evaluate('() => window.terminalTest.getSocketState()')
        requests_before = len(token_requests)
        page.click('#quick-settings')
        check(page.input_value('#pref-uiLanguage') == 'zh-TW', 'settings did not show the saved language')
        check(page.input_value('#pref-agentAccessMintMode') == 'direct_active', 'language changed the saved permission')
        page.select_option('#pref-uiLanguage', 'en')
        clear_emitted(page)
        page.click('#settings-save')
        page.wait_for_selector('#settings-modal.open', state='hidden')
        check(page.evaluate("() => JSON.parse(localStorage.getItem('terminal.pref.v1')).uiLanguage") == 'en',
              'settings did not save the display preference')
        check(page.evaluate('() => window.languagePreviewSentinel === true'), 'saving language reloaded the active page')
        check(page.inner_text('#agent-access-mint-btn') == '🤖 授權 Agent', 'language changed before opening another page')
        check(page.locator('html').get_attribute('lang') == 'zh-TW', 'active document language changed before reopening')
        after = active_agent_state(page)
        for field in ['terminal_id', 'connected', 'session_id', 'viewer_id', 'agent_binding_id',
                      'mode', 'mode_version', 'external_token']:
            check(after[field] == before[field], f'saving language changed Agent state field {field}')
        check(page.evaluate('() => window.terminalTest.getSocketState()') == socket_before,
              'saving language changed the active socket')
        check(len(token_requests) == requests_before, 'saving language created or renewed a token')
        check(not any(entry['event'] in {'agent_attach', 'agent_detach', 'agent_mode_set', 'agent_pause', 'start_ssh', 'stop_ssh'}
                      for entry in get_emitted(page)), 'saving language emitted an access or connection mutation')

        next_page = context.new_page()
        next_page.goto(debug_url(access_url), wait_until='domcontentloaded')
        next_page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        check(next_page.locator('html').get_attribute('lang') == 'en', 'new page did not apply the saved language')
        check(next_page.inner_text('#agent-access-mint-btn') == '🤖 Authorize agent', 'new page did not show English authorization')
        check(next_page.inner_text('#agent-connect-btn') == 'Agent connection', 'new page did not show English connection text')
        check(next_page.locator('#agent-mode-controls').get_attribute('aria-label') == 'Agent permission',
              'new page did not apply English accessible names')
        check(page.inner_text('#agent-access-mint-btn') == '🤖 授權 Agent', 'new page changed the existing page language')
    finally:
        close_context(context)


def test_agent_panel_can_be_dragged(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible', timeout=5000)
        before = page.locator('#agent-panel').bounding_box()
        header = page.locator('#agent-panel-header').bounding_box()
        check(before is not None and header is not None, 'agent panel/header did not render')
        page.mouse.move(header['x'] + 20, header['y'] + 10)
        page.mouse.down()
        page.mouse.move(header['x'] - 150, header['y'] - 90)
        page.mouse.up()
        after = page.locator('#agent-panel').bounding_box()
        check(after is not None, 'agent panel disappeared after drag')
        check(abs(after['x'] - before['x']) > 40, 'agent panel x position did not change after drag')
        check(abs(after['y'] - before['y']) > 40, 'agent panel y position did not change after drag')
        saved = page.evaluate("() => JSON.parse(localStorage.getItem('agentPanelPosition.v1'))")
        check(isinstance(saved.get('left'), (int, float)), 'agent panel left position was not saved')
        check(isinstance(saved.get('top'), (int, float)), 'agent panel top position was not saved')
        page.click('#agent-panel-close-btn')
        page.wait_for_function(
            "() => !document.getElementById('agent-panel').classList.contains('visible')",
            timeout=5000,
        )
    finally:
        close_context(context)


def test_terminal_pip_hides_selected_tab_and_keeps_background_tab(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    context, page = new_page(browser, access_url, ui_language)
    try:
        initial = page.evaluate(
            """() => ({
                canPip: window.terminalTest.canMoveActiveTerminalToPip(),
                tabs: window.terminalTest.getTerminalTabsState()
            })"""
        )
        check(initial['canPip'] is False, 'single tab should not offer Terminal to PiP')
        check(len(initial['tabs']['tabs']) == 1, 'initial workspace should have one terminal tab')

        page.click('#new-tab-btn')
        page.wait_for_function("() => window.terminalTest.getTerminalTabsState().tabs.length === 2", timeout=5000)
        page.evaluate("terminalId => window.terminalTest.switchTerminalForTest(terminalId)", TERMINAL_ID)
        page.wait_for_function(
            "terminalId => window.terminalTest.getTerminalTabsState().activeTerminalId === terminalId",
            arg=TERMINAL_ID,
            timeout=5000,
        )
        before_pip = page.evaluate(
            """() => ({
                canPip: window.terminalTest.canMoveActiveTerminalToPip(),
                tabs: window.terminalTest.getTerminalTabsState()
            })"""
        )
        check(before_pip['canPip'] is True, 'two visible tabs should offer Terminal to PiP')
        active_id = before_pip['tabs']['activeTerminalId']
        background_id = next(item['id'] for item in before_pip['tabs']['tabs'] if item['id'] != active_id)

        attach_agent(page)
        page.evaluate(
            "payload => window.terminalTest.writeTerminalOutput(payload)",
            '\x1b]2;PiP workspace title\x07',
        )
        page.wait_for_function(
            "() => document.getElementById('terminal-title').innerText === 'PiP workspace title'",
            timeout=5000,
        )
        check(page.evaluate('() => !!window.documentPictureInPicture'), 'Document PiP is unavailable in the test browser')
        page.evaluate("terminalId => window.terminalTest.showContextMenuForTest(terminalId)", active_id)
        page.click('#pip-option')
        page.wait_for_function('() => !!window.documentPictureInPicture.window', timeout=5000)

        pip_status = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                return {
                    applicationTitle: pipDocument.querySelector('.pip-application-title')?.innerText,
                    applicationTitleHidden: pipDocument.querySelector('.pip-application-title-item')?.hidden,
                    mintText: pipDocument.querySelector('.pip-agent-mint:not(.pip-agent-mint-3x)')?.innerText,
                    mintHidden: pipDocument.querySelector('.pip-agent-mint:not(.pip-agent-mint-3x)')?.hidden,
                    mint3xText: pipDocument.querySelector('.pip-agent-mint-3x')?.innerText,
                    mint3xHidden: pipDocument.querySelector('.pip-agent-mint-3x')?.hidden,
                    agentPanelText: pipDocument.querySelector('.pip-agent-panel')?.innerText
                };
            }"""
        )
        check(pip_status['applicationTitle'] == 'PiP workspace title', 'Terminal PiP did not show the OSC title')
        check(pip_status['applicationTitleHidden'] is False, 'Terminal PiP hid a non-empty OSC title')
        check(pip_status['mintText'] == message(page, 'agent.token.create') and pip_status['mintHidden'] is False, 'Terminal PiP did not show Create token')
        check(pip_status['mint3xText'] == message(page, 'agent.token.create_3x') and pip_status['mint3xHidden'] is False, 'Terminal PiP did not show Create token 3x')
        check(pip_status['agentPanelText'] == message(page, 'agent.panel.show'), 'Terminal PiP Agent panel control was incorrect')
        compact_pip = page.evaluate("""() => {
            const doc = documentPictureInPicture.window.document;
            doc.documentElement.style.width = '480px';
            doc.documentElement.style.height = '600px';
            const buttons = [...doc.querySelectorAll('.pip-agent-button')].filter(button => button.getClientRects().length);
            const metrics = buttons.map(button => {
                const rect = button.getBoundingClientRect();
                return {text:button.innerText,left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom};
            });
            doc.documentElement.style.width = '';
            doc.documentElement.style.height = '';
            return metrics;
        }""")
        check(all(0 <= item['left'] < item['right'] <= 480 and 0 <= item['top'] < item['bottom'] <= 600 for item in compact_pip),
              f'Terminal PiP controls overflow at 480x600: {compact_pip}')

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        page.uncheck('#pref-showTerminalTitleInStatusBar')
        page.click('#settings-save')
        page.wait_for_function(
            "() => documentPictureInPicture.window.document.querySelector('.pip-application-title-item').hidden",
            timeout=5000,
        )

        in_pip = page.evaluate(
            """() => ({
                canPip: window.terminalTest.canMoveActiveTerminalToPip(),
                tabs: window.terminalTest.getTerminalTabsState()
            })"""
        )
        moved_tab = next(item for item in in_pip['tabs']['tabs'] if item['id'] == active_id)
        background_tab = next(item for item in in_pip['tabs']['tabs'] if item['id'] == background_id)
        check(in_pip['canPip'] is False, 'remaining single background tab should not offer another PiP move')
        check(in_pip['tabs']['activeTerminalId'] == background_id, 'background did not switch to remaining tab')
        check(moved_tab['inPip'] is True and moved_tab['hidden'] is True, 'PiP tab did not disappear from tab list')
        check(background_tab['active'] is True and background_tab['hidden'] is False, 'remaining tab was not active and visible')

        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.pip-agent-mint-3x').click()")
        page.wait_for_function(
            """terminalId => {
                const token = window.terminalTest.getAgentStateForTest(terminalId)?.external_token;
                return token && (token.status === 'active' || token.status === 'error');
            }""",
            arg=active_id,
            timeout=5000,
        )
        token_state = page.evaluate(
            """terminalId => ({
                token: window.terminalTest.getAgentStateForTest(terminalId)?.external_token,
                activeTerminalId: window.terminalTest.getTerminalTabsState().activeTerminalId
            })""",
            active_id,
        )
        check(token_state['token']['status'] == 'active', 'Terminal PiP Mint+ did not mint an active token')
        check(token_state['token']['idleTimeoutMultiplier'] == 3, 'Terminal PiP Mint+ did not request the 3x lifetime')
        check(token_state['activeTerminalId'] == background_id, 'Terminal PiP Mint+ changed the main active terminal')

        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.pip-agent-panel').click()")
        page.wait_for_function(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                return !!pipDocument.querySelector('#agent-panel.visible')
                    && pipDocument.querySelector('.pip-agent-mint').hidden
                    && pipDocument.querySelector('.pip-agent-mint-3x').hidden;
            }""",
            timeout=5000,
        )
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('#agent-panel-close-btn').click()")
        page.wait_for_function(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                return !pipDocument.querySelector('.pip-agent-mint').hidden
                    && !pipDocument.querySelector('.pip-agent-mint-3x').hidden;
            }""",
            timeout=5000,
        )

        page.evaluate('() => documentPictureInPicture.window.close()')
        page.wait_for_function('() => !window.documentPictureInPicture.window', timeout=5000)
        restored = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        restored_tab = next(item for item in restored['tabs'] if item['id'] == active_id)
        check(restored_tab['inPip'] is False and restored_tab['hidden'] is False, 'restored PiP tab did not return to tab list')
    finally:
        close_context(context)


def test_sftp_status_actions_and_terminal_pip_transition(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    context, page = new_page(browser, access_url, ui_language)

    def text(key, params=None):
        return message(page, 'browser.files.' + key, params)
    try:
        page.evaluate(
            """() => window.terminalTest.applyTerminalListForTest({
                terminals: [
                    {
                        terminal_id: 'main',
                        connection_type: 'ssh',
                        terminal_label: 'SSH',
                        term: 'xterm-256color',
                        files_available: true,
                        connected: true
                    },
                    {
                        terminal_id: 'term-2',
                        connection_type: 'local_shell',
                        terminal_label: 'bash',
                        term: 'xterm-256color',
                        files_available: true,
                        connected: true
                    }
                ]
            })"""
        )
        page.evaluate("() => window.terminalTest.switchTerminalForTest('main')")
        available_status = page.evaluate(
            """() => {
                const button = document.getElementById('sftp-status-btn');
                return {
                    hidden: button.hidden,
                    disabled: button.disabled,
                    title: button.title,
                    text: button.innerText
                };
            }"""
        )
        check(
            available_status == {
                'hidden': False,
                'disabled': False,
                'title': message(page, 'browser.chrome.files_open'),
                'text': '📁',
            },
            'connected SSH status bar did not expose the SFTP action',
        )
        page.click('#sftp-status-btn')
        page.wait_for_function(
            "expected => documentPictureInPicture.window?.document.querySelector('.sftp-pip-title')?.textContent === expected",
            arg=text('title'),
            timeout=5000,
        )
        page.evaluate('() => documentPictureInPicture.window.close()')
        page.wait_for_function('() => !window.documentPictureInPicture.window', timeout=5000)

        page.evaluate("() => window.terminalTest.setSftpAvailabilityForTest('main', false)")
        unavailable_status = page.evaluate(
            """() => {
                const button = document.getElementById('sftp-status-btn');
                const mark = button.querySelector('.sftp-unavailable-mark');
                return {
                    hidden: button.hidden,
                    disabled: button.disabled,
                    title: button.title,
                    text: button.innerText,
                    markColor: getComputedStyle(mark).color
                };
            }"""
        )
        check(unavailable_status['hidden'] is False, 'unavailable SFTP status action disappeared')
        check(unavailable_status['disabled'] is True, 'unavailable SFTP status action remained enabled')
        check(unavailable_status['title'] == message(page, 'browser.chrome.files_unavailable'), 'unavailable Files status hint was unclear')
        check('×' in unavailable_status['text'], 'unavailable SFTP status action omitted its cross mark')
        check(unavailable_status['markColor'] == 'rgb(255, 69, 58)', 'unavailable SFTP cross was not red')
        unavailable_menu = page.evaluate("() => window.terminalTest.showContextMenuForTest('main')")
        check(unavailable_menu['sftpVisible'] is True, 'unavailable SSH context action disappeared')
        check(unavailable_menu['sftpDisabled'] is True, 'unavailable SSH context action remained enabled')
        check(message(page, 'browser.chrome.files_unavailable') in unavailable_menu['sftpText'], 'unavailable SSH context action hint was unclear')

        page.evaluate("() => window.terminalTest.setSftpAvailabilityForTest('main', null)")
        page.evaluate("() => window.terminalTest.showContextMenuForTest('main')")
        page.click('#pip-option')
        page.wait_for_function('() => !!window.documentPictureInPicture.window', timeout=5000)
        pip_action = page.evaluate(
            """() => {
                const button = documentPictureInPicture.window.document.querySelector('.pip-sftp-button');
                return {
                    hidden: button.hidden,
                    disabled: button.disabled,
                    title: button.title,
                    text: button.innerText
                };
            }"""
        )
        check(
            pip_action == {
                'hidden': False,
                'disabled': False,
                'title': message(page, 'browser.chrome.files_open'),
                'text': '📁',
            },
            'Terminal PiP did not expose the SFTP action',
        )

        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.pip-sftp-button').click()")
        page.wait_for_function(
            "expected => documentPictureInPicture.window?.document.querySelector('.sftp-pip-title')?.textContent === expected",
            arg=text('title'),
            timeout=5000,
        )
        restored = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        restored_main = next(item for item in restored['tabs'] if item['id'] == 'main')
        check(restored_main['inPip'] is False, 'opening SFTP from Terminal PiP did not restore the terminal')
        page.evaluate('() => documentPictureInPicture.window.close()')
        page.wait_for_function('() => !window.documentPictureInPicture.window', timeout=5000)
    finally:
        close_context(context)


def test_sftp_send_context_action_is_limited_to_connected_ssh_tabs(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    context, page = new_page(browser, access_url, ui_language)

    def text(key, params=None):
        return message(page, 'browser.files.' + key, params)
    browser_console = []
    page.on('console', lambda message: browser_console.append(message.text))
    try:
        page.evaluate(
            """() => window.terminalTest.applyTerminalListForTest({
                terminals: [
                    {
                        terminal_id: 'main',
                        connection_type: 'ssh',
                        terminal_label: 'hcbox',
                        term: 'xterm-256color',
                        files_available: true,
                        connected: true
                    },
                    {
                        terminal_id: 'term-2',
                        connection_type: 'local_shell',
                        terminal_label: 'Local Shell',
                        term: 'xterm-256color',
                        files_available: true,
                        connected: true
                    }
                ]
            })"""
        )
        ssh_menu = page.evaluate("() => window.terminalTest.showContextMenuForTest('main')")
        check(ssh_menu['terminalId'] == 'main', 'SFTP context action targeted the wrong terminal')
        check(ssh_menu['sftpVisible'] is True, 'connected SSH tab did not show SFTP send action')
        check(message(page, 'browser.chrome.files_open') in ssh_menu['sftpText'], 'Files context action label was unclear')
        check(page.evaluate('() => !!window.documentPictureInPicture'), 'Document PiP is unavailable in the test browser')
        page.click('#sftp-send-option')
        page.wait_for_function('() => !!window.documentPictureInPicture.window', timeout=5000)
        pip_state = page.evaluate(
            """() => ({
                title: documentPictureInPicture.window.document.querySelector('.sftp-pip-title')?.textContent,
                documentTitle: documentPictureInPicture.window.document.title,
                hint: documentPictureInPicture.window.document.querySelector('.sftp-direct-hint')?.textContent,
                hasDropZone: !!documentPictureInPicture.window.document.querySelector('.sftp-drop-zone'),
                hasPathInput: !!documentPictureInPicture.window.document.querySelector('.sftp-path-input'),
                innerCloseCount: documentPictureInPicture.window.document.querySelectorAll('.sftp-pip-close').length,
                navigationTitles: [...documentPictureInPicture.window.document.querySelectorAll('.sftp-path-controls .sftp-icon-button')]
                    .map(button => button.title)
            })"""
        )
        check(pip_state['title'] == text('title'), 'Files PiP title was missing')
        check(pip_state['documentTitle'] == text('title'), 'Files document title was missing')
        check(pip_state['hint'] == text('endpoint_hint'), 'SFTP PiP did not explain the direct endpoint boundary')
        check(pip_state['hasDropZone'] is True, 'SFTP PiP did not expose a file drop zone')
        check(pip_state['hasPathInput'] is True, 'SFTP PiP did not expose destination path navigation')
        check(pip_state['innerCloseCount'] == 0, 'Files kept a duplicate close control')
        check(
            pip_state['navigationTitles'] == [text(key) for key in ['home','parent','refresh']],
            'Files navigation icons did not expose clear descriptions',
        )

        page.wait_for_function(
            "expected => documentPictureInPicture.window.document.querySelector('.sftp-transfer-status')?.textContent !== expected",
            arg=text('opening'),
            timeout=5000,
        )
        rendered = page.evaluate(
            """() => window.terminalTest.renderSftpEntriesForTest({
                path: '/home/tester',
                directories: [{ name: 'docs', mtime: 20 }],
                files: [
                    { file_id: 'sftpf_random_a', name: 'reference.txt', size: 9, mtime: 25 },
                    { file_id: 'sftpf_random_b', name: 'existing.txt', size: 4, mtime: 26 }
                ]
            })"""
        )
        check(rendered is True, 'SFTP PiP test fixture could not render remote files')
        clear_emitted(page)
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-refresh').click()")
        refresh_requests = get_emitted(page, 'sftp_browse_request')
        check(len(refresh_requests) == 1, 'Files Refresh did not request a new directory listing')
        refresh_payload = refresh_requests[0]['args'][0]
        check(refresh_payload['path'] == '/home/tester', 'Files Refresh did not keep the current directory')
        page.evaluate(
            """payload => window.terminalTest.handleSftpBrowseResultForTest({
                request_id: payload.request_id,
                terminal_id: 'main',
                status: 'ready',
                path: '/home/tester',
                endpoint: { user: 'tester', host: 'host.example', port: 22, route: 'direct' },
                directories: [{ name: 'docs', mtime: 20 }],
                files: [
                    { file_id: 'sftpf_random_a', name: 'reference.txt', size: 9, mtime: 25 },
                    { file_id: 'sftpf_random_b', name: 'existing.txt', size: 4, mtime: 26 }
                ],
                truncated: false,
                max_upload_bytes: 1024
            })""",
            refresh_payload,
        )
        clear_emitted(page)
        file_ui = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const rows = () => [...pipDocument.querySelectorAll('.sftp-directory-entry')]
                    .map(button => button.dataset.entryName);
                const sort = column => pipDocument.querySelector(`[data-sort-column="${column}"]`).click();
                const initialRows = rows();
                sort('size');
                sort('size');
                const sizeDescendingRows = rows();
                sort('date');
                sort('date');
                const dateDescendingRows = rows();
                sort('name');
                const list = pipDocument.querySelector('.sftp-directory-list');
                list.focus();
                list.dispatchEvent(new KeyboardEvent('keydown', { key: 'r', bubbles: true }));
                list.dispatchEvent(new KeyboardEvent('keydown', { key: 'e', bubbles: true }));
                const typeaheadMatch = pipDocument.activeElement?.dataset.entryName;
                const files = [...pipDocument.querySelectorAll('.sftp-file-entry')];
                const reference = files.find(button => button.dataset.entryName === 'reference.txt');
                pipDocument.documentElement.style.height = '360px';
                reference.click();
                const preparing = {
                    disabled: pipDocument.querySelector('.sftp-file-download').disabled,
                    text: pipDocument.querySelector('.sftp-file-download').innerText
                };
                const request = window.terminalTest.getEmitted()
                    .find(item => item.event === 'sftp_download_ticket_request');
                window.terminalTest.handleSftpDownloadTicketResultForTest({
                    request_id: request.args[0].request_id,
                    terminal_id: 'main',
                    status: 'ready',
                    download_url: '/sftp/download/test-ticket',
                    download_id: 'sftpd_testlog',
                    filename: 'reference.txt',
                    size: 9,
                    expires_in_seconds: 60
                });
                return {
                    fileCount: files.length,
                    columnHeaders: [...pipDocument.querySelectorAll('.sftp-sort-button')].map(button => button.innerText),
                    initialRows,
                    sizeDescendingRows,
                    dateDescendingRows,
                    typeaheadMatch,
                    operationVisible: pipDocument.querySelector('.sftp-file-operation-box').classList.contains('visible'),
                    actions: [...pipDocument.querySelectorAll('.sftp-file-operation-box .sftp-file-operation-actions button')]
                        .map(button => button.innerText),
                    operationPath: pipDocument.querySelector('.sftp-file-operation-path').innerText,
                    operationMeta: pipDocument.querySelector('.sftp-file-operation-meta').innerText,
                    preparing,
                    downloadReady: !pipDocument.querySelector('.sftp-file-download').disabled,
                    selected: reference.classList.contains('selected'),
                    selectedPressed: reference.getAttribute('aria-pressed'),
                    sourceOverflow: pipDocument.defaultView.getComputedStyle(
                        pipDocument.querySelector('.sftp-source-pane')).overflowY,
                    actionBottom: pipDocument.querySelector('.sftp-file-operation-actions')
                        .getBoundingClientRect().bottom,
                    viewportHeight: pipDocument.documentElement.clientHeight,
                    status: pipDocument.querySelector('.sftp-transfer-status').innerText
                };
            }"""
        )
        check(file_ui['fileCount'] == 2, 'SFTP PiP did not list regular files')
        check(file_ui['columnHeaders'] == [text('name') + ' ▲', text('size'), text('date')], 'Files list did not expose sortable columns')
        check(file_ui['initialRows'] == ['docs', 'existing.txt', 'reference.txt'], 'Files Name sort did not keep folders first')
        check(file_ui['sizeDescendingRows'] == ['docs', 'reference.txt', 'existing.txt'], 'Files Size toggle did not sort descending')
        check(file_ui['dateDescendingRows'] == ['docs', 'existing.txt', 'reference.txt'], 'Files Date toggle did not sort descending')
        check(file_ui['typeaheadMatch'] == 'reference.txt', 'Files typeahead did not accumulate a quick prefix')
        check(file_ui['operationVisible'] is True, 'selecting an SFTP file did not open file actions')
        check(file_ui['actions'] == [text(key) for key in ['download','copy_to','rename_action','delete_action']], 'Files actions were incomplete')
        check(file_ui['operationPath'] == '/home/tester/reference.txt', 'selected file card omitted the full path')
        check(text('detailed_bytes', {'size':'9 B','bytes':9}) in file_ui['operationMeta'] and '1970' in file_ui['operationMeta'], 'selected file card omitted exact file metadata')
        check(file_ui['preparing'] == {'disabled': True, 'text': text('preparing')}, 'SFTP Download was enabled before its ticket was ready')
        check(file_ui['downloadReady'] is True, 'SFTP Download was not enabled after its ticket became ready')
        check(file_ui['selected'] is True and file_ui['selectedPressed'] == 'true', 'selected Files row was not highlighted')
        check(file_ui['sourceOverflow'] == 'auto', 'short Files source pane did not provide a scroll fallback')
        check(file_ui['actionBottom'] <= file_ui['viewportHeight'], 'short Files window clipped the selected file actions')
        compact_files = page.evaluate("""() => {
            const doc = documentPictureInPicture.window.document;
            doc.documentElement.style.width = '480px';
            doc.documentElement.style.height = '600px';
            const metrics = [...doc.querySelectorAll('.sftp-path-controls button, .sftp-file-operation-actions button')]
                .filter(button => button.getClientRects().length).map(button => {
                    const rect = button.getBoundingClientRect();
                    return {text:button.innerText,title:button.title,left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom};
                });
            doc.documentElement.style.width = '';
            return metrics;
        }""")
        check(all(0 <= item['left'] < item['right'] <= 480 and 0 <= item['top'] < item['bottom'] <= 600 for item in compact_files),
              f'Files controls overflow at 480x600: {compact_files}')
        page.evaluate("() => { documentPictureInPicture.window.document.documentElement.style.height = ''; }")
        check(
            file_ui['status'] == text('download_ready', {'name':'reference.txt'}),
            'selecting a file implied that it had already downloaded',
        )

        download_requests = get_emitted(page, 'sftp_download_ticket_request')
        check(len(download_requests) == 1, 'selecting an SFTP file did not prepare one download ticket')
        download_payload = download_requests[0]['args'][0]
        check(download_payload['file_id'] == 'sftpf_random_a', 'SFTP Download did not use the backend file ID')
        check('filename' not in download_payload and 'directory' not in download_payload, 'SFTP Download used display names as control data')
        browser_download = page.evaluate(
            """() => {
                let clicked = null;
                const pipWindow = documentPictureInPicture.window;
                const originalClick = pipWindow.HTMLAnchorElement.prototype.click;
                pipWindow.HTMLAnchorElement.prototype.click = function () {
                    clicked = {
                        href: this.href,
                        filename: this.download,
                        target: this.target,
                        rel: this.rel,
                        hiddenByStyle: this.style.display === 'none',
                        ownerIsPipDocument: this.ownerDocument === pipWindow.document
                    };
                };
                try {
                    documentPictureInPicture.window.document.querySelector('.sftp-file-download').click();
                    return {
                        clicked,
                        remainingLinks: document.querySelectorAll('a[href*="/sftp/download/"]').length,
                        buttonDisabled: documentPictureInPicture.window.document.querySelector('.sftp-file-download').disabled,
                        buttonText: documentPictureInPicture.window.document.querySelector('.sftp-file-download').innerText
                    };
                } finally {
                    pipWindow.HTMLAnchorElement.prototype.click = originalClick;
                }
            }"""
        )
        check(browser_download['clicked'] is not None, 'the ready SFTP Download button did not trigger a browser download')
        access_parts = urllib.parse.urlsplit(access_url)
        access_origin = f'{access_parts.scheme}://{access_parts.netloc}'
        check(browser_download['clicked']['href'].startswith(access_origin + '/sftp/download/'), 'SFTP browser download lost the main page origin')
        check(browser_download['clicked']['filename'] == '', 'SFTP browser download did not defer the filename to Content-Disposition')
        check(browser_download['clicked']['target'] == '_blank', 'SFTP browser download tried to navigate the non-navigable PiP window')
        check(browser_download['clicked']['rel'] == 'noopener', 'SFTP browser download did not isolate the top-level download context')
        check(browser_download['clicked']['hiddenByStyle'] is True, 'SFTP browser download trigger could become visible')
        check(browser_download['clicked']['ownerIsPipDocument'] is True, 'SFTP browser download did not preserve the PiP user-activation context')
        check(browser_download['remainingLinks'] == 0, 'SFTP browser download trigger was not removed')
        check(browser_download['buttonDisabled'] is True and browser_download['buttonText'] == text('download_started'), 'used SFTP download ticket remained actionable')
        check(any(message.startswith('[sftp] Download ticket requested') for message in browser_console), 'SFTP browser log omitted the ticket request')
        check(any(message.startswith('[sftp] Download ticket ready') for message in browser_console), 'SFTP browser log omitted the ready ticket')
        check(any(message.startswith('[sftp] Download button clicked') for message in browser_console), 'SFTP browser log omitted the explicit click')
        check(any(message.startswith('[sftp] Download link dispatched') for message in browser_console), 'SFTP browser log omitted the link dispatch')

        selection_toggle = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const row = [...pipDocument.querySelectorAll('.sftp-file-entry')]
                    .find(button => button.dataset.entryName === 'reference.txt');
                row.click();
                const hiddenAfterToggle = !pipDocument.querySelector('.sftp-file-operation-box').classList.contains('visible');
                row.click();
                return {
                    hiddenAfterToggle,
                    visibleAfterToggle: pipDocument.querySelector('.sftp-file-operation-box').classList.contains('visible'),
                    selectedAfterToggle: row.classList.contains('selected')
                };
            }"""
        )
        check(selection_toggle == {
            'hiddenAfterToggle': True,
            'visibleAfterToggle': True,
            'selectedAfterToggle': True,
        }, 'clicking the selected file did not toggle selection without another action')

        clear_emitted(page)
        copy_picker = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                pipDocument.querySelector('.sftp-file-copy').click();
                const request = window.terminalTest.getEmitted()
                    .find(item => item.event === 'sftp_browse_request' && item.args[0].terminal_id === 'term-2');
                window.terminalTest.handleSftpBrowseResultForTest({
                    request_id: request.args[0].request_id,
                    terminal_id: 'term-2',
                    status: 'ready',
                    path: '/home/local',
                    endpoint: { route: 'local', shell: 'bash', platform: 'linux' },
                    directories: [{ name: 'work', mtime: 40 }],
                    files: [],
                    truncated: false,
                    max_upload_bytes: 1024
                });
                return {
                    open: pipDocument.querySelector('.sftp-files-workspace').classList.contains('copy-open'),
                    sourceHidden: pipDocument.defaultView.getComputedStyle(pipDocument.querySelector('.sftp-source-pane')).display === 'none',
                    title: pipDocument.querySelector('.sftp-destination-title').innerText,
                    sourcePath: pipDocument.querySelector('.sftp-copy-source-path').innerText,
                    sourceMeta: pipDocument.querySelector('.sftp-copy-source-meta').innerText,
                    sessions: [...pipDocument.querySelector('.sftp-session-select').options].map(option => option.value),
                    path: pipDocument.querySelector('.sftp-destination-pane .sftp-path-input').value,
                    filename: pipDocument.querySelector('.sftp-copy-name-input').value,
                    endpoint: pipDocument.querySelector('.sftp-destination-pane .sftp-endpoint-value').innerText,
                    innerCloseCount: pipDocument.querySelectorAll('.sftp-pip-close, .sftp-destination-close').length,
                    cancelText: pipDocument.querySelector('.sftp-copy-cancel').innerText,
                    lifecycle: pipDocument.querySelector('.sftp-copy-lifecycle-hint').innerText,
                    duplicateSummaryCount: pipDocument.querySelectorAll('.sftp-copy-summary').length
                };
            }"""
        )
        check(copy_picker['open'] is True and copy_picker['sourceHidden'] is True, 'Copy to did not switch to the destination Files pane')
        check(copy_picker['title'] == text('destination_title'), 'destination Files pane title was unclear')
        check(copy_picker['sourcePath'] == '/home/tester/reference.txt', 'destination Files pane omitted the source file path')
        check(text('detailed_bytes', {'size':'9 B','bytes':9}) in copy_picker['sourceMeta'], 'destination Files pane omitted exact source file metadata')
        check(copy_picker['sessions'] == ['term-2'], 'destination Files pane listed an invalid session')
        check(copy_picker['path'] == '/home/local', 'destination Files pane did not browse the selected session')
        check(copy_picker['filename'] == 'reference.txt', 'destination Files pane did not preserve the source name')
        check(copy_picker['endpoint'].startswith('Local Shell'), 'destination Files pane did not identify Local Shell')
        check(copy_picker['innerCloseCount'] == 0, 'destination Files pane kept a duplicate close icon')
        check(copy_picker['cancelText'] == message(page, 'common.cancel'), 'destination Files pane did not provide one clear pre-copy exit')
        check(copy_picker['lifecycle'] == text('cancel_before_copy'), 'destination Files pane did not explain pre-copy cancellation')
        check(copy_picker['duplicateSummaryCount'] == 0, 'destination Files pane kept a duplicate instruction card')

        clear_emitted(page)
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-copy-confirm').click()")
        copy_requests = get_emitted(page, 'files_copy_request')
        check(len(copy_requests) == 1, 'Copy did not emit one typed Files copy request')
        copy_payload = copy_requests[0]['args'][0]
        check(copy_payload['source_terminal_id'] == 'main', 'Files copy lost the source terminal')
        check(copy_payload['source_file_id'] == 'sftpf_random_a', 'Files copy did not use the opaque source file reference')
        check(copy_payload['destination_terminal_id'] == 'term-2', 'Files copy lost the destination terminal')
        check(copy_payload['destination_directory'] == '/home/local', 'Files copy lost the canonical destination view')
        check(copy_payload['destination_filename'] == 'reference.txt', 'Files copy lost the destination filename')
        check('source_path' not in copy_payload, 'Files copy trusted the displayed source path as authority')
        conflict_ui = page.evaluate(
            """payload => {
                const w = documentPictureInPicture.window;
                w.document.documentElement.style.height = '480px';
                w.document.documentElement.style.width = '620px';
                window.terminalTest.handleFilesCopyResultForTest({
                    request_id: payload.request_id, status: 'conflict',
                    destination_path: '/home/' + 'nested/'.repeat(100) + 'reference.txt', existing_size: 9
                });
                const box = w.document.querySelector('.sftp-destination-pane .sftp-conflict-box');
                const rect = box.getBoundingClientRect();
                return { visible: w.getComputedStyle(box).display !== 'none',
                    top: rect.top, bottom: rect.bottom };
            }""", copy_payload,
        )
        check(conflict_ui['visible'] and 0 <= conflict_ui['top'] < conflict_ui['bottom'] <= 480,
              'conflict choices were clipped in a short Files window')
        check(len(get_emitted(page, 'files_copy_request')) == 1,
              'conflict started another copy before the user chose an action')
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-destination-pane .sftp-conflict-keep').click()")
        copy_payload = get_emitted(page, 'files_copy_request')[-1]['args'][0]
        check(copy_payload['conflict_mode'] == 'keep_both', 'Keep Both lost the explicit conflict choice')
        copy_result_ui = page.evaluate(
            """payload => {
                window.terminalTest.handleFilesCopyResultForTest({
                    request_id: payload.request_id,
                    copy_id: 'filesc_test',
                    status: 'running',
                    revision: 0,
                    source_size: 9,
                    bytes_copied: 4,
                    total_bytes: 9,
                    destination_path: '/home/local/reference.txt'
                });
                window.terminalTest.handleFilesCopyResultForTest({
                    request_id: payload.request_id,
                    copy_id: 'filesc_foreign',
                    status: 'failed',
                    revision: 99,
                    message: 'Foreign copy failed.'
                });
                const statusAfterForeign = documentPictureInPicture.window.document
                    .querySelector('.sftp-destination-pane .sftp-transfer-status').innerText;
                const feedback = documentPictureInPicture.window.document.querySelector('.sftp-copy-feedback');
                const progress = feedback.querySelector('.sftp-progress');
                const progressVisible = progress.getBoundingClientRect().height >= 6
                    && feedback.getBoundingClientRect().top >= 0 && feedback.getBoundingClientRect().bottom <= 480;
                window.terminalTest.handleFilesCopyResultForTest({
                    request_id: payload.request_id,
                    copy_id: 'filesc_test',
                    status: 'committing',
                    revision: 1,
                    source_size: 9,
                    bytes_copied: 9,
                    total_bytes: 9,
                    destination_path: '/home/local/reference.txt'
                });
                const publishingButton = documentPictureInPicture.window.document.querySelector('.sftp-copy-cancel');
                const publishing = {
                    text: publishingButton.innerText,
                    disabled: publishingButton.disabled,
                    lifecycle: documentPictureInPicture.window.document
                        .querySelector('.sftp-copy-lifecycle-hint').innerText
                };
                window.terminalTest.handleFilesCopyResultForTest({
                    request_id: payload.request_id,
                    copy_id: 'filesc_test',
                    status: 'completed',
                    revision: 2,
                    source_size: 9,
                    bytes_copied: 9,
                    total_bytes: 9,
                    destination_path: '/home/local/reference.txt'
                });
                const refreshRequest = window.terminalTest.getEmitted()
                    .findLast(item => item.event === 'sftp_browse_request' && item.args[0].terminal_id === 'term-2');
                window.terminalTest.handleSftpBrowseResultForTest({
                    request_id: refreshRequest.args[0].request_id,
                    terminal_id: 'term-2',
                    status: 'ready',
                    path: '/home/local',
                    endpoint: { route: 'local', shell: 'bash', platform: 'linux' },
                    directories: [],
                    files: [{ file_id: 'sftpf_destination', name: 'reference.txt', size: 9, mtime: 30 }],
                    truncated: false,
                    max_upload_bytes: 1024
                });
                return {
                    statusAfterForeign,
                    progressVisible,
                    publishing,
                    terminalButtonText: documentPictureInPicture.window.document
                        .querySelector('.sftp-copy-cancel').innerText,
                    progressHidden: !documentPictureInPicture.window.document
                        .querySelector('.sftp-destination-pane .sftp-progress').classList.contains('visible')
                };
            }""",
            copy_payload,
        )
        check(copy_result_ui['statusAfterForeign'] == text('copy_progress', {'percent':44,'copied':'4 B','total':'9 B'}), 'Files copy accepted a foreign copy_id with the same request_id')
        check(copy_result_ui['progressVisible'], 'Files copy progress or cancel controls were clipped')
        page.evaluate("() => { const s = documentPictureInPicture.window.document.documentElement.style; s.height = ''; s.width = ''; }")
        check(copy_result_ui['publishing']['text'] == text('publishing'), 'commit barrier did not replace the cancel action')
        check(copy_result_ui['publishing']['disabled'] is True, 'commit barrier still allowed cancellation')
        check(copy_result_ui['publishing']['lifecycle'] == text('publishing_started'), 'commit barrier did not explain its cancellation boundary')
        check(copy_result_ui['terminalButtonText'] == message(page, 'common.close'), 'completed Files copy did not provide a clear close action')
        check(copy_result_ui['progressHidden'] is True, 'Files copy did not accept its bound terminal result')
        back_navigation = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                pipDocument.querySelector('.sftp-copy-cancel').click();
                return {
                    destinationClosed: !pipDocument.querySelector('.sftp-destination-pane'),
                    sourceVisible: pipDocument.defaultView.getComputedStyle(pipDocument.querySelector('.sftp-source-pane')).display !== 'none'
                };
            }"""
        )
        check(back_navigation == {'destinationClosed': True, 'sourceVisible': True}, 'destination Close did not return to the source pane')

        clear_emitted(page)
        page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                pipDocument.querySelector('.sftp-file-copy').click();
                const request = window.terminalTest.getEmitted()
                    .find(item => item.event === 'sftp_browse_request' && item.args[0].terminal_id === 'term-2');
                window.terminalTest.handleSftpBrowseResultForTest({
                    request_id: request.args[0].request_id,
                    terminal_id: 'term-2',
                    status: 'ready',
                    path: '/home/local',
                    endpoint: { route: 'local', shell: 'bash', platform: 'linux' },
                    directories: [],
                    files: [],
                    truncated: false,
                    max_upload_bytes: 1024
                });
            }"""
        )
        clear_emitted(page)
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-copy-confirm').click()")
        cancel_copy_payload = get_emitted(page, 'files_copy_request')[0]['args'][0]
        clear_emitted(page)
        cancel_pending_ui = page.evaluate(
            """payload => {
                window.terminalTest.handleFilesCopyResultForTest({
                    request_id: payload.request_id,
                    copy_id: 'filesc_cancel_test',
                    status: 'running',
                    revision: 0,
                    source_size: 9,
                    bytes_copied: 2,
                    total_bytes: 9,
                    destination_path: '/home/local/reference.txt'
                });
                const button = documentPictureInPicture.window.document.querySelector('.sftp-copy-cancel');
                const runningText = button.innerText;
                button.click();
                return {
                    runningText,
                    pendingText: button.innerText,
                    pendingDisabled: button.disabled,
                    lifecycle: documentPictureInPicture.window.document
                        .querySelector('.sftp-copy-lifecycle-hint').innerText
                };
            }""",
            cancel_copy_payload,
        )
        cancel_requests = get_emitted(page, 'files_copy_cancel_request')
        check(len(cancel_requests) == 1, 'Cancel copy did not emit one typed cancellation request')
        check(cancel_requests[0]['args'][0]['copy_id'] == 'filesc_cancel_test', 'Cancel copy lost the backend copy id')
        check(cancel_pending_ui['runningText'] == text('cancel_copy'), 'running Files copy did not expose cancellation')
        check(cancel_pending_ui['pendingText'] == text('cancelling') and cancel_pending_ui['pendingDisabled'] is True, 'Files copy cancellation could be submitted twice')
        check(cancel_pending_ui['lifecycle'] == text('cancelling_copy'), 'Files copy did not explain pending cancellation')
        cancelled_ui = page.evaluate(
            """payload => {
                window.terminalTest.handleFilesCopyResultForTest({
                    request_id: payload.request_id,
                    copy_id: 'filesc_cancel_test',
                    status: 'cancelled',
                    revision: 1,
                    source_size: 9,
                    bytes_copied: 2,
                    total_bytes: 9,
                    destination_path: '/home/local/reference.txt',
                    message: 'File copy cancelled before publishing.'
                });
                const pipDocument = documentPictureInPicture.window.document;
                const result = {
                    buttonText: pipDocument.querySelector('.sftp-copy-cancel').innerText,
                    status: pipDocument.querySelector('.sftp-destination-pane .sftp-transfer-status').innerText,
                    lifecycle: pipDocument.querySelector('.sftp-copy-lifecycle-hint').innerText
                };
                pipDocument.querySelector('.sftp-copy-cancel').click();
                result.closed = !pipDocument.querySelector('.sftp-destination-pane');
                return result;
            }""",
            cancel_copy_payload,
        )
        check(cancelled_ui['buttonText'] == message(page, 'common.close'), 'cancelled Files copy did not restore a close action')
        check('cancelled before publishing' in cancelled_ui['status'], 'Files copy did not show the cancellation boundary')
        check(cancelled_ui['lifecycle'] == text('copy_cancelled_lifecycle'), 'Files copy did not explain the terminal cancellation state')
        check(cancelled_ui['closed'] is True, 'cancelled Files copy destination pane did not close')

        clear_emitted(page)
        local_file_picker = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const input = pipDocument.querySelector('.sftp-file-input');
                const transfer = new DataTransfer();
                transfer.items.add(new File(['upload'], 'local-upload.txt', {
                    type: 'text/plain',
                    lastModified: Date.UTC(2026, 8, 2, 2, 20)
                }));
                pipDocument.documentElement.style.height = '360px';
                input.files = transfer.files;
                input.dispatchEvent(new Event('change', { bubbles: true }));
                const card = pipDocument.querySelector('.sftp-selected-file');
                const result = {
                    visible: card.classList.contains('visible'),
                    path: pipDocument.querySelector('.sftp-selected-file-path').innerText,
                    meta: pipDocument.querySelector('.sftp-selected-file-meta').innerText,
                    actions: [...card.querySelectorAll('button')].map(button => button.innerText),
                    actionBottom: card.querySelector('.sftp-file-operation-actions').getBoundingClientRect().bottom,
                    viewportHeight: pipDocument.documentElement.clientHeight
                };
                pipDocument.documentElement.style.height = '';
                pipDocument.querySelector('.sftp-local-copy').click();
                const request = window.terminalTest.getEmitted()
                    .find(item => item.event === 'sftp_browse_request' && item.args[0].terminal_id === 'main');
                window.terminalTest.handleSftpBrowseResultForTest({
                    request_id: request.args[0].request_id,
                    terminal_id: 'main',
                    status: 'ready',
                    path: '/home/tester',
                    endpoint: { user: 'tester', host: 'host.example', port: 22, route: 'direct' },
                    directories: [],
                    files: [],
                    truncated: false,
                    max_upload_bytes: 1024
                });
                result.sessions = [...pipDocument.querySelector('.sftp-session-select').options]
                    .map(option => option.value);
                result.sourcePath = pipDocument.querySelector('.sftp-copy-source-path').innerText;
                return result;
            }"""
        )
        check(local_file_picker['visible'] is True, 'selected local file card was not shown')
        check(local_file_picker['path'] == 'local-upload.txt', 'selected local file card omitted the browser file name')
        check(text('detailed_bytes', {'size':'6 B','bytes':6}) in local_file_picker['meta'] and '2026' in local_file_picker['meta'], 'selected local file card omitted exact metadata')
        check(local_file_picker['actions'] == [text('send'), text('copy_to')], 'selected local file card actions were unclear')
        check(local_file_picker['actionBottom'] <= local_file_picker['viewportHeight'],
              'short Files window clipped the selected local file actions')
        check(local_file_picker['sessions'] == ['main', 'term-2'], 'local file Copy to omitted an eligible Files destination')
        check(local_file_picker['sourcePath'] == 'local-upload.txt', 'local file destination pane omitted its source card')
        clear_emitted(page)
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-copy-confirm').click()")
        local_upload_requests = get_emitted(page, 'sftp_upload_ticket_request')
        check(len(local_upload_requests) == 1, 'local file Copy to did not request an upload ticket')
        local_upload_payload = local_upload_requests[0]['args'][0]
        check(local_upload_payload['terminal_id'] == 'main', 'local file Copy to targeted the wrong Files session')
        check(local_upload_payload['directory'] == '/home/tester', 'local file Copy to lost the destination folder')
        check(local_upload_payload['filename'] == 'local-upload.txt', 'local file Copy to lost the destination name')
        page.evaluate(
            """payload => {
                window.terminalTest.handleSftpUploadTicketResultForTest({
                    request_id: payload.request_id,
                    terminal_id: payload.terminal_id,
                    status: 'failed',
                    message: 'Test stopped before upload.'
                });
                documentPictureInPicture.window.document.querySelector('.sftp-copy-cancel').click();
            }""",
            local_upload_payload,
        )

        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-file-rename').click()")
        rename_initial = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                return {
                    confirmDisabled: pipDocument.querySelector('.sftp-rename-confirm').disabled,
                    hint: pipDocument.querySelector('.sftp-rename-hint').innerText,
                    inputFocused: pipDocument.activeElement === pipDocument.querySelector('.sftp-rename-input')
                };
            }"""
        )
        check(rename_initial['confirmDisabled'] is True, 'Rename allowed the unchanged file name')
        check(rename_initial['hint'] == text('filename_exists'), 'Rename did not explain the duplicate name')
        check(rename_initial['inputFocused'] is True, 'Rename did not focus the file name input')
        rename_duplicate = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const input = pipDocument.querySelector('.sftp-rename-input');
                input.value = 'existing.txt';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                return pipDocument.querySelector('.sftp-rename-confirm').disabled;
            }"""
        )
        check(rename_duplicate is True, 'Rename allowed another existing file name')
        rename_ready = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const input = pipDocument.querySelector('.sftp-rename-input');
                input.value = 'renamed.txt';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                return pipDocument.querySelector('.sftp-rename-confirm').disabled;
            }"""
        )
        check(rename_ready is False, 'Rename kept a unique file name disabled')
        page.evaluate(
            """() => documentPictureInPicture.window.document.querySelector('.sftp-rename-input')
                .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))"""
        )
        rename_cancelled = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                return {
                    controlsVisible: pipDocument.querySelector('.sftp-rename-controls').classList.contains('visible'),
                    renameFocused: pipDocument.activeElement === pipDocument.querySelector('.sftp-file-rename')
                };
            }"""
        )
        check(rename_cancelled == {'controlsVisible': False, 'renameFocused': True}, 'Rename Escape did not return to file actions')

        clear_emitted(page)
        page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                pipDocument.querySelector('.sftp-file-rename').click();
                const input = pipDocument.querySelector('.sftp-rename-input');
                input.value = 'renamed.txt';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                pipDocument.querySelector('.sftp-rename-confirm').click();
            }"""
        )
        rename_requests = get_emitted(page, 'sftp_file_action_request')
        check(len(rename_requests) == 1, 'SFTP Rename did not emit one action request')
        rename_payload = rename_requests[0]['args'][0]
        check(rename_payload['file_id'] == 'sftpf_random_a', 'SFTP Rename did not use the backend file ID')
        check(rename_payload['new_filename'] == 'renamed.txt', 'SFTP Rename lost the new file name')
        check('filename' not in rename_payload and 'directory' not in rename_payload, 'SFTP Rename used the old display name as control data')
        page.wait_for_function(
            "() => !documentPictureInPicture.window.document.querySelector('.sftp-rename-confirm').disabled",
            timeout=5000,
        )

        page.evaluate(
            """() => {
                window.terminalTest.renderSftpEntriesForTest({
                    path: '/home/tester',
                    files: [{ file_id: 'sftpf_random_a', name: 'reference.txt', size: 9, mtime: 25 }]
                });
                const pipDocument = documentPictureInPicture.window.document;
                pipDocument.querySelector('.sftp-file-entry').click();
                pipDocument.querySelector('.sftp-file-delete').click();
            }"""
        )
        first_delete = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const yes = pipDocument.querySelector('.sftp-delete-yes').getBoundingClientRect();
                return {
                    question: pipDocument.querySelector('.sftp-delete-phase-one .sftp-delete-question').innerText,
                    path: pipDocument.querySelector('.sftp-delete-path').innerText,
                    noFocused: pipDocument.activeElement === pipDocument.querySelector('.sftp-delete-no'),
                    secondPhaseVisible: pipDocument.querySelector('.sftp-delete-actions.phase-two').getClientRects().length > 0,
                    yesCenterX: yes.left + yes.width / 2
                };
            }"""
        )
        check(first_delete['question'] == text('delete_question'), 'first delete warning was unclear')
        check(first_delete['path'] == '/home/tester/reference.txt', 'delete warning did not show the full remote path')
        check(first_delete['noFocused'] is True, 'first delete warning did not focus No')
        check(first_delete['secondPhaseVisible'] is False, 'second delete actions were visible during the first phase')
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-delete-yes').click()")
        second_delete = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const sure = pipDocument.querySelector('.sftp-delete-sure').getBoundingClientRect();
                const dont = pipDocument.querySelector('.sftp-delete-dont').getBoundingClientRect();
                return {
                    question: pipDocument.querySelector('.sftp-delete-phase-two .sftp-delete-question').innerText,
                    dontFocused: pipDocument.activeElement === pipDocument.querySelector('.sftp-delete-dont'),
                    firstPhaseVisible: pipDocument.querySelector('.sftp-delete-actions.phase-one').getClientRects().length > 0,
                    sure: { left: sure.left, right: sure.right },
                    dont: { left: dont.left, right: dont.right }
                };
            }"""
        )
        check(second_delete['question'] == text('delete_final'), 'second delete warning did not state permanent loss')
        check(second_delete['dontFocused'] is True, 'second delete warning did not focus the safe action')
        check(second_delete['firstPhaseVisible'] is False, 'first delete actions were visible during the second phase')
        original_x = first_delete['yesCenterX']
        check(
            not (second_delete['sure']['left'] <= original_x <= second_delete['sure']['right'])
            and not (second_delete['dont']['left'] <= original_x <= second_delete['dont']['right']),
            'second delete actions overlapped the first Yes click position',
        )

        clear_emitted(page)
        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.sftp-delete-sure').click()")
        delete_requests = get_emitted(page, 'sftp_file_action_request')
        check(len(delete_requests) == 1, 'SFTP Delete did not emit one action request')
        delete_payload = delete_requests[0]['args'][0]
        check(delete_payload['file_id'] == 'sftpf_random_a', 'SFTP Delete did not use the backend file ID')
        check(delete_payload['delete_confirmation'] == 'permanent_delete_confirmed', 'SFTP Delete omitted structured confirmation')
        check('filename' not in delete_payload and 'directory' not in delete_payload, 'SFTP Delete used display names as control data')
        page.evaluate('() => documentPictureInPicture.window.close()')
        page.wait_for_function('() => !window.documentPictureInPicture.window', timeout=5000)

        page.evaluate(
            """() => window.terminalTest.applyTerminalListForTest({
                terminals: [{
                    terminal_id: 'main',
                    connection_type: 'local_shell',
                    terminal_label: 'bash',
                    term: 'xterm-256color',
                    files_available: true,
                    connected: true
                }]
            })"""
        )
        local_menu = page.evaluate("() => window.terminalTest.showContextMenuForTest('main')")
        check(local_menu['sftpVisible'] is True, 'local shell tab did not expose Files')
        check(local_menu['sftpDisabled'] is False, 'Files was disabled for a capable local shell tab')
        check(message(page, 'browser.chrome.files_open') in local_menu['sftpText'], 'local shell Files label was unclear')
        page.click('#sftp-send-option')
        page.wait_for_function('() => !!window.documentPictureInPicture.window', timeout=5000)
        reopened_files = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                return {
                    title: pipDocument.title,
                    destinationOpen: !!pipDocument.querySelector('.sftp-destination-pane'),
                    selectedLocalVisible: pipDocument.querySelector('.sftp-selected-file').classList.contains('visible'),
                    selectedRemoteVisible: pipDocument.querySelector('.sftp-file-operation-box').classList.contains('visible')
                };
            }"""
        )
        check(reopened_files == {
            'title': text('title'),
            'destinationOpen': False,
            'selectedLocalVisible': False,
            'selectedRemoteVisible': False,
        }, 'reopened Files did not reset to its initial source state')
        page.evaluate('() => documentPictureInPicture.window.close()')
        page.wait_for_function('() => !window.documentPictureInPicture.window', timeout=5000)
    finally:
        close_context(context)


def test_restored_terminal_list_allocates_next_new_tab_id(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.evaluate(
            """() => window.terminalTest.applyTerminalListForTest({
                terminals: [
                    {
                        terminal_id: 'main',
                        connection_type: 'local_shell',
                        terminal_label: 'bash',
                        term: 'xterm-256color',
                        connected: true
                    },
                    {
                        terminal_id: 'term-2',
                        connection_type: 'ssh',
                        terminal_label: 'SSH',
                        term: 'xterm-256color',
                        connected: true
                    }
                ]
            })"""
        )
        restored = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        check(
            [tab['id'] for tab in restored['tabs']] == ['main', 'term-2'],
            'restored terminal list did not create the expected initial tab set',
        )
        check(restored['nextTerminalIndex'] >= 3, 'restored terminal list did not advance the tab allocator')

        page.click('#new-tab-btn')
        page.wait_for_function(
            "() => window.terminalTest.getTerminalTabsState().tabs.some(tab => tab.id === 'term-3')",
            timeout=5000,
        )
        state = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        dom = page.evaluate("() => window.terminalTest.getTerminalDomStateForTest()")
        state_ids = [tab['id'] for tab in state['tabs']]
        dom_ids = [tab['id'] for tab in dom['tabDom']]
        check(state_ids == ['main', 'term-2', 'term-3'], f'new tab allocator reused a restored id: {state_ids}')
        check(dom_ids == ['main', 'term-2', 'term-3'], f'tab DOM diverged from terminal state: {dom_ids}')
        check(len(set(dom_ids)) == len(dom_ids), f'tab DOM has duplicate terminal ids: {dom_ids}')
        check(
            sum(1 for pane in dom['panes'] if 'active' in pane['className'].split()) == 1,
            f'terminal panes have inconsistent active state: {dom["panes"]}',
        )

        page.locator('.terminal-tab[data-terminal-id="term-2"] .tab-close').click()
        page.wait_for_function(
            "() => !window.terminalTest.getTerminalTabsState().tabs.some(tab => tab.id === 'term-2')",
            timeout=5000,
        )
        page.click('#new-tab-btn')
        page.wait_for_function(
            "() => window.terminalTest.getTerminalTabsState().tabs.some(tab => tab.id === 'term-4')",
            timeout=5000,
        )
        after_reopen = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        after_reopen_dom = page.evaluate("() => window.terminalTest.getTerminalDomStateForTest()")
        reopened_state_ids = [tab['id'] for tab in after_reopen['tabs']]
        reopened_dom_ids = [tab['id'] for tab in after_reopen_dom['tabDom']]
        check(reopened_state_ids == ['main', 'term-3', 'term-4'], f'reopened tab reused a closed id: {reopened_state_ids}')
        check(reopened_dom_ids == ['main', 'term-3', 'term-4'], f'DOM diverged after close/reopen: {reopened_dom_ids}')
        check(len(set(reopened_dom_ids)) == len(reopened_dom_ids), f'DOM has duplicate ids after close/reopen: {reopened_dom_ids}')
        check(
            sum(1 for pane in after_reopen_dom['panes'] if 'active' in pane['className'].split()) == 1,
            f'terminal panes diverged after close/reopen: {after_reopen_dom["panes"]}',
        )
    finally:
        close_context(context)


def test_operator_observation_warning_ui(browser, access_url):
    from settings_transfer_i18n_browser_smoke import message
    context, page = new_page(browser, access_url)
    try:
        page.click('#agent-toggle-btn')
        page.wait_for_function(
            "() => window.terminalTest.getOperatorObservationState()?.enabled === true",
            timeout=5000,
        )
        page.evaluate("() => document.getElementById('operator-observation-start-btn').click()")
        page.wait_for_function(
            "() => window.terminalTest.getOperatorObservationState()?.active === true",
            timeout=5000,
        )
        ui_state = page.evaluate(
            """() => ({
                body: document.body.classList.contains('operator-observing'),
                panel: document.getElementById('agent-panel').classList.contains('operator-observing'),
                count: window.terminalTest.getOperatorObservationState().eventCount,
                text: document.getElementById('operator-observation-state').innerText
            })"""
        )
        check(ui_state['body'] is True, 'operator observation did not set body warning class')
        check(ui_state['panel'] is True, 'operator observation did not set panel warning class')
        check(ui_state['text'] == message(page, 'browser.chrome.observing', {'count':ui_state['count'] or 0}),
              'operator observation status did not show the localized warning and exact event count')
        page.evaluate("() => document.getElementById('operator-observation-mark-btn').click()")
        page.wait_for_function(
            "() => window.terminalTest.getOperatorObservationState()?.eventCount >= 1",
            timeout=5000,
        )
        page.evaluate("() => document.getElementById('operator-observation-stop-btn').click()")
        page.wait_for_function(
            "() => window.terminalTest.getOperatorObservationState()?.active === false",
            timeout=5000,
        )
        check(
            page.evaluate("() => !document.body.classList.contains('operator-observing')"),
            'operator observation warning class stayed active after stop',
        )
    finally:
        close_context(context)


def set_agent_mode(page, mode, expected_mode):
    emit_socket(page, 'agent_mode_set', {'terminal_id': TERMINAL_ID, 'mode': mode})
    return wait_for_agent(page, f"state.mode === '{expected_mode}'")


def request_agent_action(page, text):
    emit_socket(page, 'agent_suggestion_request', {
        'terminal_id': TERMINAL_ID,
        'mock_input': text,
    })
    state = wait_for_agent(
        page,
        'state.pending_action && state.pending_action.status === "pending_approval"',
    )
    return state['pending_action']


def approval_payload_from_action(action):
    return {
        'terminal_id': TERMINAL_ID,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'session_id': action['session_id'],
        'viewer_id': action['viewer_id'],
        'agent_binding_id': action['agent_binding_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    }


def test_hidden_mirror_ignores_visible_scroll(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        output = ''.join(f'mirror-{index:03d}\\r\\n' for index in range(90))
        page.evaluate(
            """payload => window.terminalTest.writeTerminalOutput(payload.data, payload.output_seq)""",
            {'data': output, 'output_seq': 90},
        )
        page.wait_for_function(
            "() => window.terminalTest.getMirrorSnapshot()?.lines?.join('\\n').includes('mirror-089')",
            timeout=10000,
        )
        before = page.evaluate('() => window.terminalTest.getMirrorSnapshot()')
        page.evaluate('() => window.terminalTest.scrollVisibleTerminal(-60)')
        page.wait_for_timeout(100)
        after = page.evaluate('() => window.terminalTest.getMirrorSnapshot()')
        check(before['lines'] == after['lines'], 'mirror snapshot changed after visible terminal scroll')
        check(before['base_y'] == after['base_y'], 'mirror base_y changed after visible terminal scroll')
        check(after['output_seq'] == 90, 'mirror output_seq did not track injected output')
    finally:
        close_context(context)


def test_privacy_states_block_snapshots_and_agent_runs(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        set_agent_mode(page, 'approval', 'approval_pending')

        set_privacy(page, 'private_input')
        wait_for_agent(page, "state.privacy_state === 'private_input'")
        clear_emitted(page)
        page.evaluate('() => window.terminalTest.sendAgentSnapshot()')
        check(not get_emitted(page, 'agent_viewport_snapshot'), 'private_input allowed a snapshot emit')
        emit_socket(page, 'agent_provider_run_request', {'terminal_id': TERMINAL_ID})
        wait_for_last_action_error(page, 'agent_privacy_blocked')

        set_privacy(page, 'normal')
        wait_for_agent(page, "state.privacy_state === 'normal'")
        page.evaluate("() => window.terminalTest.startPasteReview(':\\n:\\n')")
        wait_for_agent(page, "state.privacy_state === 'paste_review'")
        clear_emitted(page)
        page.evaluate('() => window.terminalTest.sendAgentSnapshot()')
        check(not get_emitted(page, 'agent_viewport_snapshot'), 'paste_review allowed a snapshot emit')
        emit_socket(page, 'agent_provider_run_request', {'terminal_id': TERMINAL_ID})
        wait_for_last_action_error(page, 'agent_privacy_blocked')
        page.evaluate("() => document.getElementById('paste-review-cancel').click()")
        wait_for_agent(page, "state.privacy_state === 'normal'")

        set_privacy(page, 'paused')
        wait_for_agent(page, "state.privacy_state === 'paused' && state.mode === 'paused'")
        clear_emitted(page)
        page.evaluate('() => window.terminalTest.sendAgentSnapshot()')
        check(not get_emitted(page, 'agent_viewport_snapshot'), 'paused allowed a snapshot emit')
        emit_socket(page, 'agent_provider_run_request', {'terminal_id': TERMINAL_ID})
        wait_for_last_action_error(page, 'agent_paused')
    finally:
        close_context(context)


def test_agent_panel_status_gates_and_external_hint(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    context, page = new_page(browser, access_url, ui_language)
    try:
        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible', timeout=5000)
        attach_agent(page)
        set_agent_mode(page, 'approval', 'approval_pending')

        set_privacy(page, 'private_input')
        wait_for_agent(page, "state.privacy_state === 'private_input'")
        emit_socket(page, 'agent_provider_run_request', {'terminal_id': TERMINAL_ID})
        wait_for_last_action_error(page, 'agent_privacy_blocked')
        page.wait_for_function(
            "() => document.getElementById('agent-status-detail').innerText.includes('agent_privacy_blocked')",
            timeout=5000,
        )
        panel_state = page.evaluate(
            """() => ({
                statusBoxError: document.getElementById('agent-status-box').classList.contains('error'),
                statusMain: document.getElementById('agent-status-main').innerText,
                statusDetail: document.getElementById('agent-status-detail').innerText,
                privacyText: document.getElementById('agent-gate-privacy').innerText,
                privacyBlocking: document.getElementById('agent-gate-privacy').classList.contains('blocking')
            })"""
        )
        check(panel_state['statusBoxError'] is True, 'agent status row did not mark action error')
        check('agent_privacy_blocked' in panel_state['statusDetail'], 'agent status row did not show error_code')
        check(panel_state['privacyText'] == message(page, 'browser.chrome.privacy', {'value':'private_input'}), 'privacy gate chip did not show privacy state')
        check(panel_state['privacyBlocking'] is True, 'privacy gate chip did not mark blocking state')

        set_privacy(page, 'normal')
        wait_for_agent(page, "state.privacy_state === 'normal'")
        emit_socket(page, 'ssh_input', {'terminal_id': TERMINAL_ID, 'data': 'x'})
        wait_for_agent(page, 'state.human_input_lease_active === true')
        human_gate = page.evaluate(
            """() => ({
                text: document.getElementById('agent-gate-human').innerText,
                blocking: document.getElementById('agent-gate-human').classList.contains('blocking')
            })"""
        )
        human_pattern = re.escape(message(page, 'browser.chrome.human_seconds', {'seconds':123456})).replace('123456', r'\d+')
        check(human_gate['text'] == message(page, 'browser.chrome.human_locked') or re.fullmatch(human_pattern, human_gate['text']),
              'human input gate chip did not show active lease')
        check(human_gate['blocking'] is True, 'human input gate chip did not mark blocking state')

        emit_socket(page, 'agent_mode_set', {'terminal_id': TERMINAL_ID, 'mode': 'disabled'})
        wait_for_agent(page, "state.mode === 'disabled'")
        disabled_external = page.evaluate(
            """() => ({
                buttonDisabled: document.getElementById('agent-external-token-btn').disabled,
                hint: document.getElementById('agent-external-hint').innerText,
                commandTag: document.getElementById('agent-external-command').tagName,
                commandOutputOpen: document.getElementById('agent-external-output').open,
                accessText: document.getElementById('agent-access-toggle-btn').innerText,
                modeButtonsDisabled: Array.from(document.querySelectorAll('[data-agent-mode]')).every(button => button.disabled)
            })"""
        )
        check(disabled_external['accessText'] == message(page, 'agent.access.enable'), 'agent access toggle did not offer enable in disabled mode')
        check(disabled_external['modeButtonsDisabled'] is True, 'agent permission buttons were not disabled while access was off')
        check(disabled_external['buttonDisabled'] is True, 'external token button stayed enabled in disabled mode')
        check(disabled_external['hint'] == message(page, 'agent.token.enable_required'), 'external token hint did not explain disabled prerequisite')
        check(disabled_external['commandTag'] == 'TEXTAREA', 'external token command output is not a textarea')
        check(disabled_external['commandOutputOpen'] is False, 'external token command output was not collapsed by default')

        page.click('#agent-access-toggle-btn')
        wait_for_agent(page, "state.mode === 'observe'")
        enabled_external = page.evaluate(
            """() => ({
                buttonDisabled: document.getElementById('agent-external-token-btn').disabled,
                hint: document.getElementById('agent-external-hint').innerText,
                accessText: document.getElementById('agent-access-toggle-btn').innerText,
                modeLabels: Array.from(document.querySelectorAll('[data-agent-mode]')).map(button => button.innerText)
            })"""
        )
        check(enabled_external['accessText'] == message(page, 'agent.access.disable'), 'agent access toggle did not offer disable after enabling')
        expected_labels = [message(page, button.get_attribute('data-i18n')) for button in page.locator('[data-agent-mode]').all()]
        check(enabled_external['modeLabels'] == expected_labels, 'agent permission buttons did not use user-facing labels')
        check(enabled_external['buttonDisabled'] is False, 'external token button did not enable in observe mode')
        check(enabled_external['hint'] == message(page, 'agent.token.local_hint'), 'external token hint did not show available state')

        panel_mint_state = page.evaluate(
            """() => ({
                panel3xDisabled: document.getElementById('agent-external-token-3x-btn').disabled,
                statusMintVisible: document.getElementById('agent-status-mint-btn').classList.contains('visible'),
                statusMint3xVisible: document.getElementById('agent-status-mint-3x-btn').classList.contains('visible')
            })"""
        )
        check(panel_mint_state['panel3xDisabled'] is False, 'Agent panel 3x mint button did not enable')
        check(panel_mint_state['statusMintVisible'] is False, 'status mint button stayed visible while Agent panel was open')
        check(panel_mint_state['statusMint3xVisible'] is False, 'status 3x mint button stayed visible while Agent panel was open')

        page.click('#agent-panel-close-btn')
        page.wait_for_function(
            """() => (
                document.getElementById('agent-status-mint-btn').classList.contains('visible')
                && document.getElementById('agent-status-mint-3x-btn').classList.contains('visible')
            )""",
            timeout=5000,
        )
        page.click('#agent-status-mint-3x-btn')
        page.wait_for_function(
            """() => {
                const token = window.terminalTest.getActiveAgentState()?.external_token;
                return token && (token.status === 'active' || token.status === 'error');
            }""",
            timeout=5000,
        )
        status_minted = page.evaluate(
            """() => {
                const token = window.terminalTest.getActiveAgentState()?.external_token;
                return {
                    status: token?.status,
                    remainingMs: Number(token?.expiresAt || 0) - Date.now(),
                    idleTimeoutMultiplier: token?.idleTimeoutMultiplier,
                    panelVisible: document.getElementById('agent-panel').classList.contains('visible')
                };
            }"""
        )
        check(status_minted['status'] == 'active', 'status-bar 3x mint did not complete')
        check(status_minted['idleTimeoutMultiplier'] == 3, 'status-bar 3x mint did not retain the structured multiplier')
        check(status_minted['remainingMs'] > 10 * 60 * 1000, 'status-bar 3x mint did not extend the idle lifetime')
        check(status_minted['panelVisible'] is False, 'status-bar mint unexpectedly opened the Agent panel')
        check(page.locator('#agent-pause-btn + #agent-status-mint-btn + #agent-status-mint-3x-btn').count() == 1,
              'Mint actions are not adjacent to Pause Agent in the tab row')

        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible', timeout=5000)
        minted_command = page.evaluate("() => document.getElementById('agent-external-command').value")
        check('# terminal handoff:' in minted_command, '3x mint did not expose the stable terminal handoff path')
    finally:
        close_context(context)


def test_external_token_tab_indicator_tracks_background_lifecycle(browser, access_url):
    context, page = new_page(browser, access_url)
    main_tab = '.terminal-tab[data-terminal-id="main"]'
    try:
        attach_agent(page)
        check(page.locator(main_tab + '.agent-token-active').count() == 0,
              'Enabled access without a minted token acquired the turquoise indicator')
        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible')
        page.click('#agent-external-token-btn')
        page.wait_for_selector(main_tab + '.agent-token-active', state='attached')
        page.click('#new-tab-btn')
        check(page.locator(main_tab + '.active').count() == 0, 'Fixture did not switch tabs')
        check(page.locator(main_tab + '.agent-token-active').count() == 1,
              'Background tab lost its minted-token indicator')
        check(page.locator('.terminal-tab.active.agent-token-active').count() == 0,
              'New tab inherited the other terminal token indicator')
        check('Agent token active' in page.locator(main_tab).get_attribute('title'),
              'Active token has no text explanation')
        color = lambda: page.locator(main_tab + ' .tab-state').evaluate(
            'element => getComputedStyle(element).backgroundColor')
        check(color() == 'rgb(64, 224, 208)', 'Active token light is not turquoise')
        check(re.fullmatch(r'\(\d+\)', page.locator(main_tab + ' .tab-agent-countdown').inner_text()),
              'Active token has no remaining-seconds label')
        # Control wall time for synthetic expiry; leave the real refresh timer running.
        page.evaluate('() => { window.tabTokenTestNow = Date.now(); Date.now = () => window.tabTokenTestNow; }')

        def token_event(status, remaining_ms):
            page.evaluate('payload => window.terminalTest.applyAgentExternalTokenStateForTest(payload)', {
                'terminal_id': TERMINAL_ID, 'token_status': status,
                'external_agent_token': {'remaining_idle_ms': remaining_ms},
            })

        # No active-panel countdown: only the shared clock can dim this background tab.
        token_event('active', 1000)
        page.evaluate('() => { window.tabTokenTestNow += 1001; }')
        page.wait_for_selector(main_tab + '.agent-token-expired', state='attached', timeout=3000)
        check(color() == 'rgb(71, 115, 110)', 'Expired token light is not dim turquoise')
        check('expired' in page.locator(main_tab).get_attribute('title'), 'Expiry tooltip is missing')
        check(page.locator(main_tab + ' .tab-agent-countdown').is_hidden(),
              'Expired background tab retained a countdown')
        check(page.locator(main_tab + '.agent-token-active').count() == 0, 'Expired token stayed bright')
        token_event('attached', 60000)
        page.wait_for_selector(main_tab + '.agent-token-active', state='attached')
        check(page.locator(main_tab + ' .tab-agent-countdown').inner_text() == '(60)',
              'Token activity did not refresh the background countdown')
        page.evaluate('() => { window.tabTokenTestNow += 1100; }')
        page.wait_for_function("""() => document.querySelector(
            '.terminal-tab[data-terminal-id="main"] .tab-agent-countdown').innerText === '(59)'""", timeout=3000)
        token_event('revoked', 60000)
        check(page.locator(main_tab + '.agent-token-active').count() == 0, 'Revoked token stayed bright')
        check(page.locator(main_tab + '.agent-token-expired').count() == 0, 'Revocation was shown as expiry')
        check('Agent token' not in page.locator(main_tab).get_attribute('title'),
              'Revoked token tooltip remained stale')
        check(page.locator(main_tab + ' .tab-agent-countdown').is_hidden(),
              'Revoked token retained a countdown label')
        token_event('active', 60000)
        token_event('invalidated', 60000)
        check(page.locator(main_tab + '.agent-token-active').count() == 0, 'Invalidated token stayed bright')
        token_event('active', 60000)
        emit_socket(page, 'agent_mode_set', {'terminal_id': TERMINAL_ID, 'mode': 'disabled'})
        page.wait_for_function("() => window.terminalTest.getAgentStateForTest('main').mode === 'disabled'")
        check(page.locator(main_tab + '.agent-token-active').count() == 0, 'Disabled access stayed bright')
        check(color() == 'rgb(52, 199, 89)', 'Disabled access lost the normal connected light')
        page.evaluate('id => window.terminalTest.switchTerminalForTest(id)', TERMINAL_ID)
        set_agent_mode(page, 'direct', 'direct_active')
        page.set_viewport_size({'width': 640, 'height': 600})
        for selector in ['#new-tab-btn', '#agent-pause-btn', '#agent-access-mint-btn',
                         '#agent-toggle-btn', '#quick-settings']:
            bounds = page.locator(selector).bounding_box()
            check(bounds is not None and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 640,
                  f'{selector} is clipped beside the compact tab row')
        check(page.locator('#agent-status-mint-btn').is_hidden(),
              'compact tab row did not hide the redundant standard Mint action')
        check(page.locator('#agent-status-mint-3x-btn').is_hidden(),
              'compact tab row did not move the 3x Mint action into the Agent panel')
        page.click('#agent-access-mint-btn')
        page.wait_for_selector(main_tab + '.agent-token-active', state='attached')
        page.click('#agent-pause-btn')
        wait_for_agent(page, "state.mode === 'paused'")
        check(page.locator(main_tab + '.agent-token-active').count() == 0, 'Paused access stayed bright')
        check(page.locator(main_tab + ' .tab-agent-countdown').is_hidden(), 'Paused token retained a countdown')
    finally:
        close_context(context)


def test_session_recovery_new_tab_can_renew_external_agent_token(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible', timeout=5000)
        page.evaluate("() => document.getElementById('agent-external-token-btn').click()")
        page.wait_for_function(
            "() => window.terminalTest.getActiveAgentState()?.external_token?.status === 'active'",
            timeout=5000,
        )
        check(
            '--token' in page.evaluate("() => document.getElementById('agent-external-command').value"),
            'initial external token command did not render after structured token state became active',
        )

        page.evaluate("() => window.terminalTest.showSessionRecoveryForTest()")
        page.click('#session-recovery-remembered-token')
        page.wait_for_function(
            """() => {
                const socket = window.terminalTest.getSocketState();
                return !document.getElementById('session-recovery-modal').classList.contains('open')
                    && socket.connected === true
                    && socket.serverConnectionState === 'available';
            }""",
            timeout=10000,
        )
        page.wait_for_function(
            "() => window.terminalTest.getTerminalTabsState().tabs.length === 1",
            timeout=5000,
        )

        page.evaluate("() => document.getElementById('new-tab-btn').click()")
        page.evaluate("() => document.getElementById('connectBtn').click()")
        page.wait_for_function(
            "() => window.terminalTest.getActiveAgentState()?.connected === true",
            timeout=10000,
        )
        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible', timeout=5000)
        page.click('#agent-access-toggle-btn')
        wait_for_agent(page, "state.mode === 'observe'")
        page.evaluate(
            "() => { document.getElementById('agent-external-command').value = 'stale display command'; }"
        )
        page.evaluate(
            """() => window.terminalTest.emitSocket('agent_mode_set', {
                terminal_id: window.terminalTest.getActiveAgentState().terminal_id,
                mode: 'approval_pending'
            })"""
        )
        wait_for_agent(page, "state.mode === 'approval_pending' && state.external_token === null")
        recovered_token_ui = page.evaluate(
            """() => ({
                buttonText: document.getElementById('agent-external-token-btn').innerText,
                command: document.getElementById('agent-external-command').value
            })"""
        )
        check(recovered_token_ui['buttonText'] == 'Create token', 'new terminal reused stale external token command state')
        check(recovered_token_ui['command'] == '', 'new terminal kept stale external token command text')
        page.evaluate("() => document.getElementById('agent-external-token-btn').click()")
        page.wait_for_function(
            """() => {
                const token = window.terminalTest.getActiveAgentState()?.external_token;
                return token && (token.status === 'active' || token.status === 'error');
            }""",
            timeout=5000,
        )
        command = page.evaluate("() => document.getElementById('agent-external-command').value")
        state_after_token = active_agent_state(page)
        token_state = state_after_token['external_token']
        check(not command.startswith('error:'), f'external token renew after session recovery failed: {command}')
        check('--terminal' in command and '--token' in command, 'external token renew did not produce a CLI command')
        check(token_state['terminalId'] == state_after_token['terminal_id'], 'external token state was not bound to the active terminal')
        check(token_state['status'] == 'active', 'external token state did not record active status')
    finally:
        close_context(context)


def test_rendered_viewport_snapshot_returns_png(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        page.evaluate("() => window.terminalTest.applyColorScheme('oneHalfLight')")
        page.evaluate(
            """payload => window.terminalTest.writeTerminalOutput(payload.data, payload.output_seq)""",
            {'data': 'rendered-viewport-check\\r\\n', 'output_seq': 321},
        )
        page.wait_for_function(
            "() => window.terminalTest.getMirrorSnapshot()?.output_seq === 321",
            timeout=10000,
        )
        result = page.evaluate(
            """async () => await window.terminalTest.buildViewportRenderResult({
                request_id: 'render-test-1',
                terminal_id: 'main',
                render_mode: 'visible_xterm_png'
            })"""
        )
        check(result['status'] == 'ok', f"render result failed: {result}")
        check(result['request_id'] == 'render-test-1', 'render result used the wrong request id')
        check(result['render_type'] == 'xterm_viewport', 'render result used the wrong render type')
        check(result['render_mode'] == 'visible_xterm_png', 'render result used the wrong render mode')
        check(result['mime_type'] == 'image/png', 'render result used the wrong MIME type')
        check(result['source'] == 'visible_xterm_dom', 'foreground render did not use the visible xterm DOM')
        check(result['image_base64'].startswith('iVBORw0KGgo'), 'render result is not a PNG')
        check(result['pixel_width'] > 0 and result['pixel_height'] > 0, 'render result has invalid dimensions')
        check(result['cols'] > 0 and result['rows'] > 0, 'render result has invalid terminal size')
        check(result['output_seq'] == 321, 'render result did not preserve output_seq')
        background_pixel = page.evaluate(
            """async payload => {
                const image = new Image();
                const loaded = new Promise((resolve, reject) => {
                    image.onload = resolve;
                    image.onerror = () => reject(new Error('png decode failed'));
                });
                image.src = `data:image/png;base64,${payload.image_base64}`;
                await loaded;
                const canvas = document.createElement('canvas');
                canvas.width = image.width;
                canvas.height = image.height;
                const context = canvas.getContext('2d');
                context.drawImage(image, 0, 0);
                const x = Math.max(0, image.width - 2);
                const y = Math.max(0, image.height - 2);
                return Array.from(context.getImageData(x, y, 1, 1).data);
            }""",
            result,
        )
        check(background_pixel[3] == 255, 'rendered PNG background is transparent')
        check(
            all(channel >= 245 for channel in background_pixel[:3]),
            f'rendered PNG background does not match light xterm theme: {background_pixel}',
        )
    finally:
        close_context(context)


def test_background_terminal_render_uses_mirror_canvas_png(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        page.evaluate("() => window.terminalTest.applyColorScheme('oneHalfLight')")
        page.evaluate(
            """payload => window.terminalTest.writeTerminalOutput(payload.data, payload.output_seq)""",
            {
                'data': (
                    '\x1b[2J\x1b[H\x1b[38;5;45m'
                    '████████\r\n████████\r\n████████\r\n'
                    '\x1b[38;5;226;48;5;33m▄▄▄▄▄▄▄▄\r\n▀▀▀▀▀▀▀▀\x1b[0m\r\n'
                    '\x1b[38;2;215;135;135m ▐▛███▛█\r\n▝▜██████▀\x1b[0m'
                ),
                'output_seq': 322,
            },
        )
        page.wait_for_function(
            "() => window.terminalTest.getMirrorSnapshot()?.output_seq === 322",
            timeout=10000,
        )
        page.click('#new-tab-btn')
        page.wait_for_function(
            "() => window.terminalTest.getTerminalTabsState().activeTerminalId !== 'main'",
            timeout=5000,
        )
        result = page.evaluate(
            """async () => await window.terminalTest.buildViewportRenderResultForTerminal('main', {
                request_id: 'render-background-test-1',
                terminal_id: 'main',
                render_mode: 'visible_xterm_png'
            })"""
        )
        check(result['status'] == 'ok', f"background render result failed: {result}")
        check(result['source'] == 'terminal_mirror_canvas', 'background render did not use the mirror canvas')
        check(result['image_base64'].startswith('iVBORw0KGgo'), 'background render result is not a PNG')
        check(result['pixel_width'] > 1 and result['pixel_height'] > 1, 'background render returned a degenerate PNG')
        check(result['cols'] > 0 and result['rows'] > 0, 'background render has invalid terminal size')
        check(result['output_seq'] == 322, 'background render did not preserve output_seq')
        decoded = page.evaluate(
            """async payload => {
                const image = new Image();
                const loaded = new Promise((resolve, reject) => {
                    image.onload = resolve;
                    image.onerror = () => reject(new Error('png decode failed'));
                });
                image.src = `data:image/png;base64,${payload.image_base64}`;
                await loaded;
                const canvas = document.createElement('canvas');
                canvas.width = image.width;
                canvas.height = image.height;
                const context = canvas.getContext('2d');
                context.drawImage(image, 0, 0);
                const pixels = context.getImageData(0, 0, image.width, image.height).data;
                let nonBackgroundPixels = 0;
                let coralPixelCount = 0;
                let partialCoralPixels = 0;
                const cyanRows = [];
                const yellowRows = [];
                const cyanColumns = [];
                for (let index = 0; index < pixels.length; index += 4) {
                    if (pixels[index] < 245 || pixels[index + 1] < 245 || pixels[index + 2] < 245) {
                        nonBackgroundPixels += 1;
                    }
                }
                for (let y = 0; y < image.height; y += 1) {
                    let cyanCount = 0;
                    let yellowCount = 0;
                    for (let x = 0; x < image.width; x += 1) {
                        const offset = (y * image.width + x) * 4;
                        const red = pixels[offset];
                        const green = pixels[offset + 1];
                        const blue = pixels[offset + 2];
                        if (red < 40 && green >= 190 && blue >= 220) cyanCount += 1;
                        if (red >= 220 && green >= 220 && blue < 40) yellowCount += 1;
                        const coralLike = red > 40 && green > 20 && blue > 20
                            && red > green && Math.abs(green - blue) <= 2;
                        const exactCoral = Math.abs(red - 215) <= 2
                            && Math.abs(green - 135) <= 2 && Math.abs(blue - 135) <= 2;
                        if (coralLike) coralPixelCount += 1;
                        if (coralLike && !exactCoral) partialCoralPixels += 1;
                    }
                    if (cyanCount >= 20) cyanRows.push(y);
                    if (yellowCount >= 20) yellowRows.push(y);
                }
                for (let x = 0; x < image.width; x += 1) {
                    let cyanCount = 0;
                    for (let y = 0; y < image.height; y += 1) {
                        const offset = (y * image.width + x) * 4;
                        const red = pixels[offset];
                        const green = pixels[offset + 1];
                        const blue = pixels[offset + 2];
                        if (red < 40 && green >= 190 && blue >= 220) cyanCount += 1;
                    }
                    if (cyanCount >= 10) cyanColumns.push(x);
                }
                const maxStep = values => values.reduce(
                    (largest, value, index) => index === 0
                        ? largest
                        : Math.max(largest, value - values[index - 1]),
                    0
                );
                return {
                    width: image.width,
                    height: image.height,
                    nonBackgroundPixels,
                    coralPixelCount,
                    partialCoralPixels,
                    cyanRowCount: cyanRows.length,
                    cyanRowMaxStep: maxStep(cyanRows),
                    cyanColumnCount: cyanColumns.length,
                    cyanColumnMaxStep: maxStep(cyanColumns),
                    yellowRowCount: yellowRows.length,
                    yellowRowMaxStep: maxStep(yellowRows)
                };
            }""",
            result,
        )
        check(decoded['width'] == result['pixel_width'], 'background PNG width metadata does not match the image')
        check(decoded['height'] == result['pixel_height'], 'background PNG height metadata does not match the image')
        check(decoded['nonBackgroundPixels'] > 0, 'background PNG did not contain terminal glyphs')
        check(decoded['cyanRowCount'] > 20, 'background PNG did not render enough full-block rows')
        check(decoded['cyanRowMaxStep'] == 1, 'background PNG retained horizontal full-block seams')
        check(decoded['cyanColumnCount'] > 20, 'background PNG did not render enough full-block columns')
        check(decoded['cyanColumnMaxStep'] == 1, 'background PNG retained vertical full-block seams')
        check(decoded['yellowRowCount'] > 8, 'background PNG did not render enough half-block rows')
        check(decoded['yellowRowMaxStep'] == 1, 'background PNG retained a half-block boundary seam')
        check(decoded['coralPixelCount'] > 20, 'background PNG did not render enough quadrant pixels')
        check(decoded['partialCoralPixels'] == 0, 'background PNG retained blended quadrant seams')
    finally:
        close_context(context)


def test_paste_review_approve_and_cancel(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language)
    try:
        attach_agent(page)

        clear_emitted(page)
        page.evaluate("() => window.terminalTest.blurActiveTerminalForTest()")
        page.evaluate("() => window.terminalTest.startPasteReview(':')")
        page.wait_for_function("() => window.terminalTest.activeTerminalHasFocus()", timeout=5000)
        short_paste_inputs = get_emitted(page, 'ssh_input')
        check(len(short_paste_inputs) == 1, 'short paste did not emit exactly one ssh_input')
        check(short_paste_inputs[0]['args'][0]['data'] == ':', 'short paste used the wrong payload')

        clear_emitted(page)
        page.evaluate("() => window.terminalTest.blurActiveTerminalForTest()")
        page.evaluate("() => window.terminalTest.startPasteReview(':\\n:\\n')")
        wait_for_agent(page, "state.privacy_state === 'paste_review'")
        page.evaluate("() => document.getElementById('paste-review-cancel').click()")
        wait_for_agent(page, "state.privacy_state === 'normal'")
        page.wait_for_function("() => window.terminalTest.activeTerminalHasFocus()", timeout=5000)
        check(not get_emitted(page, 'ssh_input'), 'paste review cancel emitted ssh_input')

        clear_emitted(page)
        page.evaluate("() => window.terminalTest.blurActiveTerminalForTest()")
        page.evaluate("() => window.terminalTest.startPasteReview(':\\n:\\n')")
        wait_for_agent(page, "state.privacy_state === 'paste_review'")
        page.evaluate("() => document.getElementById('paste-review-approve').click()")
        wait_for_agent(page, "state.privacy_state === 'normal'")
        page.wait_for_function("() => window.terminalTest.activeTerminalHasFocus()", timeout=5000)
        ssh_inputs = get_emitted(page, 'ssh_input')
        check(len(ssh_inputs) == 1, 'paste review approve did not emit exactly one ssh_input')
        payload = ssh_inputs[0]['args'][0]
        check(payload['terminal_id'] == TERMINAL_ID, 'paste review ssh_input used the wrong terminal')
        check(payload['data'] == ':\n:\n', 'paste review ssh_input used the wrong payload')
    finally:
        close_context(context)


def test_clipboard_paste_targets_and_native_review(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language)
    try:
        attach_agent(page)
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        page.evaluate("""() => {
            Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
                readText: () => new Promise(resolve => { window.resolveFixturePaste = resolve; })
            }});
        }""")

        def begin():
            page.evaluate("document.querySelector('.terminal-pane.active .xterm-helper-textarea').focus()")
            page.evaluate("document.getElementById('paste-option').click()")
            return page.evaluate('window.standtermUi.contextPasteRequest()')

        def finish():
            page.evaluate("window.resolveFixturePaste('')")

        clear_emitted(page)
        request = begin()
        check(bool(request), 'context paste did not capture a target')
        check(page.evaluate("id => window.standtermUi.completeContextPaste(id, ':')", request), 'context paste failed')
        check(not page.evaluate("id => window.standtermUi.completeContextPaste(id, ':')", request), 'context paste was reusable')
        finish()
        check(len(get_emitted(page, 'ssh_input')) == 1, 'context paste did not send exactly once')
        page.wait_for_function('window.terminalTest.activeTerminalHasFocus()')

        # Web readText uses the same original-state guard when its promise is delayed.
        clear_emitted(page)
        begin()
        page.evaluate("document.getElementById('new-tab-btn').focus()")
        page.evaluate("window.resolveFixturePaste(':')")
        check(not get_emitted(page, 'ssh_input'), 'delayed web paste ignored a changed target')

        for mode in ['modal', 'focus', 'tab']:
            clear_emitted(page)
            request = begin()
            check(bool(request), f'{mode} paste did not capture a target')
            if mode == 'modal':
                page.evaluate("document.getElementById('settings-modal').classList.add('open')")
            elif mode == 'focus':
                page.evaluate("document.getElementById('new-tab-btn').focus()")
            else:
                page.evaluate("document.getElementById('new-tab-btn').click()")
                page.wait_for_function("window.standtermUi.snapshot().terminalId !== 'main'")
            check(not page.evaluate("id => window.standtermUi.completeContextPaste(id, ':')", request), f'{mode} change accepted stale paste')
            finish()
            check(not get_emitted(page, 'ssh_input'), f'{mode} change sent input')
            page.evaluate("document.getElementById('settings-modal').classList.remove('open')")
            if mode == 'tab':
                page.locator('.terminal-tab[data-terminal-id="main"]').click()

        clear_emitted(page)
        # Synthetic clipboard events exercise real xterm listeners without OS clipboard access.
        page.evaluate("window.terminalTest.writeTerminalOutput('\\x1b[?2004l')")
        page.wait_for_timeout(100)
        for approve in [False, True]:
            page.evaluate("""() => {
                const data = new DataTransfer(); data.setData('text/plain', ':\\n:');
                document.querySelector('.terminal-pane.active .xterm-helper-textarea').dispatchEvent(
                    new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
            }""")
            check(page.locator('#paste-review-modal').evaluate("el => el.classList.contains('open')"), 'native two-line paste skipped review')
            check(not get_emitted(page, 'ssh_input'), 'native paste sent input before review')
            page.evaluate(f"document.getElementById('paste-review-{'approve' if approve else 'cancel'}').click()")
        inputs = get_emitted(page, 'ssh_input')
        check(len(inputs) == 1 and inputs[0]['args'][0]['data'] == ':\r:',
              f"native paste sent incorrect or duplicate input: {[entry['args'][0]['data'] for entry in inputs]!r}")
        check(page.locator('#context-menu').evaluate("el => getComputedStyle(el).userSelect") == 'none', 'context menu text remains selectable')

        clear_emitted(page)
        page.evaluate("window.terminalTest.writeTerminalOutput('\\x1b[?2004h')")
        page.wait_for_timeout(100)
        page.evaluate("""() => {
            const data = new DataTransfer(); data.setData('text/plain', ':\\n:');
            document.querySelector('.terminal-pane.active .xterm-helper-textarea').dispatchEvent(
                new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
        }""")
        check(not get_emitted(page, 'ssh_input'), 'bracketed paste sent before review')
        page.evaluate("document.getElementById('paste-review-approve').click()")
        inputs = get_emitted(page, 'ssh_input')
        check(len(inputs) == 1 and inputs[0]['args'][0]['data'] == '\x1b[200~:\r:\x1b[201~', 'review lost bracketed paste semantics')

        clear_emitted(page)
        page.evaluate("""() => document.querySelector('.terminal-pane.active .xterm-helper-textarea').dispatchEvent(
            new KeyboardEvent('keydown', {key: 'v', code: 'KeyV', keyCode: 86, ctrlKey: true, bubbles: true, cancelable: true}))""")
        inputs = get_emitted(page, 'ssh_input')
        check(len(inputs) == 1 and inputs[0]['args'][0]['data'] == '\x16', 'Ctrl+V no longer sends the terminal control code')

        # A previous document's request must not be reusable after a reload.
        request = begin()
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.standtermUi?.version === 1')
        check(not page.evaluate("id => window.standtermUi.completeContextPaste(id, ':')", request), 'reloaded document accepted an old paste request')
    finally:
        close_context(context)


def test_clipboard_paste_encoding_is_consistent(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        for bracketed in [False, True]:
            page.evaluate("value => window.terminalTest.writeTerminalOutput(value)", '\x1b[?2004' + ('h' if bracketed else 'l'))
            page.wait_for_timeout(100)
            for route in ['native', 'web', 'desktop']:
                for text in [':', ':\n:', ':\r\n:', ':\x1b[201~:', ':\x1b[201~\n:']:
                    result = page.evaluate("""async ({route, text}) => {
                        const area = document.querySelector('.terminal-pane.active .xterm-helper-textarea');
                        area.focus(); window.terminalTest.clearEmitted();
                        if (route === 'native') {
                            const data = new DataTransfer(); data.setData('text/plain', text);
                            area.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
                        } else {
                            let resolve;
                            Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                                readText: () => route === 'web' ? Promise.resolve(text)
                                    : new Promise(done => { resolve = done; })
                            }});
                            const pending = document.getElementById('paste-option').onclick({stopPropagation() {}});
                            if (route === 'desktop') {
                                const id = window.standtermUi.contextPasteRequest();
                                if (!id || !window.standtermUi.completeContextPaste(id, text)) throw new Error('Missing paste target');
                                resolve('');
                            }
                            await pending;
                        }
                        const open = () => document.getElementById('paste-review-modal').classList.contains('open');
                        const outputs = () => window.terminalTest.getEmitted()
                            .filter(entry => entry.event === 'ssh_input').map(entry => entry.args[0].data);
                        const review = open();
                        const before = outputs();
                        const preview = document.getElementById('paste-review-preview').value;
                        if (review) document.getElementById('paste-review-approve').click();
                        return {review, before, preview, repeatedReview: open(), outputs: outputs()};
                    }""", {'route': route, 'text': text})
                    expected = text.replace('\x1b', '\u241b')
                    check(result['review'] == ('\n' in text), f'{route}: incorrect paste review decision')
                    if result['review']:
                        check(not result['before'], f'{route}: paste sent before approval')
                        check(result['preview'] == expected.replace('\r\n', '\n'), f'{route}: preview differs from sanitized text')
                    expected = expected.replace('\r\n', '\r').replace('\n', '\r')
                    if bracketed:
                        expected = '\x1b[200~' + expected + '\x1b[201~'
                    check(result['outputs'] == [expected],
                          f"{route}: paste encoding differs or was sent more than once: {result['outputs']!r}, expected {[expected]!r}")
                    check(not result['repeatedReview'], f'{route}: approved paste was reviewed again')
    finally:
        close_context(context)


def test_approval_payload_and_stale_rejections(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        set_agent_mode(page, 'approval', 'approval_pending')
        action = request_agent_action(page, ':\n')
        check(page.inner_text('#agent-approve-btn') == 'Approve input', 'input approval did not name the proposed input')
        check(page.inner_text('#agent-reject-btn') == 'Reject', 'input rejection label changed its meaning')

        clear_emitted(page)
        page.evaluate("() => document.getElementById('agent-approve-btn').click()")
        page.wait_for_function(
            "() => window.terminalTest.getEmitted().some(entry => entry.event === 'agent_action_approve')",
            timeout=10000,
        )
        approve_events = get_emitted(page, 'agent_action_approve')
        approve_payload = approve_events[-1]['args'][0]
        for key in ['proposal_id', 'session_id', 'viewer_id', 'agent_binding_id', 'mode_version', 'privacy_version']:
            check(key in approve_payload, f'approval payload omitted {key}')
            check(approve_payload[key] is not None and approve_payload[key] != '', f'approval payload omitted {key}')
            check(approve_payload[key] == action[key], f'approval payload {key} did not match the proposal')
        wait_for_agent(page, "state.last_action && state.last_action.status === 'completed'")

        privacy_action = request_agent_action(page, ':\n')
        stale_privacy_payload = approval_payload_from_action(privacy_action)
        set_privacy(page, 'private_input')
        wait_for_agent(
            page,
            f"state.privacy_state === 'private_input' && state.privacy_version > {privacy_action['privacy_version']}",
        )
        emit_socket(page, 'agent_action_approve', stale_privacy_payload)
        wait_for_last_action_error(page, 'agent_privacy_blocked')

        set_privacy(page, 'normal')
        wait_for_agent(page, "state.privacy_state === 'normal'")
        mode_action = request_agent_action(page, ':\n')
        stale_mode_payload = approval_payload_from_action(mode_action)
        emit_socket(page, 'agent_mode_set', {'terminal_id': TERMINAL_ID, 'mode': 'observe'})
        wait_for_agent(page, f"state.mode === 'observe' && state.mode_version > {mode_action['mode_version']}")
        emit_socket(page, 'agent_action_approve', stale_mode_payload)
        wait_for_last_action_error(page, 'agent_mode_changed')
    finally:
        close_context(context)


def test_file_copy_approval_shows_canonical_plan(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language=ui_language)
    zh = ui_language == 'zh-TW'
    try:
        attach_agent(page)
        set_agent_mode(page, 'direct', 'direct_active')
        state = active_agent_state(page)
        page.evaluate(
            """payload => window.terminalTest.applyAgentActionPayloadForTest(payload)""",
            {
                'action_id': 'copy-action-1',
                'proposal_id': 'copy-proposal-1',
                'action_type': 'file_copy',
                'status': 'pending_approval',
                'terminal_id': TERMINAL_ID,
                'destination_terminal_id': 'term-2',
                'session_id': state['session_id'],
                'viewer_id': state['viewer_id'],
                'agent_binding_id': state['agent_binding_id'],
                'mode_version': state['mode_version'],
                'privacy_version': state['privacy_version'],
                'source_endpoint': {
                    'route': 'direct',
                    'user': 'builder',
                    'host': 'source.example',
                    'port': 22,
                },
                'destination_endpoint': {
                    'route': 'local',
                    'shell': 'bash',
                    'platform': 'linux',
                },
                'source_path': '/srv/releases/image.bin',
                'destination_path': '/tmp/image.bin',
                'source_size': 1536,
                'destination_exists': True,
                'destination_existing_size': 64,
                'conflict_mode': 'replace',
                'escaped_preview': 'Copy approved backend plan',
            },
        )
        details = page.evaluate(
            """() => ({
                visible: document.getElementById('agent-file-copy-details').classList.contains('visible'),
                source: document.getElementById('agent-file-copy-source').innerText,
                destination: document.getElementById('agent-file-copy-destination').innerText,
                size: document.getElementById('agent-file-copy-size').innerText,
                warning: document.getElementById('agent-file-copy-warning').innerText,
                approve: document.getElementById('agent-approve-btn').innerText,
                deny: document.getElementById('agent-reject-btn').innerText,
                previewDisplay: getComputedStyle(document.getElementById('agent-action-preview')).display,
                metaDisplay: getComputedStyle(document.getElementById('agent-action-meta')).display,
                actionHeight: document.getElementById('agent-action-box').getBoundingClientRect().height,
                approveDisabled: document.getElementById('agent-approve-btn').disabled
            })"""
        )
        check(details['visible'] is True, 'file copy approval details were hidden')
        check(details['source'] == 'builder@source.example:22:/srv/releases/image.bin', 'source plan was not exact')
        check(details['destination'] == 'Local Shell (bash):/tmp/image.bin', 'destination plan was not exact')
        check(details['size'] == '1.50 KiB', 'source size was not rendered')
        check(details['warning'] == ('將覆寫既有檔案（64 B）。' if zh else 'Replace the existing file (64 B).'),
              'replace warning did not retain the existing destination size and overwrite consequence')
        check(details['approve'] == ('核准複製' if zh else 'Approve copy'), 'copy approval button was not explicit')
        check(details['deny'] == ('拒絕' if zh else 'Reject'), 'copy rejection button changed its meaning')
        check(details['previewDisplay'] == 'none' and details['metaDisplay'] == 'none', 'generic action details expanded the copy prompt')
        check(details['actionHeight'] < 220, 'copy approval prompt was not compact')
        check(details['approveDisabled'] is False, 'copy approval button was unexpectedly disabled')
        copy_payload = active_agent_state(page)['pending_action']
        page.evaluate('payload => window.terminalTest.applyAgentActionPayloadForTest(payload)', {
            **copy_payload, 'action_revision': 1, 'conflict_mode': 'keep_both',
            'destination_path': '/tmp/image (2).bin',
        })
        check(page.inner_text('#agent-file-copy-destination') == 'Local Shell (bash):/tmp/image (2).bin',
              'keep-both translation changed the canonical destination filename')
        check(page.inner_text('#agent-file-copy-warning') == ('複製到顯示的目的地，並保留既有檔案。' if zh else
              'Copy to the destination shown; keep the existing file.'), 'keep-both warning did not preserve the existing file')
        page.evaluate('payload => window.terminalTest.applyAgentActionPayloadForTest(payload)', {
            **copy_payload, 'action_revision': 2, 'status': 'running', 'bytes_copied': 768, 'total_bytes': 1536,
        })
        clear_emitted(page)
        page.evaluate(
            """payload => window.terminalTest.applyAgentActionPayloadForTest(payload)""",
            {
                'action_id': 'copy-action-1',
                'action_type': 'file_copy',
                'action_revision': 3,
                'status': 'failed',
                'terminal_id': TERMINAL_ID,
                'error_code': 'file_copy_publish_outcome_unknown',
            },
        )
        status_detail = page.locator('#agent-status-detail').inner_text()
        unknown_warning = ('無法確認複製結果。目的地可能已變更，請先檢查再重試。' if zh else
                           'Copy result unknown. The destination may have changed; check it before retrying.')
        check(
            unknown_warning in status_detail and 'file_copy_publish_outcome_unknown' in status_detail,
            'publish outcome warning was not explicit',
        )
        queue_text = page.inner_text('.transfer-queue-item[data-action-id="copy-action-1"]')
        check(unknown_warning in queue_text and 'file_copy_publish_outcome_unknown' in queue_text,
              'transfer queue lost the unknown-result warning or diagnostic code')
        for width in [1280, 480]:
            page.set_viewport_size({'width': width, 'height': 700})
            for selector in ['#agent-status-detail', '.transfer-queue-status']:
                visible_warning = page.locator(selector).evaluate('''element => ({
                    wraps: getComputedStyle(element).whiteSpace !== 'nowrap',
                    fits: element.scrollWidth <= element.clientWidth,
                    height: element.clientHeight
                })''')
                check(visible_warning['wraps'] and visible_warning['fits'] and visible_warning['height'] > 0,
                      f'unknown-result warning is clipped at {width}px: {selector}')
        check(not any(entry['event'] in {'agent_action_approve', 'agent_action_cancel', 'agent_suggestion_request', 'ssh_input'}
                      for entry in get_emitted(page)), 'unknown publication outcome automatically retried an operation')
    finally:
        close_context(context)


def test_localized_input_decisions_preserve_exact_proposal(browser, access_url):
    context, page = new_page(browser, access_url, ui_language='zh-TW')
    try:
        attach_agent(page)
        set_agent_mode(page, 'approval', 'approval_pending')
        state = active_agent_state(page)
        for decision in ['approve', 'reject']:
            action = {
                'action_id': f'localized-input-{decision}',
                'proposal_id': f'localized-proposal-{decision}',
                'action_type': 'terminal_input', 'action_revision': 0,
                'status': 'pending_approval', 'terminal_id': TERMINAL_ID,
                'escaped_preview': '<b>Approve copy</b> \\n',
                'byte_length': 26, 'line_count': 1,
                'ends_with_newline': False, 'contains_control_chars': False,
                **{field: state[field] for field in ['session_id', 'viewer_id', 'agent_binding_id', 'mode_version', 'privacy_version']},
            }
            result = page.evaluate(
                """({action, decision}) => {
                    window.terminalTest.applyAgentActionPayloadForTest(action);
                    window.terminalTest.clearEmitted();
                    const approve = document.getElementById('agent-approve-btn');
                    const reject = document.getElementById('agent-reject-btn');
                    const preview = document.getElementById('agent-action-preview');
                    const labels = {approve: approve.innerText, reject: reject.innerText,
                        preview: preview.innerText, markup: preview.querySelectorAll('b').length};
                    const button = decision === 'approve' ? approve : reject;
                    button.click();
                    button.click();
                    return {labels, events: window.terminalTest.getEmitted(),
                        locked: approve.disabled && reject.disabled,
                        mode: window.terminalTest.getActiveAgentState().mode};
                }""",
                {'action': action, 'decision': decision},
            )
            check(result['labels']['approve'] == '核准輸入' and result['labels']['reject'] == '拒絕',
                  'localized input decision labels did not distinguish input from copy')
            check(result['labels']['preview'] == action['escaped_preview'] and result['labels']['markup'] == 0,
                  'proposal display text was translated or interpreted as markup')
            check(len(result['events']) == 1 and result['events'][0]['event'] == f'agent_action_{decision}',
                  'localized input decision emitted another action or more than one decision')
            check(result['events'][0]['args'][0] == approval_payload_from_action(action),
                  'localized input decision changed the exact proposal binding')
            check(result['locked'] and result['mode'] == 'approval_pending',
                  'one input decision changed permission or remained repeatable')
    finally:
        close_context(context)


def test_agent_transfer_stop_states_and_dismissal(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language=ui_language)
    zh = ui_language == 'zh-TW'
    try:
        attach_agent(page)
        state = active_agent_state(page)
        action = {
            'action_id': 'localized-transfer-stop', 'proposal_id': 'localized-transfer-proposal',
            'action_type': 'file_copy', 'action_revision': 1,
            'status': 'running', 'terminal_id': TERMINAL_ID,
            'destination_terminal_id': 'fixture-destination',
            'source_endpoint': {'route': 'local', 'shell': 'bash', 'platform': 'linux'},
            'destination_endpoint': {'route': 'direct', 'host': 'destination.example', 'user': 'builder', 'port': 22},
            'source_path': '/fixture/source.bin', 'destination_path': '/fixture/existing.bin',
            'source_size': 1536, 'total_bytes': 1536, 'bytes_copied': 768,
            **{field: state[field] for field in ['session_id', 'viewer_id', 'agent_binding_id', 'mode_version', 'privacy_version']},
        }
        result = page.evaluate(
            """action => {
                const apply = payload => window.terminalTest.applyAgentActionPayloadForTest(payload);
                const button = () => document.querySelector('.transfer-queue-stop');
                const status = () => document.querySelector('.transfer-queue-status').innerText;
                apply(action);
                window.terminalTest.clearEmitted();
                const running = button().innerText;
                button().click();
                const pending = {text: button().innerText, disabled: button().disabled, status: status()};
                button().click();
                const cancellations = window.terminalTest.getEmitted();
                apply({...action, action_revision: 2, status: 'committing'});
                window.terminalTest.clearEmitted();
                const committing = {disabled: button().disabled, title: button().title, status: status()};
                button().click();
                committing.events = window.terminalTest.getEmitted();
                apply({...action, action_revision: 3, status: 'failed', error_code: 'file_copy_cancelled_by_operator'});
                const stopped = {status: status(), title: button().title, aria: button().getAttribute('aria-label')};
                const lastAction = window.terminalTest.getActiveAgentState().last_action;
                window.terminalTest.clearEmitted();
                button().click();
                return {running, pending, cancellations, committing, stopped,
                    dismissed: document.querySelectorAll('.transfer-queue-item').length === 0,
                    dismissEvents: window.terminalTest.getEmitted(),
                    lastActionUnchanged: JSON.stringify(lastAction) === JSON.stringify(window.terminalTest.getActiveAgentState().last_action)};
            }""",
            action,
        )
        check(result['running'] == ('停止' if zh else 'Stop'), 'running transfer did not offer Stop')
        check(result['pending']['text'] == ('正在停止…' if zh else 'Stopping…') and result['pending']['disabled'],
              'pending cancellation did not remain distinct from a confirmed stop')
        check(result['pending']['status'] != ('已停止' if zh else 'Stopped'), 'Stop click claimed backend-confirmed cancellation')
        check(len(result['cancellations']) == 1 and result['cancellations'][0]['event'] == 'agent_action_cancel',
              'Stop emitted more than one cancellation or another operation')
        check(result['cancellations'][0]['args'][0] == approval_payload_from_action(action),
              'transfer cancellation changed the proposal binding')
        check(result['committing']['disabled'] and not result['committing']['events'], 'committing transfer remained cancellable')
        check(result['committing']['title'] == ('正在完成複製，已無法停止。' if zh else 'Finishing the copy; it can no longer be stopped.'),
              'committing tooltip implied completion or allowed stopping')
        check(result['stopped']['status'] == ('已停止' if zh else 'Stopped'), 'operator cancellation was not shown as confirmed stopped')
        check(result['stopped']['title'] == ('隱藏此筆' if zh else 'Dismiss') and 'existing.bin' in result['stopped']['aria'],
              'finished transfer dismissal lost its meaning or filename')
        check(result['dismissed'] and not result['dismissEvents'] and result['lastActionUnchanged'],
              'Dismiss changed the transfer result or emitted a file operation')

        failed = {**action, 'action_id': 'localized-disconnected-copy', 'proposal_id': 'localized-disconnected-proposal'}
        result = page.evaluate(
            """action => {
                window.terminalTest.applyAgentActionPayloadForTest(action);
                window.terminalTest.applyAgentActionPayloadForTest({...action, action_revision: 2,
                    status: 'failed', error_code: 'terminal_disconnected'});
                return document.querySelector('.transfer-queue-status').innerText;
            }""", failed)
        check('terminal_disconnected' in result and result != ('已停止' if zh else 'Stopped'),
              'generic disconnection was misrepresented as confirmed cancellation')
    finally:
        close_context(context)


def test_agent_copy_and_transfer_states_in_traditional_chinese(browser, access_url):
    test_file_copy_approval_shows_canonical_plan(browser, access_url, ui_language='zh-TW')
    test_agent_transfer_stop_states_and_dismissal(browser, access_url, ui_language='zh-TW')


def test_file_copy_approval_is_global_and_decision_is_single_shot(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.evaluate(
            """() => window.terminalTest.applyTerminalListForTest({
                terminals: [
                    {
                        terminal_id: 'main',
                        connection_type: 'ssh',
                        terminal_label: 'SSH - source',
                        term: 'xterm-256color',
                        connected: true
                    },
                    {
                        terminal_id: 'term-2',
                        connection_type: 'ssh',
                        terminal_label: 'SSH - destination',
                        term: 'xterm-256color',
                        connected: true
                    }
                ]
            })"""
        )
        page.evaluate("() => window.terminalTest.switchTerminalForTest('term-2')")
        copy_payload = {
            'action_id': 'copy-global-1',
            'proposal_id': 'copy-global-proposal-1',
            'action_type': 'file_copy',
            'action_revision': 0,
            'status': 'pending_approval',
            'terminal_id': 'main',
            'destination_terminal_id': 'term-2',
            'source_endpoint': {'route': 'direct', 'user': 'source', 'host': 'source.example', 'port': 22},
            'destination_endpoint': {'route': 'direct', 'user': 'destination', 'host': 'destination.example', 'port': 22},
            'source_path': '/srv/source.bin',
            'destination_path': '/srv/destination.bin',
            'source_size': 1536,
            'bytes_copied': 0,
            'total_bytes': 1536,
            'conflict_mode': 'fail',
            'escaped_preview': 'Copy source to destination',
        }
        page.evaluate(
            "payload => window.terminalTest.applyAgentActionPayloadForTest(payload)",
            copy_payload,
        )
        global_prompt = page.evaluate(
            """() => ({
                tabs: window.terminalTest.getTerminalTabsState(),
                panelVisible: document.getElementById('agent-panel').classList.contains('visible'),
                actionVisible: document.getElementById('agent-action-box').classList.contains('visible'),
                approveDisabled: document.getElementById('agent-approve-btn').disabled
            })"""
        )
        check(global_prompt['tabs']['activeTerminalId'] == 'term-2', 'file copy prompt switched the active terminal')
        check(global_prompt['tabs']['agentPanelTerminalId'] == 'main', 'file copy prompt did not target the source terminal')
        check(global_prompt['tabs']['agentPanelInMainDocument'] is True, 'file copy prompt stayed in another document')
        check(global_prompt['panelVisible'] is True and global_prompt['actionVisible'] is True, 'background file copy prompt was hidden')
        check(global_prompt['approveDisabled'] is False, 'background file copy approval was disabled')

        clear_emitted(page)
        page.evaluate(
            """() => {
                const button = document.getElementById('agent-approve-btn');
                button.click();
                button.click();
            }"""
        )
        approvals = get_emitted(page, 'agent_action_approve')
        check(len(approvals) == 1, 'double click emitted more than one file copy approval')
        check(page.locator('#agent-approve-btn').is_disabled(), 'approval button did not lock immediately')

        page.evaluate(
            "payload => window.terminalTest.applyAgentActionPayloadForTest(payload)",
            {**copy_payload, 'action_revision': 2, 'status': 'running', 'bytes_copied': 768},
        )
        progress = page.evaluate(
            """() => ({
                actionVisible: document.getElementById('agent-action-box').classList.contains('visible'),
                queueButtonVisible: !document.getElementById('transfer-queue-btn').hidden,
                queueVisible: document.getElementById('transfer-queue-panel').classList.contains('visible'),
                count: document.getElementById('transfer-queue-count').innerText,
                status: document.querySelector('.transfer-queue-status')?.innerText,
                progress: document.querySelector('.transfer-queue-progress')?.getAttribute('aria-valuenow'),
                stop: document.querySelector('.transfer-queue-stop')?.innerText,
                toolbarOrder: [
                    document.getElementById('sftp-status-btn').nextElementSibling.id,
                    document.getElementById('transfer-queue-btn').nextElementSibling.id
                ]
            })"""
        )
        check(progress['actionVisible'] is False, 'running file copy kept the approval prompt open')
        check(progress['queueButtonVisible'] is True and progress['queueVisible'] is True, 'running transfer queue was hidden')
        check(progress['count'] == '1', 'transfer queue count was incorrect')
        check(progress['status'] == '50% · 768 B of 1.50 KiB', 'file copy progress was incorrect')
        check(progress['progress'] == '50', 'file copy progress bar was incorrect')
        check(progress['stop'] == 'Stop', 'running transfer could not be stopped')
        check(progress['toolbarOrder'] == ['transfer-queue-btn', 'quick-settings'], 'transfer queue was not between Files and Settings')

        clear_emitted(page)
        page.click('.transfer-queue-stop')
        cancel_events = get_emitted(page, 'agent_action_cancel')
        check(len(cancel_events) == 1, 'transfer stop did not emit one cancellation')
        check(cancel_events[0]['args'][0]['terminal_id'] == 'main', 'transfer stop targeted the active tab instead of its source tab')

        page.evaluate(
            "payload => window.terminalTest.applyAgentActionPayloadForTest(payload)",
            {**copy_payload, 'action_revision': 3, 'status': 'completed', 'bytes_copied': 1536},
        )
        page.evaluate(
            "payload => window.terminalTest.applyAgentActionPayloadForTest(payload)",
            {**copy_payload, 'action_revision': 2, 'status': 'running', 'bytes_copied': 768},
        )
        monotonic = page.evaluate(
            "() => window.terminalTest.getAgentStateForTest('main')",
        )
        check(monotonic['last_action']['status'] == 'completed', 'stale progress replaced the completed action')
        check(monotonic['pending_action'] is None, 'stale progress reopened the completed action')
        completed_queue = page.evaluate(
            """() => ({
                status: document.querySelector('.transfer-queue-status')?.innerText,
                dismiss: document.querySelector('.transfer-queue-stop')?.innerText,
                buttonVisible: !document.getElementById('transfer-queue-btn').hidden
            })"""
        )
        check(completed_queue['status'] == 'Completed · 1.50 KiB', 'completed transfer result was not retained')
        check(completed_queue['dismiss'] == '×', 'completed transfer did not offer dismissal')
        check(completed_queue['buttonVisible'] is True, 'completed transfer disappeared immediately')
        page.wait_for_selector('#transfer-queue-btn', state='hidden', timeout=12000)
        check(page.locator('#transfer-queue-panel').get_attribute('aria-hidden') == 'true', 'empty transfer queue stayed open')
        page.click('#agent-panel-close-btn')
        page.evaluate(
            """payload => window.terminalTest.applyAgentActionPayloadForTest(payload)""",
            {
                'action_id': 'command-background-1',
                'action_type': 'terminal_input',
                'status': 'pending_approval',
                'terminal_id': 'main',
                'escaped_preview': 'echo background',
            },
        )
        normal_prompt = page.evaluate(
            """() => ({
                panelVisible: document.getElementById('agent-panel').classList.contains('visible'),
                activeTerminalId: window.terminalTest.getTerminalTabsState().activeTerminalId,
                pending: window.terminalTest.getAgentStateForTest('main').pending_action
            })"""
        )
        check(normal_prompt['activeTerminalId'] == 'term-2', 'normal approval switched the active terminal')
        check(normal_prompt['panelVisible'] is False, 'normal command approval became a global prompt')
        check(normal_prompt['pending']['action_id'] == 'command-background-1', 'normal approval was not retained on its terminal')
    finally:
        close_context(context)


def test_file_copy_approval_keeps_controls_visible_with_long_paths(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        # Debug instrumentation exposes the fixture API, but its overlay is not
        # part of the operator's normal approval layout.
        page.add_style_tag(content='#debug-hud, #payload-log { display: none !important; }')
        attach_agent(page)
        set_agent_mode(page, 'direct', 'direct_active')
        payload = {
            'action_id': 'copy-long-layout', 'proposal_id': 'copy-long-proposal',
            'action_type': 'file_copy', 'status': 'pending_approval',
            'terminal_id': TERMINAL_ID, 'destination_terminal_id': 'term-2',
            'source_endpoint': {'route': 'direct', 'user': 'source', 'host': 'source.example', 'port': 22},
            'destination_endpoint': {'route': 'direct', 'user': 'destination', 'host': 'destination.example', 'port': 22},
            'source_path': '/source/' + 'long-directory/' * 100 + 'image.bin',
            'destination_path': '/destination/' + 'another-directory/' * 100 + 'image.bin',
            'source_size': 1536, 'conflict_mode': 'replace', 'destination_exists': True,
            'destination_existing_size': 64, 'escaped_preview': 'Copy plan\n' * 100,
        }
        for width, height in [(640, 480), (360, 300)]:
            page.set_viewport_size({'width': width, 'height': height})
            page.evaluate('payload => window.terminalTest.applyAgentActionPayloadForTest(payload)', payload)
            page.wait_for_selector('#agent-action-box.visible')
            geometry = page.evaluate("""() => {
                const panel = document.getElementById('agent-panel').getBoundingClientRect();
                const content = document.getElementById('agent-action-content');
                const actionBox = document.getElementById('agent-action-box').getBoundingClientRect();
                const buttons = ['agent-approve-btn', 'agent-reject-btn'].map(id => {
                    const button = document.getElementById(id);
                    const r = button.getBoundingClientRect();
                    return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth
                        && document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) === button;
                });
                return { buttons, panel: { top: panel.top, bottom: panel.bottom, height: panel.height },
                    actionHeight: actionBox.height,
                    pauseDisplay: getComputedStyle(document.getElementById('agent-action-pause-btn')).display,
                    hits: ['agent-approve-btn', 'agent-reject-btn'].map(id => {
                        const r = document.getElementById(id).getBoundingClientRect();
                        const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                        return { id: hit?.id, tag: hit?.tagName };
                    }),
                    controls: document.getElementById('agent-action-controls').getBoundingClientRect().toJSON(),
                    panelFits: panel.top >= 0 && panel.bottom <= innerHeight,
                    scrollable: content.scrollHeight > content.clientHeight,
                    noHorizontalOverflow: content.scrollWidth <= content.clientWidth + 1 };
            }""")
            check(all(geometry['buttons']), f'long copy details hid or covered an approval control: {geometry}')
            check(geometry['panelFits'], 'approval panel exceeded the viewport')
            check(geometry['actionHeight'] <= min(320, height * 0.48) + 1, 'copy approval card exceeded its compact limit')
            check(geometry['pauseDisplay'] == 'none', 'copy approval showed the unrelated pause control')
            check(geometry['scrollable'], 'long copy details were not scrollable')
            check(geometry['noHorizontalOverflow'], 'long paths caused horizontal overflow')
            page.evaluate("document.getElementById('agent-action-content').scrollTop = 999999")
            check('Replace the existing file (64 B).' == page.locator('#agent-file-copy-warning').inner_text(), 'replace warning was lost')
            page.locator('#agent-approve-btn').click(trial=True)
            page.locator('#agent-reject-btn').click(trial=True)
        clear_emitted(page)
        page.click('#agent-reject-btn')
        check(len(get_emitted(page, 'agent_action_reject')) == 1, 'visible reject did not emit one decision')
    finally:
        close_context(context)


def test_ime_anchor_poc_loads_and_fails_open(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.wait_for_selector('.xterm[data-ime-anchor-poc="enabled"]')
        # The optional PoC asset must not prevent Core startup if unavailable.
        page.route('**/static/js/standterm-ime-anchor-poc.js', lambda route: route.abort())
        page.reload(wait_until='domcontentloaded')
        page.wait_for_selector('.xterm')
        check(page.locator('.xterm[data-ime-anchor-poc]').count() == 0,
              'Missing PoC asset did not retain native xterm positioning')
    finally:
        close_context(context)


def test_cjk_width_compatibility_defaults_off(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        state = page.evaluate(
            """() => ({
                checked: document.getElementById('pref-cjkWideAmbiguous').checked
            })"""
        )
        check(state['checked'] is False, 'CJK width compatibility checkbox defaulted on')
    finally:
        close_context(context)


def test_windows_font_fallback_defaults_and_migrates_legacy(browser, access_url):
    powerline = '"StandTerm Powerline Symbols", '
    expected = powerline + 'Consolas, "Cascadia Mono", "Courier New", monospace'
    legacy = 'Consolas, "Courier New", monospace'
    custom = 'Custom Mono, monospace'
    context, page = new_page(browser, access_url)
    try:
        initial = page.evaluate("() => window.terminalTest.getActiveTerminalOptions().fontFamily")
        check(initial == expected, 'terminal font fallback did not default to Cascadia Mono')

        page.evaluate(
            "fontFace => localStorage.setItem('terminal.pref.v1', JSON.stringify({ fontFace }))",
            legacy,
        )
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        page.wait_for_function(
            '() => window.terminalTest.getActiveTerminalOptions() !== null',
            timeout=10000,
        )
        migrated = page.evaluate("() => window.terminalTest.getActiveTerminalOptions().fontFamily")
        check(migrated == expected, 'legacy terminal font fallback was not migrated')

        page.evaluate(
            "fontFace => localStorage.setItem('terminal.pref.v1', JSON.stringify({ fontFace }))",
            custom,
        )
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            '() => window.terminalTest.getActiveTerminalOptions() !== null',
            timeout=10000,
        )
        preserved = page.evaluate("() => window.terminalTest.getActiveTerminalOptions().fontFamily")
        check(preserved == powerline + custom, 'custom terminal font face was overwritten by default migration')
    finally:
        close_context(context)


def test_powerline_symbol_fallback_defaults_on_and_preserves_opt_out(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        initial = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(
            initial['fontFamily'].startswith('"StandTerm Powerline Symbols"'),
            'Powerline symbol fallback did not default on',
        )

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        check(
            page.locator('#pref-powerlineSymbols').is_checked() is True,
            'Powerline symbol fallback checkbox did not default on',
        )
        page.uncheck('#pref-powerlineSymbols')
        page.click('#settings-save')
        page.wait_for_function(
            "() => !window.terminalTest.getActiveTerminalOptions().fontFamily.startsWith('\\\"StandTerm Powerline Symbols\\\"')",
            timeout=5000,
        )
        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        page.check('#pref-powerlineSymbols')
        page.click('#settings-save')
        page.wait_for_function(
            "() => window.terminalTest.getActiveTerminalOptions().fontFamily.startsWith('\\\"StandTerm Powerline Symbols\\\"')",
            timeout=5000,
        )
        enabled = page.evaluate(
            """() => ({
                options: window.terminalTest.getActiveTerminalOptions(),
                stored: JSON.parse(localStorage.getItem('terminal.pref.v1')).powerlineSymbols
            })"""
        )
        check(enabled['stored'] is True, 'Powerline symbol fallback preference was not saved')
        check(
            enabled['options']['mirrorFontFamily'] == enabled['options']['fontFamily'],
            'Powerline symbol fallback did not update the agent mirror',
        )
        loaded = page.evaluate(
            """async () => {
                const fonts = await document.fonts.load(
                    '14px "StandTerm Powerline Symbols"',
                    '\ue0a0'
                );
                return fonts.length;
            }"""
        )
        check(loaded > 0, 'bundled Powerline symbols font did not load')

        page.click('#new-tab-btn')
        page.wait_for_function(
            "() => window.terminalTest.getTerminalTabsState().tabs.length === 2",
            timeout=5000,
        )
        new_tab = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(
            new_tab['fontFamily'] == enabled['options']['fontFamily'],
            'new terminal did not use the Powerline symbol fallback',
        )
        check(
            new_tab['mirrorFontFamily'] == enabled['options']['fontFamily'],
            'new agent mirror did not use the Powerline symbol fallback',
        )

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        page.uncheck('#pref-powerlineSymbols')
        page.click('#settings-save')
        page.wait_for_function(
            "() => !window.terminalTest.getActiveTerminalOptions().fontFamily.startsWith('\\\"StandTerm Powerline Symbols\\\"')",
            timeout=5000,
        )
        disabled = page.evaluate(
            """() => ({
                options: window.terminalTest.getActiveTerminalOptions(),
                stored: JSON.parse(localStorage.getItem('terminal.pref.v1')).powerlineSymbols
            })"""
        )
        check(disabled['stored'] is False, 'Powerline symbol fallback disable was not saved')
        check(
            disabled['options']['mirrorFontFamily'] == disabled['options']['fontFamily'],
            'disabling Powerline symbol fallback did not update the agent mirror',
        )
        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        page.uncheck('#pref-showTerminalTitleInStatusBar')
        page.click('#settings-save')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function(
            '() => !!window.terminalTest && window.terminalTest.getActiveTerminalOptions() !== null',
            timeout=10000,
        )
        restored = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(restored['fontFamily'] == disabled['options']['fontFamily'], 'saved Powerline opt-out was overwritten on reload')
        check(restored['mirrorFontFamily'] == restored['fontFamily'], 'agent mirror ignored the restored Powerline opt-out')
        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        check(page.locator('#pref-powerlineSymbols').is_checked() is False, 'saved Powerline opt-out checkbox was overwritten')
        check(page.locator('#pref-showTerminalTitleInStatusBar').is_checked() is False, 'saved terminal title opt-out was overwritten')
    finally:
        close_context(context)


def test_webgl_renderer_closes_block_glyph_row_gaps(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        options = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(options['renderer'] == 'webgl', 'terminal did not activate the WebGL renderer')

        odd_metrics = None
        for font_size in range(8, 33):
            page.evaluate(
                "size => window.terminalTest.setActiveTerminalFontSizeForTest(size)",
                font_size,
            )
            page.wait_for_timeout(50)
            metrics = page.evaluate(
                """() => {
                    const canvas = Array.from(document.querySelectorAll(
                        '.terminal-pane.active .xterm-screen canvas'
                    )).at(-1);
                    const options = window.terminalTest.getActiveTerminalOptions();
                    const cells = window.terminalTest.getActiveTerminalBufferCellsForTest(0);
                    if (!canvas || !options || !cells || !cells.length) return null;
                    return {
                        fontSize: options.fontSize,
                        cellWidth: Math.round(canvas.width / cells.length)
                    };
                }"""
            )
            if metrics and metrics['cellWidth'] == 7:
                odd_metrics = metrics
                break
        check(odd_metrics is not None, 'could not create a 7-pixel WebGL cell width fixture')

        page.evaluate(
            """() => window.terminalTest.writeTerminalOutput(
                '\\x1b[2J\\x1b[H\\x1b[?25l\\x1b[38;2;215;135;135m'
                + '████████\\r\\n████████\\r\\n████████\\r\\n▛███▛'
                + '\\x1b[0m'
            )"""
        )
        page.wait_for_timeout(200)
        pixels = page.evaluate(
            """() => {
                const canvases = Array.from(document.querySelectorAll(
                    '.terminal-pane.active .xterm-screen canvas'
                ));
                const canvas = canvases[canvases.length - 1];
                if (!canvas) return { error: 'missing_canvas' };
                const gl = canvas.getContext('webgl2');
                if (!gl) return { error: 'missing_webgl_context' };
                const rgba = new Uint8Array(canvas.width * canvas.height * 4);
                gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
                const coloredRows = [];
                let partialCoralPixels = 0;
                for (let y = 0; y < canvas.height; y += 1) {
                    let count = 0;
                    for (let x = 0; x < canvas.width; x += 1) {
                        const offset = (y * canvas.width + x) * 4;
                        const red = rgba[offset];
                        const green = rgba[offset + 1];
                        const blue = rgba[offset + 2];
                        if (red > 180 && green >= 100 && green <= 170 && blue >= 100 && blue <= 170) {
                            count += 1;
                        }
                        const coralLike = red > 40 && green > 20 && blue > 20
                            && red > green && Math.abs(green - blue) <= 2;
                        const exactCoral = Math.abs(red - 215) <= 2
                            && Math.abs(green - 135) <= 2 && Math.abs(blue - 135) <= 2;
                        if (coralLike && !exactCoral) partialCoralPixels += 1;
                    }
                    if (count >= 20) coloredRows.push(y);
                }
                let maxStep = 0;
                for (let index = 1; index < coloredRows.length; index += 1) {
                    maxStep = Math.max(maxStep, coloredRows[index] - coloredRows[index - 1]);
                }
                return { coloredRowCount: coloredRows.length, maxStep, partialCoralPixels };
            }"""
        )
        check(not pixels.get('error'), f"could not inspect WebGL terminal pixels: {pixels.get('error')}")
        check(pixels['coloredRowCount'] > 20, 'block glyph fixture did not render enough colored rows')
        check(pixels['maxStep'] == 1, 'block glyphs retained a blank pixel row between terminal cells')
        check(
            pixels['partialCoralPixels'] == 0,
            f"quadrant glyphs retained blended seams at {odd_metrics}: {pixels['partialCoralPixels']} pixels",
        )
        page.evaluate(
            """() => window.terminalTest.writeTerminalOutput(
                '\\x1b[2J\\x1b[H\\x1b[38;2;215;135;135m'
                + String.fromCodePoint(0x1FB73).repeat(8)
                + '\\x1b[0m'
            )"""
        )
        page.wait_for_timeout(200)
        narrow_stripe_pixels = page.evaluate(
            """() => {
                const canvases = Array.from(document.querySelectorAll(
                    '.terminal-pane.active .xterm-screen canvas'
                ));
                const canvas = canvases[canvases.length - 1];
                if (!canvas) return 0;
                const gl = canvas.getContext('webgl2');
                if (!gl) return 0;
                const rgba = new Uint8Array(canvas.width * canvas.height * 4);
                gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
                let count = 0;
                for (let offset = 0; offset < rgba.length; offset += 4) {
                    const red = rgba[offset];
                    const green = rgba[offset + 1];
                    const blue = rgba[offset + 2];
                    if (red > 40 && green > 20 && blue > 20
                        && red > green && Math.abs(green - blue) <= 2) {
                        count += 1;
                    }
                }
                return count;
            }"""
        )
        check(narrow_stripe_pixels > 0, 'narrow one-eighth glyph collapsed at 7 pixels')
    finally:
        close_context(context)


def test_unicode_provider_keeps_emoji_text_in_separate_cells(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.evaluate(
            """() => window.terminalTest.writeTerminalOutput(
                '\\x1b[2J\\x1b[HA🟩B\\r\\n📁C',
                611
            )"""
        )
        page.wait_for_timeout(100)
        for use_mirror, label in [(False, 'visible'), (True, 'mirror')]:
            first = page.evaluate(
                "([row, mirror]) => window.terminalTest.getActiveTerminalBufferCellsForTest(row, mirror)",
                [0, use_mirror],
            )
            second = page.evaluate(
                "([row, mirror]) => window.terminalTest.getActiveTerminalBufferCellsForTest(row, mirror)",
                [1, use_mirror],
            )
            check(first is not None and second is not None, f'{label} emoji fixture did not reach the buffer')
            check(first[0] == {'chars': 'A', 'width': 1}, f'{label} ASCII prefix moved unexpectedly')
            check(first[1] == {'chars': '🟩', 'width': 2}, f'{label} colored emoji was not two cells wide')
            check(first[2] == {'chars': '', 'width': 0}, f'{label} colored emoji omitted its trailing cell')
            check(first[3] == {'chars': 'B', 'width': 1}, f'{label} text overlapped the colored emoji')
            check(second[0] == {'chars': '📁', 'width': 2}, f'{label} folder emoji was not two cells wide')
            check(second[1] == {'chars': '', 'width': 0}, f'{label} folder emoji omitted its trailing cell')
            check(second[2] == {'chars': 'C', 'width': 1}, f'{label} text overlapped the folder emoji')
    finally:
        close_context(context)


def test_cursor_type_setting_updates_existing_and_new_terminals(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        initial = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(initial['cursorStyle'] == 'block', 'terminal cursor type did not default to block')
        check(initial['mirrorCursorStyle'] == 'block', 'mirror cursor type did not default to block')

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        settings_state = page.evaluate(
            """() => ({
                value: document.getElementById('pref-cursorStyle').value,
                options: Array.from(document.getElementById('pref-cursorStyle').options).map(item => item.value)
            })"""
        )
        check(settings_state['value'] == 'block', 'settings cursor type did not default to block')
        check(settings_state['options'] == ['block', 'underline', 'bar'], 'settings cursor type options changed unexpectedly')

        page.select_option('#pref-cursorStyle', 'underline')
        page.click('#settings-save')
        page.wait_for_function(
            "() => window.terminalTest.getActiveTerminalOptions()?.cursorStyle === 'underline'",
            timeout=5000,
        )
        updated = page.evaluate(
            """() => ({
                options: window.terminalTest.getActiveTerminalOptions(),
                stored: JSON.parse(localStorage.getItem('terminal.pref.v1')).cursorStyle
            })"""
        )
        check(updated['options']['mirrorCursorStyle'] == 'underline', 'mirror cursor type did not update')
        check(updated['stored'] == 'underline', 'cursor type was not saved to preferences')

        page.click('#new-tab-btn')
        page.wait_for_function("() => window.terminalTest.getTerminalTabsState().tabs.length === 2", timeout=5000)
        new_tab = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(new_tab['cursorStyle'] == 'underline', 'new terminal did not use saved cursor type')
        check(new_tab['mirrorCursorStyle'] == 'underline', 'new mirror terminal did not use saved cursor type')
        tabbed_padding_top = page.evaluate(
            """() => parseFloat(getComputedStyle(
                document.querySelector('.terminal-pane.active .xterm')
            ).paddingTop)"""
        )
        check(tabbed_padding_top == 2, 'tabbed terminal did not keep compact top padding')
    finally:
        close_context(context)


def test_webgl_bar_cursor_is_visible_at_first_column(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        options = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(options['renderer'] == 'webgl', 'terminal did not activate the WebGL renderer')

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        page.select_option('#pref-cursorStyle', 'bar')
        page.click('#settings-save')
        page.wait_for_function(
            "() => window.terminalTest.getActiveTerminalOptions()?.cursorStyle === 'bar'",
            timeout=5000,
        )
        page.evaluate(
            "() => document.querySelector('.terminal-pane.active .xterm-helper-textarea')?.focus()"
        )
        page.wait_for_function("() => window.terminalTest.activeTerminalHasFocus()", timeout=5000)

        edge_spacing = page.evaluate(
            """() => {
                const pane = document.querySelector('.terminal-pane.active');
                const terminal = pane?.querySelector('.xterm');
                const screen = terminal?.querySelector('.xterm-screen');
                if (!pane || !terminal || !screen) return null;
                const paneRect = pane.getBoundingClientRect();
                const screenRect = screen.getBoundingClientRect();
                const style = getComputedStyle(terminal);
                const viewport = terminal.querySelector('.xterm-viewport');
                const scrollbar = getComputedStyle(viewport, '::-webkit-scrollbar');
                const scrollbarTrack = getComputedStyle(viewport, '::-webkit-scrollbar-track');
                const scrollbarThumb = getComputedStyle(viewport, '::-webkit-scrollbar-thumb');
                return {
                    left: screenRect.left - paneRect.left,
                    top: screenRect.top - paneRect.top,
                    paddingLeft: parseFloat(style.paddingLeft),
                    paddingTop: parseFloat(style.paddingTop),
                    scrollbarWidth: parseFloat(scrollbar.width),
                    scrollbarTrackBackground: scrollbarTrack.backgroundColor,
                    scrollbarThumbBackground: scrollbarThumb.backgroundColor
                };
            }"""
        )
        check(edge_spacing is not None, 'terminal edge spacing could not be measured')
        check(edge_spacing['paddingLeft'] == 2, f'terminal left padding changed: {edge_spacing}')
        check(edge_spacing['paddingTop'] == 2, f'terminal top padding changed: {edge_spacing}')
        check(edge_spacing['left'] >= 2, f'terminal screen still touches the left edge: {edge_spacing}')
        check(edge_spacing['top'] >= 2, f'terminal screen still touches the top edge: {edge_spacing}')
        check(edge_spacing['scrollbarWidth'] == 14, f'terminal scrollbar width changed: {edge_spacing}')
        check(
            edge_spacing['scrollbarTrackBackground'] == 'rgba(0, 0, 0, 0)',
            f'terminal scrollbar track is not transparent: {edge_spacing}',
        )
        check(
            edge_spacing['scrollbarThumbBackground'] == 'rgb(68, 68, 68)',
            f'terminal scrollbar thumb color changed: {edge_spacing}',
        )

        def count_cursor_pixels(cursor_column):
            cursor_move = '' if cursor_column == 0 else f'\x1b[{cursor_column}C'
            page.evaluate(
                "payload => window.terminalTest.writeTerminalOutput(payload)",
                f'\x1b[6 q\x1b[2J\x1b[H\x1b[?25h{cursor_move}',
            )
            page.wait_for_timeout(100)
            return page.evaluate(
                """column => {
                    const canvases = Array.from(document.querySelectorAll(
                        '.terminal-pane.active .xterm-screen canvas'
                    ));
                    const canvas = canvases[canvases.length - 1];
                    const cells = window.terminalTest.getActiveTerminalBufferCellsForTest(0);
                    if (!canvas || !cells || !cells.length) return { error: 'missing_canvas' };
                    const gl = canvas.getContext('webgl2');
                    if (!gl) return { error: 'missing_webgl_context' };
                    const rgba = new Uint8Array(canvas.width * canvas.height * 4);
                    gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
                    const cellWidth = canvas.width / cells.length;
                    const startX = Math.floor(column * cellWidth);
                    const endX = Math.min(canvas.width, Math.ceil(startX + 4));
                    let count = 0;
                    const brightColumns = new Set();
                    for (let y = 0; y < canvas.height; y += 1) {
                        for (let x = startX; x < endX; x += 1) {
                            const offset = (y * canvas.width + x) * 4;
                            if (rgba[offset] > 200 && rgba[offset + 1] > 200
                                && rgba[offset + 2] > 200 && rgba[offset + 3] > 0) {
                                count += 1;
                                brightColumns.add(x);
                            }
                        }
                    }
                    return {
                        count,
                        brightColumnCount: brightColumns.size,
                        cellWidth,
                        canvasWidth: canvas.width,
                        canvasHeight: canvas.height
                    };
                }""",
                cursor_column,
            )

        first_column = count_cursor_pixels(0)
        second_column = count_cursor_pixels(1)
        check(not first_column.get('error'), f"could not inspect first-column cursor: {first_column}")
        check(not second_column.get('error'), f"could not inspect second-column cursor: {second_column}")
        check(second_column['count'] > 0, f"bar cursor fixture was not visible at column 1: {second_column}")
        check(first_column['count'] > 0, f"bar cursor was invisible at column 0: {first_column}")
    finally:
        close_context(context)


def test_osc_title_updates_fixed_status_column(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        initial_tab_title = page.evaluate(
            """() => window.terminalTest.getTerminalTabsState().tabs
                .find(tab => tab.active)?.title"""
        )
        initial_status = page.evaluate(
            """() => ({
                itemHidden: document.getElementById('terminal-title-item').hidden,
                separatorHidden: document.getElementById('terminal-title-separator').hidden
            })"""
        )
        check(initial_status['itemHidden'] is True, 'empty OSC title column remained visible')
        check(initial_status['separatorHidden'] is True, 'empty OSC title separator remained visible')

        page.evaluate(
            "payload => window.terminalTest.writeTerminalOutput(payload)",
            '\x1b]2;Codex - standterm\x07',
        )
        page.wait_for_function(
            "() => document.getElementById('terminal-title').innerText === 'Codex - standterm'",
            timeout=5000,
        )
        status_layout = page.evaluate(
            """() => {
                const item = document.getElementById('terminal-title-item');
                const style = getComputedStyle(item);
                const size = document.getElementById('terminal-size').closest('.sb-item');
                return {
                    width: style.width,
                    flexBasis: style.flexBasis,
                    beforeSize: item.nextElementSibling?.nextElementSibling === size
                };
            }"""
        )
        check(status_layout['width'] == '220px', f"OSC title column width changed: {status_layout}")
        check(status_layout['flexBasis'] == '220px', f"OSC title column is not fixed: {status_layout}")
        check(status_layout['beforeSize'] is True, 'OSC title column is not immediately before size')

        title_state = page.evaluate(
            """() => ({
                text: document.getElementById('terminal-title').innerText,
                tooltip: document.getElementById('terminal-title-item').title,
                itemHidden: document.getElementById('terminal-title-item').hidden,
                separatorHidden: document.getElementById('terminal-title-separator').hidden,
                tabTitle: window.terminalTest.getTerminalTabsState().tabs.find(tab => tab.active)?.title
            })"""
        )
        check(title_state['tooltip'] == 'Codex - standterm', 'OSC 2 title tooltip did not preserve the full title')
        check(title_state['itemHidden'] is False, 'non-empty OSC title column remained hidden')
        check(title_state['separatorHidden'] is False, 'non-empty OSC title separator remained hidden')
        check(title_state['tabTitle'] == initial_tab_title, 'OSC 2 unexpectedly changed the StandTerm tab label')

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        check(
            page.locator('#pref-showTerminalTitleInStatusBar').is_checked() is True,
            'OSC title status preference did not default on',
        )
        page.uncheck('#pref-showTerminalTitleInStatusBar')
        page.click('#settings-save')
        page.wait_for_function(
            """() => document.getElementById('terminal-title-item').hidden
                && document.getElementById('terminal-title-separator').hidden""",
            timeout=5000,
        )
        stored_preference = page.evaluate(
            "() => JSON.parse(localStorage.getItem('terminal.pref.v1')).showTerminalTitleInStatusBar"
        )
        check(stored_preference is False, 'OSC title status preference was not persisted off')

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        page.check('#pref-showTerminalTitleInStatusBar')
        page.click('#settings-save')
        page.wait_for_function(
            """() => !document.getElementById('terminal-title-item').hidden
                && document.getElementById('terminal-title').innerText === 'Codex - standterm'""",
            timeout=5000,
        )

        long_title = 'This is a very very long terminal title that must be truncated in the fixed status column'
        page.evaluate(
            "payload => window.terminalTest.writeTerminalOutput(payload)",
            f'\x1b]2;{long_title}\x07',
        )
        page.wait_for_function(
            "expected => document.getElementById('terminal-title').innerText === expected",
            arg=long_title,
            timeout=5000,
        )
        overflow_state = page.evaluate(
            """() => {
                const item = document.getElementById('terminal-title-item');
                const style = getComputedStyle(item);
                return {
                    overflowed: item.scrollWidth > item.clientWidth,
                    textOverflow: style.textOverflow,
                    tooltip: item.title
                };
            }"""
        )
        check(overflow_state['overflowed'] is True, 'long OSC title did not overflow the fixed column')
        check(overflow_state['textOverflow'] == 'ellipsis', 'long OSC title did not use ellipsis')
        check(overflow_state['tooltip'] == long_title, 'long OSC title tooltip was truncated')

        page.evaluate(
            "payload => window.terminalTest.writeTerminalOutput(payload)",
            '\x1b]0;Vim workspace\x1b\\',
        )
        page.wait_for_function(
            "() => document.getElementById('terminal-title').innerText === 'Vim workspace'",
            timeout=5000,
        )
        page.evaluate(
            "payload => window.terminalTest.writeTerminalOutput(payload)",
            '\x1b]2;\x07',
        )
        page.wait_for_function(
            """() => document.getElementById('terminal-title').innerText === ''
                && document.getElementById('terminal-title-item').hidden
                && document.getElementById('terminal-title-separator').hidden""",
            timeout=5000,
        )
    finally:
        close_context(context)


def test_settings_server_tab_loads_readonly_snapshot(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language)
    try:
        def text(key):
            return page.evaluate("key => StandTermI18n.create(StandTermMessages, document.documentElement.lang).t(key)", key)

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="server"]')
        page.wait_for_function(
            """expected => document.querySelector('#server-settings-mutable-controls .server-setting-input') !== null
                && document.getElementById('server-settings-status').textContent === expected""",
            arg=text('settings.server.writable'),
            timeout=5000,
        )
        state = page.evaluate(
            """() => ({
                version: document.getElementById('server-settings-version').textContent,
                view: document.getElementById('server-cap-settings-view').textContent,
                low: document.getElementById('server-cap-settings-update-low').textContent,
                high: document.getElementById('server-cap-settings-update-high').textContent,
                selectedDefault: document.getElementById('server-setting-default-connection-select').value,
                mutableKeys: Array.from(
                    document.querySelectorAll('#server-settings-mutable-controls .settings-row[data-setting-key]')
                ).map(element => element.dataset.settingKey),
                uartBaud: document.querySelector(
                    '#server-settings-mutable-controls .server-setting-input[data-setting-key="uart.default_baud_rate"]'
                )?.value,
                localShellDefault: document.querySelector(
                    '#server-settings-mutable-controls .server-setting-input[data-setting-key="local_shell.default_kind"]'
                )?.value,
                localShellSelector: document.getElementById('local-shell-kind')?.value,
                schemaKeys: Array.from(
                    document.querySelectorAll('#server-settings-schema li[data-setting-key]')
                ).map(element => element.dataset.settingKey),
                connectionCount: document.querySelectorAll('#server-settings-connections li').length
            })"""
        )
        check(state['version'].isdigit() and int(state['version']) >= 1, 'settings server tab did not show settings version')
        check(page.inner_text('#server-settings-status') == text('settings.server.writable'), 'settings server tab did not localize write capability')
        check(state['view'] == text('settings.server.allowed'), 'settings server tab did not show view capability')
        check(state['low'] == text('settings.server.allowed'), 'settings server tab did not expose local low-risk writes')
        check(state['high'] == text('settings.server.denied'), 'settings server tab exposed high-risk writes')
        check(state['selectedDefault'], 'settings server tab did not populate default connection control')
        check('default_connection_type' in state['mutableKeys'], 'settings server tab did not render core mutable control')
        check('uart.default_baud_rate' in state['mutableKeys'], 'settings server tab did not render UART mutable control')
        check(state['uartBaud'], 'settings server tab did not populate UART baud control')
        check('local_shell.default_kind' in state['mutableKeys'], 'settings server tab did not render Local Shell default control')
        check(state['localShellDefault'] in {'bash', 'cmd', 'powershell'}, 'settings server tab did not populate Local Shell default control')
        check(state['localShellSelector'] == state['localShellDefault'], 'Local Shell selector did not start on runtime default')
        check('uart.remote_access' in state['schemaKeys'], 'settings server tab did not expose high-risk schema read-only')
        check(state['connectionCount'] > 0, 'settings server tab did not list connection types')
        clear_emitted(page)
        selected_shell_kind = page.evaluate(
            """() => {
                const input = document.querySelector(
                    '#server-settings-mutable-controls .server-setting-input[data-setting-key="local_shell.default_kind"]'
                );
                const apply = document.querySelector(
                    '#server-settings-mutable-controls button[data-setting-key="local_shell.default_kind"]'
                );
                const option = Array.from(input.options).find(item => item.value !== input.value) || input.options[0];
                input.value = option.value;
                input.dispatchEvent(new Event('change', { bubbles: true }));
                apply.click();
                return option.value;
            }"""
        )
        page.wait_for_function(
            """() => window.terminalTest.getEmitted().some(entry => (
                entry.event === 'settings_update_request'
                && entry.args?.[0]?.setting_key === 'local_shell.default_kind'
            ))""",
            timeout=5000,
        )
        local_shell_payload = get_emitted(page, 'settings_update_request')[-1]['args'][0]
        check(local_shell_payload['setting_key'] == 'local_shell.default_kind', 'Local Shell update did not use typed setting_key')
        check(local_shell_payload['value'] == selected_shell_kind, 'Local Shell update did not send selected shell kind')
        check(local_shell_payload['expected_version'] == int(state['version']), 'Local Shell update did not bind displayed settings version')
        check(local_shell_payload.get('expected_schema_digest'), 'Local Shell update did not include expected schema digest')
        page.wait_for_function(
            """({target, version}) => document.querySelector(
                    '#server-settings-mutable-controls .server-setting-input[data-setting-key="local_shell.default_kind"]'
                )?.value === target
                && document.getElementById('local-shell-kind')?.value === target
                && Number(document.getElementById('server-settings-version').textContent) > version""",
            arg={'target': selected_shell_kind, 'version': int(state['version'])},
            timeout=10000,
        )
        clear_emitted(page)
        uart_version = int(page.inner_text('#server-settings-version'))
        selected_baud = page.evaluate(
            """() => {
                const input = document.querySelector(
                    '#server-settings-mutable-controls .server-setting-input[data-setting-key="uart.default_baud_rate"]'
                );
                const apply = document.querySelector(
                    '#server-settings-mutable-controls button[data-setting-key="uart.default_baud_rate"]'
                );
                const option = Array.from(input.options).find(item => item.value !== input.value) || input.options[0];
                input.value = option.value;
                input.dispatchEvent(new Event('change', { bubbles: true }));
                apply.click();
                return option.value;
            }"""
        )
        page.wait_for_function(
            """() => window.terminalTest.getEmitted().some(entry => (
                entry.event === 'settings_update_request'
                && entry.args?.[0]?.setting_key === 'uart.default_baud_rate'
            ))""",
            timeout=5000,
        )
        update_payload = get_emitted(page, 'settings_update_request')[-1]['args'][0]
        check(update_payload['setting_key'] == 'uart.default_baud_rate', 'UART update did not use typed setting_key')
        check(int(update_payload['value']) == int(selected_baud), 'UART update did not send selected baud value')
        check(update_payload['expected_version'] == uart_version, 'UART update did not bind displayed settings version')
        check(update_payload.get('expected_schema_digest'), 'UART update did not include expected schema digest')
    finally:
        close_context(context)


def test_connection_diagnostics_are_session_scoped_and_redacted(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    context, page = new_page(browser, access_url, ui_language)
    try:
        token = urllib.parse.parse_qs(urllib.parse.urlparse(access_url).query)['token'][0]
        page.evaluate("() => window.dispatchEvent(new Event('offline'))")
        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="diagnostics"]')
        state = page.evaluate(
            """() => ({
                entries: window.terminalTest.getConnectionDiagnostics(),
                text: document.getElementById('connection-diagnostics-log').value,
                status: document.getElementById('connection-diagnostics-status').textContent
            })"""
        )
        events = [entry['event'] for entry in state['entries']]
        check('diagnostics.ready' in events, 'diagnostics did not record initialization')
        check('socket.connect' in events, 'diagnostics did not record socket connection')
        check('terminal.list' in events, 'diagnostics did not record terminal list count')
        check('page.offline' in events, 'diagnostics did not record browser offline event')
        check('Launcher Session ID:' in state['text'], 'diagnostics omitted launcher session ID')
        check(token not in state['text'], 'diagnostics exposed the access token')
        check(state['status'] == message(page, 'browser.diagnostics.count', {'count':len(state['entries'])}), 'diagnostics did not show event count')

        page.evaluate(
            """() => {
                const key = 'standterm-connection-diagnostics-v1';
                const entries = JSON.parse(sessionStorage.getItem(key));
                entries.push({
                    at: new Date().toISOString(),
                    event: 'test.injected',
                    launcher_session_id: 'fake\\nentry',
                    details: { message: 'https://example.invalid/?token=should-not-leak' }
                });
                sessionStorage.setItem(key, JSON.stringify(entries));
            }"""
        )

        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        restored_events = page.evaluate(
            "() => window.terminalTest.getConnectionDiagnostics().map(entry => entry.event)"
        )
        check('page.offline' in restored_events, 'diagnostics did not survive a same-tab reload')
        restored_text = page.evaluate(
            "() => document.getElementById('connection-diagnostics-log').value"
        )
        check('should-not-leak' not in restored_text, 'diagnostics did not redact a stored token')
        check('example.invalid' not in restored_text, 'diagnostics did not redact a stored URL')

        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="diagnostics"]')
        page.click('#connection-diagnostics-clear')
        cleared = page.evaluate(
            """() => ({
                entries: window.terminalTest.getConnectionDiagnostics(),
                status: document.getElementById('connection-diagnostics-status').textContent
            })"""
        )
        check(cleared['entries'] == [], 'diagnostics clear did not remove stored events')
        check(cleared['status'] == message(page, 'browser.diagnostics.count', {'count':0}), 'diagnostics clear did not update status')
    finally:
        close_context(context)


def test_settings_access_recovery_fetches_access_url_on_demand(browser, access_url, ui_language='en'):
    from settings_transfer_i18n_browser_smoke import message
    parsed = urllib.parse.urlparse(access_url)
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    context, page = new_page(browser, access_url, ui_language)
    try:
        page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
            configurable:true,value:{writeText:async () => {}}
        })""")
        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="server"]')
        page.wait_for_selector('#server-access-show-btn', timeout=5000)
        initial_state = page.evaluate(
            """() => ({
                status: document.getElementById('server-access-status').textContent,
                display: getComputedStyle(document.getElementById('server-access-url')).display,
                text: document.getElementById('server-access-url').textContent,
                location: window.location.href
            })"""
        )
        check('token=' not in initial_state['location'], 'access token remained in app URL before recovery action')
        check(initial_state['display'] == 'none', 'access URL was visible before explicit reveal')
        check(token not in initial_state['text'], 'access URL was rendered before explicit reveal')

        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#server-access-show-btn')
        page.wait_for_function(
            "token => document.getElementById('server-access-url')?.textContent.includes(token)",
            arg=token,
            timeout=5000,
        )
        revealed = page.evaluate(
            """() => ({
                status: document.getElementById('server-access-status').textContent,
                text: document.getElementById('server-access-url').textContent,
                location: window.location.href
            })"""
        )
        check(revealed['status'] == message(page, 'browser.server_access.shown'), 'access URL reveal did not update status')
        check(access_url in revealed['text'], 'revealed access URL did not match server access URL')
        check('token=' not in revealed['location'], 'access URL reveal modified browser location')

        page.click('#server-access-copy-btn')
        page.wait_for_function(
            "expected => document.getElementById('server-access-status')?.textContent === expected",
            arg=message(page, 'browser.server_access.copied'),
            timeout=5000,
        )
    finally:
        close_context(context)


def test_access_url_token_is_remembered_only_for_recovery(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        state = page.evaluate(
            """() => ({
                location: window.location.href,
                remembered: window.terminalTest.hasRememberedAccessToken()
            })"""
        )
        check('token=' not in state['location'], 'access token remained in app URL')
        check(state['remembered'] is True, 'access URL token was not remembered for recovery')

        page.evaluate("() => window.terminalTest.showSessionRecoveryForTest('Session expired.')")
        page.wait_for_selector('#session-recovery-modal.open', timeout=5000)
        display = page.evaluate(
            "() => getComputedStyle(document.getElementById('session-recovery-remembered-token')).display"
        )
        check(display != 'none', 'remembered-token recovery button was not shown')
    finally:
        close_context(context)


def test_connection_controls_follow_start_fields_without_legacy_payload(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        state = page.evaluate(
            """() => {
                const policy = window.terminalTest.getTerminalPolicy();
                const ssh = policy.connection_options.find(item => item.connection_type === 'ssh');
                const localShell = policy.connection_options.find(item => item.connection_type === 'local_shell');
                const uart = policy.connection_options.find(item => item.connection_type === 'uart');
                if (!ssh || !localShell || !uart) return null;

                ssh.start_fields = [
                    { name: 'host', value_type: 'string', input_type: 'text', default_value: 'schema-host' },
                    { name: 'port', value_type: 'integer', input_type: 'text', default_value: 2022 },
                    { name: 'username', value_type: 'string', input_type: 'text', default_value: 'schema-user' },
                    { name: 'password', value_type: 'string', input_type: 'password', secret: true }
                ];
                delete localShell.shell_options;
                delete localShell.default_shell_kind;
                localShell.start_fields = [{
                    name: 'local_shell_kind',
                    value_type: 'enum',
                    input_type: 'select',
                    default_value: 'beta',
                    options: [
                        { value: 'alpha', label: 'Alpha' },
                        { value: 'beta', label: 'Beta' }
                    ]
                }];
                delete uart.baud_rates;
                delete uart.default_baud_rate;
                uart.available_ports = [
                    { device: 'COM3', label: 'COM3 (Windows)', backend: 'windows' },
                    { device: '/dev/ttyUSB0', label: '/dev/ttyUSB0 (WSL)', backend: 'wsl' }
                ];
                uart.start_fields = [
                    { name: 'serial_port', value_type: 'string', input_type: 'text', default_value: '' },
                    {
                        name: 'baud_rate',
                        value_type: 'integer',
                        input_type: 'select',
                        default_value: 9600,
                        options: [
                            { value: 9600, label: '9600' },
                            { value: 115200, label: '115200' }
                        ]
                    }
                ];
                window.terminalTest.applyTerminalPolicy(policy);
                return {
                    host: document.getElementById('host').value,
                    port: document.getElementById('port').value,
                    username: document.getElementById('username').value,
                    localShell: document.getElementById('local-shell-kind').value,
                    localShellOptions: Array.from(document.getElementById('local-shell-kind').options).map(item => item.value),
                    uartPortSelectDisplay: document.getElementById('uart-port-select').style.display,
                    uartPortOptions: Array.from(document.getElementById('uart-port-select').options).map(item => item.value),
                    uartPortLabels: Array.from(document.getElementById('uart-port-select').options).map(item => item.text),
                    uartPort: document.getElementById('uart-port-select').value,
                    uartManualDisplay: document.getElementById('uart-port').style.display,
                    uartManualValue: document.getElementById('uart-port').value,
                    uartBaud: document.getElementById('uart-baud').value,
                    uartBaudOptions: Array.from(document.getElementById('uart-baud').options).map(item => item.value)
                };
            }"""
        )
        check(state is not None, 'connection policy did not expose expected backend options')
        check(state['host'] == 'schema-host', 'SSH host did not use start_fields default')
        check(state['port'] == '2022', 'SSH port did not use start_fields default')
        check(state['username'] == 'schema-user', 'SSH username did not use start_fields default')
        check(state['localShellOptions'] == ['alpha', 'beta'], 'Local Shell options did not use start_fields')
        check(state['localShell'] == 'beta', 'Local Shell default did not use start_fields')
        check(state['uartPortSelectDisplay'] != 'none', 'UART port selector did not render detected ports')
        check(state['uartPortOptions'] == ['COM3', '/dev/ttyUSB0', '__manual__'], 'UART port selector did not list detected ports and manual fallback')
        check(state['uartPortLabels'][:2] == ['COM3 (Windows)', '/dev/ttyUSB0 (WSL)'], 'UART port selector did not label port sources')
        check(state['uartPort'] == 'COM3', 'UART port selector did not default to first detected port')
        check(state['uartManualDisplay'] == 'none', 'UART manual input was visible while a detected port was selected')
        check(state['uartManualValue'] == 'COM3', 'UART manual backing value did not mirror selected port')
        check(state['uartBaudOptions'] == ['9600', '115200'], 'UART baud options did not use start_fields')
        check(state['uartBaud'] == '9600', 'UART baud default did not use start_fields')

        manual_state = page.evaluate(
            """() => {
                const selector = document.getElementById('uart-port-select');
                const input = document.getElementById('uart-port');
                selector.value = '__manual__';
                selector.dispatchEvent(new Event('change', { bubbles: true }));
                input.value = '/dev/ttyUSB1';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                return {
                    selector: selector.value,
                    input: input.value,
                    inputDisplay: input.style.display
                };
            }"""
        )
        check(manual_state['selector'] == '__manual__', 'UART manual selector value was not retained')
        check(manual_state['input'] == '/dev/ttyUSB1', 'UART manual input did not accept WSL device path')
        check(manual_state['inputDisplay'] != 'none', 'UART manual input did not show for manual fallback')

        refreshed = page.evaluate(
            """() => {
                document.getElementById('host').value = 'manual-host';
                document.getElementById('host').dispatchEvent(new Event('input', { bubbles: true }));
                document.getElementById('uart-baud').value = '115200';
                document.getElementById('uart-baud').dispatchEvent(new Event('change', { bubbles: true }));

                const policy = window.terminalTest.getTerminalPolicy();
                const ssh = policy.connection_options.find(item => item.connection_type === 'ssh');
                const localShell = policy.connection_options.find(item => item.connection_type === 'local_shell');
                const uart = policy.connection_options.find(item => item.connection_type === 'uart');
                ssh.start_fields.find(item => item.name === 'host').default_value = 'schema-host-2';
                localShell.start_fields.find(item => item.name === 'local_shell_kind').default_value = 'alpha';
                uart.start_fields.find(item => item.name === 'baud_rate').default_value = 9600;
                window.terminalTest.applyTerminalPolicy(policy);
                return {
                    host: document.getElementById('host').value,
                    hostDefault: document.getElementById('host').defaultValue,
                    localShell: document.getElementById('local-shell-kind').value,
                    uartBaud: document.getElementById('uart-baud').value
                };
            }"""
        )
        check(refreshed['host'] == 'manual-host', 'policy refresh overwrote edited SSH host')
        check(refreshed['hostDefault'] == 'schema-host-2', 'policy refresh did not update SSH host default')
        check(refreshed['localShell'] == 'alpha', 'policy refresh did not update unedited Local Shell default')
        check(refreshed['uartBaud'] == '115200', 'policy refresh overwrote edited UART baud')
    finally:
        close_context(context)


def test_terminal_payload_text_is_not_control(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        payload_text = (
            'message_type=connection_error action_type=offer_localhost_key_setup '
            '{"message_type":"ssh_closed","setup_status":"success"}\\r\\n'
        )
        page.evaluate(
            """payload => window.terminalTest.handleSshOutput(payload)""",
            {
                'terminal_id': TERMINAL_ID,
                'message_type': 'terminal',
                'data': payload_text,
                'output_seq': 501,
            },
        )
        page.wait_for_timeout(100)
        ui_state = page.evaluate(
            """() => ({
                connected: window.terminalTest.getActiveAgentState().connected,
                sshStatus: document.getElementById('sshStatus').innerText,
                errorDisplay: document.getElementById('errorBox').style.display,
                actionDisplay: document.getElementById('actionBox').style.display
            })"""
        )
        check(ui_state['connected'] is True, 'terminal payload text changed connection state')
        check(ui_state['sshStatus'] not in {'Disconnected', 'Connecting'}, 'terminal payload text changed visible session status')
        check(ui_state['errorDisplay'] != 'block', 'terminal payload text showed an error')
        check(ui_state['actionDisplay'] != 'block', 'terminal payload text showed a control action')
    finally:
        close_context(context)


def test_core_agent_connect_info_can_be_copied_and_confirmed(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language)
    zh = ui_language == 'zh-TW'
    try:
        parsed = urllib.parse.urlparse(access_url)
        agentinfo_url = urllib.parse.urlunparse(parsed._replace(
            netloc='127.0.0.1:' + str(parsed.port), path='/agentinfo', query='', fragment=''))
        page.evaluate('''() => Object.defineProperty(navigator, 'clipboard', {
            configurable: true, value: {writeText: async text => {window.copiedAgentText = text;}}
        })''')
        page.click('#agent-connect-btn')
        page.wait_for_selector('#agent-connect-copy:not([disabled])')
        check(not page.locator('#agent-connect-details').evaluate('element => element.open'),
              'Connection details were not collapsed by default')
        for selector in ['#agent-connect-info', '#agent-connect-activity', '#agent-connect-copy-url',
                         '#agent-connect-refresh', '#agent-connect-open-panel']:
            check(page.locator(selector).is_hidden(), 'Secondary connection content remained visible: ' + selector)
        check(page.locator('#agent-connect-message').is_hidden(), 'Ready state repeated the introduction')
        check(page.input_value('#agent-connect-url') == agentinfo_url, 'Core did not expose its Agent Info URL')
        info_text = page.input_value('#agent-connect-info')
        check('Core host environment' in info_text and 'Run discover, then hello' in info_text,
              'Connect Info did not explain where and how to confirm access')
        check('--token' not in info_text and 'agt_' not in info_text, 'Connect Info exposed a token')
        check(page.locator('.terminal-tab.agent-token-active').count() == 0, 'Reading Connect Info minted a token')
        check(('目前沒有有效授權' if zh else 'No active grants') in page.text_content('#agent-connect-activity'), 'Missing authorization was not explained')
        check(page.locator('#agent-tunnel-btn').is_hidden(), 'Local shell offered an SSH tunnel')
        check(page.locator('#agent-remote-info-btn').count() == 0, 'A separate remote Agent Info button remains')
        check(page.inner_text('#agent-connect-btn') == ('Agent 連線' if zh else 'Agent connection'), 'Info button did not identify the Agent connection workflow')
        check(page.inner_text('#agent-connect-copy') == ('複製連線指引' if zh else 'Copy Prompt'), 'Local info did not offer a prompt')
        page.click('#agent-connect-copy')
        page.wait_for_function('text => window.copiedAgentText === text', arg=info_text)
        page.locator('#agent-connect-details summary').focus()
        page.keyboard.press('Enter')
        check(page.locator('#agent-connect-info').is_visible(), 'Keyboard disclosure did not reveal the prompt')
        page.click('#agent-connect-copy-url')
        page.wait_for_function('url => window.copiedAgentText === url', arg=agentinfo_url)
        page.focus('#agent-connect-url')
        page.evaluate("() => { window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus')); }")
        page.wait_for_timeout(150)
        check(page.evaluate("() => document.activeElement.id === 'agent-connect-url'"),
              'Window focus stole focus from the Agent Info URL')
        page.click('#agent-connect-open-panel')
        page.wait_for_selector('#agent-panel.visible')
        page.click('#agent-access-toggle-btn')
        wait_for_agent(page, "state.mode === 'observe'")
        page.click('#agent-external-token-btn')
        page.wait_for_selector('.terminal-tab.agent-token-active', state='attached')
        page.click('#agent-connect-btn')
        check(not page.locator('#agent-connect-details').evaluate('element => element.open'),
              'Reopening connection details retained the expanded view')
        page.click('#agent-connect-details summary')
        page.wait_for_function("text => document.getElementById('agent-connect-activity').innerText.includes(text)",
                               arg='main: ' + ('等待 Agent' if zh else 'waiting for agent'))
        with urllib.request.urlopen(agentinfo_url, timeout=5) as response:
            info = json.load(response)
        hello = subprocess.run([
            info['python_path'], info['scripts']['agent_cli'], '--agentinfo', agentinfo_url,
            '--terminal', 'main', 'hello',
        ], capture_output=True, text=True, timeout=15)
        check(hello.returncode == 0, 'The copied Agent Info URL could not run the shared hello helper')
        page.wait_for_function("text => document.getElementById('agent-connect-activity').innerText.includes(text)",
                               arg='最近一次通過驗證的請求' if zh else 'last authenticated request')
        page.click('#agent-connect-details summary')
        page.evaluate("() => { navigator.clipboard.writeText = async () => { throw new Error('Denied'); }; }")
        page.click('#agent-connect-copy')
        page.wait_for_function("text => document.getElementById('agent-connect-message').innerText.includes(text)",
                               arg='手動複製' if zh else 'copy it manually')
        check(page.locator('#agent-connect-message').is_visible(), 'Copy failure was hidden in collapsed details')
        check(page.locator('#agent-connect-info').is_visible(), 'Copy failure did not reveal the selected prompt')
        selection = page.locator('#agent-connect-info').evaluate('field => field.value.slice(field.selectionStart, field.selectionEnd)')
        check(selection == page.input_value('#agent-connect-info'), 'Clipboard fallback did not select the prompt')
        page.click('#agent-connect-close')
    finally:
        close_context(context)


def test_agent_tunnel_uses_panel_permissions_and_keeps_focus(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language=ui_language)
    def ui_text(key, params=None):
        return page.evaluate("""({key,params}) =>
            StandTermI18n.create(StandTermMessages, document.documentElement.lang).t(key, params)
        """, {'key':key,'params':params or {}})
    token_requests = []
    page.on('request', lambda request: token_requests.append(request.url)
            if '/agent/external/token' in request.url else None)
    try:
        attach_agent(page)
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        page.evaluate('''() => window.terminalTest.applyTerminalListForTest({terminals: [
            {terminal_id: 'main', connected: true, connection_type: 'ssh', terminal_label: '<img src=x>'},
            {terminal_id: 'third', connected: true, connection_type: 'ssh', terminal_label: 'Unenrolled'}
        ]})''')
        check(page.locator('#agent-connect-btn').is_hidden(), 'SSH info appeared before tunnel setup')
        page.click('#agent-tunnel-btn')
        page.wait_for_selector('#agent-tunnel-apply:not([disabled])')
        check(page.locator('#agent-tunnel-targets input').count() == 0,
              'tunnel duplicated Agent Panel permission controls')
        check(page.locator('#agent-tunnel-targets img').count() == 0,
              'terminal label was interpreted as HTML')
        check('Unenrolled' not in page.inner_text('#agent-tunnel-targets'), 'Disabled tab appeared authorized')
        page.evaluate('() => window.terminalTest.clearEmitted()')
        page.click('#agent-tunnel-apply')
        page.wait_for_selector('#agent-tunnel-apply:not([disabled])')
        requests = page.evaluate("() => window.terminalTest.getEmitted().filter(e => e.event === 'agent_tunnel')")
        check(len(requests) == 1, 'Apply sent duplicate tunnel operations')
        request = requests[0]['args'][0]
        check(request == {'terminal_id':'main','operation':'apply'}, 'carrier scope is wrong')
        check('targets' not in request, 'Browser supplied a second authorization list')
        check(not any(event['event'] in {'agent_attach','agent_detach','agent_mode_set','agent_pause'}
                      for event in get_emitted(page)), 'Tunnel setup changed Agent Panel permissions')
        page.focus('#agent-tunnel-close')
        page.evaluate("() => { window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus')); }")
        page.wait_for_timeout(150)
        check(page.evaluate("() => document.activeElement.id === 'agent-tunnel-close'"),
              'window focus stole focus from tunnel controls')
        check(page.locator('#agent-tunnel-info').is_hidden(), 'failed setup advertised usable Connect Info')
        # Backend integration tests cover real SSH; this fixture exercises the ready-state controls.
        remote_url = 'http://127.0.0.1:43210/agentinfo'
        remote_prompt = 'Run the agent on this SSH host. AgentInfoURL: ' + remote_url
        verified_at = 1700000000
        page.evaluate('payload => window.terminalTest.applyAgentTunnelStatusForTest(payload)', {
            'status': 'ready', 'carrier_id': 'test-tunnel', 'agentinfo_url': remote_url,
            'verified_at': verified_at, 'connect_info': remote_prompt,
            'ssh_context': {'host': '<img src=x>', 'ssh_tab': 'main'},
            'terminal_ids': ['main'], 'terminals': [{'terminal_id': 'main', 'last_request_at': None}],
        })
        check(page.input_value('#agent-tunnel-url') == remote_url, 'Tunnel showed the local Core URL')
        check(page.locator('#agent-connect-btn').is_visible(), 'Ready tunnel did not reveal remote info')
        check(page.locator('#agent-tunnel-carrier img').count() == 0, 'SSH host context was rendered as HTML')
        verified_time = page.evaluate('({stamp,locale}) => new Date(stamp * 1000).toLocaleString(locale)',
                                      {'stamp':verified_at,'locale':ui_language})
        check(page.inner_text('#agent-tunnel-verification') == ui_text('agent.tunnel.verified_at', {'time':verified_time}),
              'Tunnel verification time was not localized')
        check(page.inner_text('#agent-tunnel-activity') == 'main: ' + ui_text('agent.connection.waiting'),
              'Ready falsely confirmed agent access')
        page.evaluate('''() => Object.defineProperty(navigator, 'clipboard', {
            configurable: true, value: {writeText: async text => {window.copiedAgentText = text;}}
        })''')
        page.click('#agent-tunnel-copy-url')
        page.wait_for_function('url => window.copiedAgentText === url', arg=remote_url)
        prompt = page.input_value('#agent-tunnel-info')
        check(prompt == remote_prompt, 'Display localization changed the remote prompt')
        page.click('#agent-tunnel-copy')
        page.wait_for_function('text => window.copiedAgentText === text', arg=prompt)
        for carrier in ('unrelated-tunnel', 'test-tunnel'):
            activity_at = 1700000300
            page.evaluate('payload => window.terminalTest.applyAgentConnectionActivityForTest(payload)', {
                'terminal_id': 'main', 'carrier_id': carrier, 'last_request_at': activity_at,
            })
            activity_time = page.evaluate('({stamp,locale}) => new Date(stamp * 1000).toLocaleString(locale)',
                                          {'stamp':activity_at,'locale':ui_language})
            expected = ui_text('agent.connection.waiting') if carrier == 'unrelated-tunnel' else ui_text(
                'agent.connection.request_at', {'time':activity_time})
            check(page.inner_text('#agent-tunnel-activity') == 'main: ' + expected, 'Activity did not match the current SSH tunnel')
        page.evaluate('() => window.terminalTest.clearEmitted()')
        page.click('#agent-tunnel-check')
        page.wait_for_selector('#agent-tunnel-apply:not([disabled])')
        requests = page.evaluate("() => window.terminalTest.getEmitted().filter(e => e.event === 'agent_tunnel')")
        check(len(requests) == 1 and requests[0]['args'][0]['operation'] == 'check', 'Check did not verify the current tunnel')
        check(page.locator('#agent-tunnel-connection').is_hidden(), 'Failed check retained usable remote connection info')
        check(page.locator('#agent-connect-btn').is_hidden(), 'Failed check retained remote info shortcut')
        page.click('#agent-tunnel-close')
        check(not page.locator('#agent-tunnel-dialog').is_visible(), 'Close did not dismiss tunnel dialog')
        check(not token_requests, 'Agent Tunnel minted a local Agent token')
    finally:
        close_context(context)


def test_remote_agent_info_tracks_ssh_carrier_and_rejects_late_replies(browser, access_url, ui_language='en'):
    context, page = new_page(browser, access_url, ui_language=ui_language)
    try:
        page.evaluate('''() => {
            window.terminalTest.holdAgentTunnelRequestsForTest();
            Object.defineProperty(navigator, 'clipboard', {
                configurable: true, value: {writeText: async text => {window.copiedAgentText = text;}}
            });
            window.terminalTest.applyTerminalListForTest({terminals: [
                {terminal_id: 'main', connected: true, connection_type: 'ssh', terminal_label: 'Host A'},
                {terminal_id: 'second', connected: true, connection_type: 'ssh', terminal_label: 'Host B'}
            ]});
        }''')

        def ready(carrier, port):
            url = 'http://127.0.0.1:' + str(port) + '/agentinfo'
            return {'status': 'ready', 'carrier_id': carrier, 'agentinfo_url': url,
                    'verified_at': time.time(), 'terminal_ids': [], 'terminals': [],
                    'ssh_context': {'host': carrier}, 'connect_info': 'Run on ' + carrier + '. AgentInfoURL: ' + url}

        first, second = ready('host-a', 43210), ready('host-b', 43211)
        page.click('#agent-tunnel-btn')
        page.evaluate('payload => window.terminalTest.completeAgentTunnelRequestForTest(0, payload)', first)
        prompt = page.input_value('#agent-tunnel-info')
        check(prompt == first['connect_info'], 'Display localization changed the remote prompt')
        page.click('#agent-tunnel-copy')
        page.wait_for_function('text => window.copiedAgentText === text', arg=prompt)
        page.click('#agent-tunnel-close')
        page.click('#agent-connect-btn')
        title = page.evaluate("() => StandTermI18n.create(StandTermMessages, document.documentElement.lang).t('agent.connection.title')")
        check(page.inner_text('#agent-tunnel-title') == title, 'Remote shortcut opened the wrong view')
        check(page.locator('#agent-tunnel-setup').is_hidden(), 'Remote info repeated setup controls')
        check(page.locator('#agent-tunnel-copy').is_hidden(), 'Remote shortcut offered a stale cached prompt')
        page.evaluate('payload => window.terminalTest.completeAgentTunnelRequestForTest(1, payload)', first)
        check(page.locator('#agent-tunnel-info').is_hidden(), 'Remote info did not collapse the full prompt')
        for selector in ['#agent-tunnel-manage', '#agent-tunnel-refresh', '#agent-tunnel-check',
                         '#agent-tunnel-copy-url', '#agent-tunnel-carrier']:
            check(page.locator(selector).is_hidden(), 'Remote secondary content remained visible: ' + selector)
        check(page.locator('#agent-tunnel-message').is_hidden(), 'Remote ready state repeated the introduction')
        page.click('#agent-tunnel-copy')
        page.wait_for_function('text => window.copiedAgentText === text', arg=prompt)
        page.click('#agent-tunnel-details summary')
        page.click('#agent-tunnel-manage')
        check(page.locator('#agent-tunnel-setup').is_visible(), 'Manage did not reveal tunnel controls')
        page.click('#agent-tunnel-refresh')
        page.click('#agent-tunnel-close')
        page.click('.terminal-tab[data-terminal-id="second"]')
        check(page.locator('#agent-connect-btn').is_hidden(), 'Host B inherited Host A remote info')
        page.click('#agent-tunnel-btn')
        page.evaluate('payload => window.terminalTest.completeAgentTunnelRequestForTest(3, payload)', second)
        page.evaluate('payload => window.terminalTest.completeAgentTunnelRequestForTest(2, payload)', first)
        check(page.input_value('#agent-tunnel-info') == second['connect_info'], 'Late Host A reply replaced Host B prompt')
        page.evaluate("() => window.terminalTest.applyAgentTunnelStateForTest({terminal_id: 'second', carrier_id: 'old-b', status: 'stopped'})")
        check(page.input_value('#agent-tunnel-info') == second['connect_info'], 'Old carrier stop cleared the new tunnel')
        page.evaluate("() => window.terminalTest.applyAgentTunnelStateForTest({terminal_id: 'second', carrier_id: 'host-b', status: 'stopped'})")
        check(page.locator('#agent-tunnel-copy').is_hidden(), 'Stop retained a usable prompt')
        check(page.locator('#agent-connect-btn').is_hidden(), 'Stop retained the remote shortcut')
        page.click('#agent-tunnel-apply')
        page.evaluate('''() => window.terminalTest.applyTerminalListForTest({terminals: [
            {terminal_id: 'main', connected: true, connection_type: 'ssh'},
            {terminal_id: 'second', connected: false, connection_type: 'ssh'}
        ]})''')
        page.evaluate('payload => window.terminalTest.completeAgentTunnelRequestForTest(4, payload)', second)
        check(page.input_value('#agent-tunnel-info') == '', 'Disconnected SSH accepted a late setup result')
        page.click('#agent-tunnel-close')
        check(page.locator('#agent-connect-btn').is_hidden(), 'Disconnected SSH offered local info as a fallback')
        page.evaluate('''() => window.terminalTest.applyTerminalListForTest({terminals: [
            {terminal_id: 'main', connected: true, connection_type: 'local_shell'},
            {terminal_id: 'second', connected: false, connection_type: 'ssh'}
        ]})''')
        page.click('.terminal-tab[data-terminal-id="main"]')
        check(page.locator('#agent-connect-btn').is_visible(), 'Returning to a local tab did not restore Agent Info')
        page.click('#agent-connect-btn')
        page.wait_for_selector('#agent-connect-copy:not([disabled])')
        check(page.locator('#agent-tunnel-dialog').is_hidden(), 'Local tab opened SSH info')
        check('Core host environment' in page.input_value('#agent-connect-info'), 'Local tab retained the SSH prompt')
        page.click('#agent-connect-close')
    finally:
        close_context(context)


def test_ssh_host_key_prompts_default_to_cancel_and_bind_actions(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
        prompt = {
            'terminal_id': 'main', 'message_type': 'connection_error',
            'error_code': 'ssh_host_key_unknown', 'message': 'SSH host key is unknown.',
            'action_type': 'confirm_ssh_host_key', 'action_id': 'observed-key',
            'action_message': 'Received: ssh-ed25519 SHA256:example\n<img src=x onerror=alert(1)>',
            'action_question': 'Remember this host key?',
        }
        page.evaluate('payload => window.terminalTest.handleSshOutput(payload)', prompt)
        page.wait_for_function("() => document.activeElement.id === 'actionNoBtn'")
        check(page.locator('#actionNoBtn').inner_text() == 'Cancel', 'host key prompt did not default to Cancel')
        check(page.locator('#actionMessage img').count() == 0, 'fingerprint text was interpreted as HTML')
        check('<img' in page.locator('#actionMessage').inner_text(), 'fingerprint text was lost')
        page.evaluate('() => window.terminalTest.clearEmitted()')
        page.click('#actionNoBtn')
        emitted = page.evaluate('() => window.terminalTest.getEmitted()')
        actions = [entry['args'][0] for entry in emitted if entry['event'] == 'ssh_host_key_action']
        check(actions == [{'operation': 'cancel', 'action_id': 'observed-key', 'terminal_id': 'main'}],
              'Cancel did not revoke the displayed action')

        prompt.update(error_code='ssh_host_key_changed', action_message='Saved: SHA256:old\nReceived: SHA256:new')
        page.evaluate('payload => window.terminalTest.handleSshOutput(payload)', prompt)
        page.wait_for_function("() => document.activeElement.id === 'actionNoBtn'")
        emitted = page.evaluate("""() => {
            document.getElementById('host').value = 'edited.example';
            window.terminalTest.clearEmitted();
            document.getElementById('actionYesBtn').click();
            document.getElementById('actionYesBtn').click();
            return window.terminalTest.getEmitted();
        }""")
        actions = [entry['args'][0] for entry in emitted if entry['event'] == 'ssh_host_key_action']
        check(actions == [{'operation': 'confirm', 'action_id': 'observed-key', 'terminal_id': 'main'}],
              'Trust replayed an action or submitted the edited target')
        check(not any(entry['event'] == 'start_ssh' for entry in emitted), 'Trust silently reconnected an edited form')
        page.evaluate("""() => window.terminalTest.handleSshOutput({
            terminal_id: 'main', message_type: 'host_key_result', status: 'success',
            message: 'SSH host key saved. Connect again to continue.'
        })""")
        check('Connect again' in page.locator('#errorBox').inner_text(), 'Trust result omitted next step')

        emitted = page.evaluate("""() => {
            window.terminalTest.clearEmitted();
            document.getElementById('host').value = '192.168.167.254';
            document.getElementById('port').value = '2222';
            document.getElementById('ssh-direct-identity-body').querySelector('button').click();
            return window.terminalTest.getEmitted();
        }""")
        actions = [entry['args'][0] for entry in emitted if entry['event'] == 'ssh_host_identity']
        check(len(actions) == 1 and all(actions[0].get(key) == value for key, value in {
            'operation': 'inspect', 'terminal_id': 'main', 'host': '192.168.167.254', 'port': '2222', 'host_key_alias': ''
        }.items()), 'Fingerprint inspection did not use the current direct host and port')
        page.evaluate("""() => window.terminalTest.handleSshOutput({
            terminal_id: 'main', message_type: 'host_key_prompt', action_type: 'forget_ssh_host_key',
            action_id: 'forget-key', action_message: 'Saved: SHA256:old', action_question: 'Forget this key?'
        })""")
        page.wait_for_function("() => document.activeElement.id === 'actionNoBtn'")
        check(page.locator('#actionYesBtn').inner_text() == 'Forget key', 'Forget prompt used the wrong action label')
        page.evaluate("() => document.getElementById('new-tab-btn').click()")
        check(page.locator('#actionBox').is_hidden(), 'switching terminals left an actionable old prompt')
    finally:
        close_context(context)


def test_ssh_history_records_success_without_saving_profiles(browser, access_url):
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    try:
        page.goto(debug_url(access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        page.evaluate("() => window.terminalTest.setSshSessionState({ profiles: [], history: [] })")

        page.evaluate(
            """() => {
                window.terminalTest.stageSshConnectionForTest({
                    host: 'failed.example', port: '22', username: 'alice',
                    password: 'must-not-persist', saveSession: true
                });
                window.terminalTest.handleSshOutput({
                    terminal_id: 'main', message_type: 'connection_error', message: 'Rejected'
                });
            }"""
        )
        failed_state = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(failed_state['version'] == 2 and not failed_state['profiles'] and not failed_state['history']
              and not failed_state['nodes'], 'failed SSH connection was stored')

        page.evaluate(
            """() => {
                window.terminalTest.stageSshConnectionForTest({
                    host: 'history.example', port: '2222', username: 'alice',
                    password: 'must-not-persist', saveSession: false
                });
                window.terminalTest.handleSshOutput({
                    terminal_id: 'main', message_type: 'ssh_connected',
                    connection_type: 'ssh', terminal_label: 'SSH'
                });
            }"""
        )
        history_only = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(len(history_only['history']) == 1, 'successful SSH connection did not enter history')
        check(history_only['profiles'] == [], 'unchecked Save session created a profile')

        history_disabled = page.evaluate(
            """async () => {
                const before = await window.terminalTest.getSshSessionState();
                await window.terminalTest.recordSuccessfulSshConnectionForTest({
                    host: 'private.example', port: '22', username: 'alice',
                    saveHistory: false, saveSession: false
                });
                return { before, after: await window.terminalTest.getSshSessionState() };
            }"""
        )
        check(
            history_disabled['after'] == history_disabled['before'],
            'disabled Save history retained a successful connection',
        )

        profile_only = page.evaluate(
            """async () => {
                await window.terminalTest.recordSuccessfulSshConnectionForTest({
                    host: 'profile-only.example', port: '22', username: 'alice',
                    saveHistory: false, saveSession: true
                });
                return window.terminalTest.getSshSessionState();
            }"""
        )
        check(len(profile_only['history']) == 1, 'Save session implicitly enabled SSH history')
        check(
            profile_only['profiles'] == [],
            'Successful login saved a profile outside the Connect transaction',
        )
        page.evaluate(
            """async () => window.terminalTest.setSshSessionState({
                profiles: [], history: (await window.terminalTest.getSshSessionState()).history
            })"""
        )

        capped_state = page.evaluate(
            """async () => {
                for (let index = 0; index < 7; index += 1) {
                    await window.terminalTest.recordSuccessfulSshConnectionForTest({
                        host: `host-${index}.example`, port: 22, username: 'alice', saveSession: false
                    });
                }
                return window.terminalTest.getSshSessionState();
            }"""
        )
        check(len(capped_state['history']) == 6, 'SSH history did not cap at six entries')
        check(
            capped_state['history'][0]['host'] == 'host-6.example',
            f'SSH history is not newest first: {capped_state["history"]!r}',
        )
        check('host-0.example' not in [entry['host'] for entry in capped_state['history']], 'SSH history kept an evicted entry')

        saved_state = page.evaluate(
            """async () => {
                await window.terminalTest.recordSuccessfulSshConnectionForTest({
                    host: 'saved.example', port: '2200', username: 'bob',
                    password: 'must-not-persist', saveSession: true
                });
                await window.terminalTest.recordSuccessfulSshConnectionForTest({
                    host: 'SAVED.EXAMPLE', port: 2200, username: 'bob', saveSession: true
                });
                return window.terminalTest.getSshSessionState();
            }"""
        )
        check(saved_state['profiles'] == [], 'Successful history recording implicitly saved a profile')
        serialized = json.dumps(saved_state).lower()
        check('must-not-persist' not in serialized, 'SSH session storage retained a password')
        check('"password":' not in serialized, 'SSH session storage contains a password field')
    finally:
        close_context(context)


def test_ssh_profile_picker_and_settings_save_semantics(browser, access_url):
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    try:
        page.goto(debug_url(access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        page.evaluate(
            """async () => {
                await window.terminalTest.setSshSessionState({
                    profiles: [
                        { id: 'profile-a', sortOrder: 0, name: 'Build Server', host: 'build.example', port: '22', username: 'builder', keyId: null },
                        { id: 'profile-b', sortOrder: 1, name: 'Deploy Server', host: 'deploy.example', port: '2200', username: 'deployer', keyId: null }
                    ],
                    history: [
                        { id: 'history-a', host: 'recent.example', port: '2022', username: 'recent', lastUsedAt: '2026-08-26T00:00:00.000Z' }
                    ]
                });
                const policy = window.terminalTest.getTerminalPolicy();
                policy.force_connection = null;
                policy.default_connection = 'ssh';
                const ssh = policy.connection_options.find(option => option.connection_type === 'ssh');
                ssh.allowed = true;
                ssh.browser_key_allowed = true;
                window.terminalTest.applyTerminalPolicy(policy);
                const sshMode = document.querySelector('input[name="connection_type"][value="ssh"]');
                sshMode.checked = true;
                sshMode.dispatchEvent(new Event('change', { bubbles: true }));
                document.getElementById('controls').style.display = 'block';
                document.getElementById('connection-form').style.display = 'block';
                document.getElementById('ssh-fields').style.display = 'block';
            }"""
        )

        page.evaluate(
            """() => {
                document.getElementById('ssh-session-picker-toggle').click();
                document.querySelector('.ssh-session-picker-entry[data-entry-id="profile-a"]').click();
            }"""
        )
        selected_profile = page.evaluate(
            """() => ({
                host: document.getElementById('host').value,
                port: document.getElementById('port').value,
                username: document.getElementById('username').value,
                password: document.getElementById('password').value,
                indicator: document.getElementById('ssh-profile-indicator').innerText
            })"""
        )
        check(selected_profile == {
            'host': 'build.example', 'port': '22', 'username': 'builder',
            'password': '', 'indicator': 'Profile: Build Server'
        }, 'profile selection did not populate the SSH form safely')
        check(
            page.locator('#ssh-profile-name').get_attribute('maxlength') == '64',
            'SSH profile name input did not expose its 64-character limit',
        )
        check(
            page.evaluate("() => window.terminalTest.getMatchingSshProfileNameForTest()") == 'Build Server',
            'exact SSH profile target did not resolve its label',
        )

        page.evaluate(
            """() => {
                const terminalId = window.terminalTest.getTerminalTabsState().activeTerminalId;
                window.terminalTest.stageSshConnectionForTest({
                    host: 'build.example', port: '22', username: 'builder', profileName: 'Build Server'
                });
                window.terminalTest.handleSshOutput({
                    terminal_id: terminalId, message_type: 'ssh_connected',
                    connection_type: 'ssh', terminal_label: 'SSH - Build Server',
                    ssh_target: {host: 'build.example', port: '22', username: 'builder'}
                });
            }"""
        )
        detailed_label = page.evaluate(
            """() => ({
                tab: document.querySelector('.terminal-tab.active .tab-title').innerText,
                session: document.getElementById('sshStatus').innerText,
                termFieldCount: document.querySelectorAll('#terminal-term').length,
                colorFieldCount: document.querySelectorAll('#terminal-color').length
            })"""
        )
        check(detailed_label['tab'] == 'SSH - Build Server', 'profile label did not replace the SSH tab name')
        check(detailed_label['session'] == 'SSH - Build Server', 'status bar did not show the full SSH profile label')
        check(detailed_label['termFieldCount'] == 0, 'status bar still rendered the removed TERM field')
        check(detailed_label['colorFieldCount'] == 0, 'status bar still rendered the removed color field')

        page.click('#quick-settings')
        check(page.locator('#pref-showDetailedSshLabels').is_checked() is True, 'detailed SSH labels did not default on')
        page.uncheck('#pref-showDetailedSshLabels')
        page.click('#settings-save')
        compact_label = page.evaluate(
            """() => ({
                tab: document.querySelector('.terminal-tab.active .tab-title').innerText,
                session: document.getElementById('sshStatus').innerText
            })"""
        )
        check(compact_label == {'tab': 'SSH', 'session': 'SSH'}, 'detailed SSH label setting did not apply immediately')
        page.click('#quick-settings')
        page.check('#pref-showDetailedSshLabels')
        page.click('#settings-save')

        page.evaluate(
            """() => {
                const port = document.getElementById('port');
                port.value = '2222';
                port.dispatchEvent(new Event('input', { bubbles: true }));
            }"""
        )
        check(
            page.locator('#ssh-profile-indicator').inner_text() == 'Based on: Build Server (modified)',
            'editing a loaded profile was not labeled as a derived Quick Connect draft',
        )
        check(
            page.evaluate("() => window.terminalTest.getMatchingSshProfileNameForTest()") is None,
            'modified SSH target kept the original profile label',
        )
        page.evaluate(
            """() => {
                document.getElementById('ssh-session-picker-toggle').click();
                document.querySelector('.ssh-session-picker-entry[data-entry-id="history-a"]').click();
            }"""
        )
        history_selection = page.evaluate(
            """() => ({
                host: document.getElementById('host').value,
                indicatorDisplay: document.getElementById('ssh-profile-indicator').style.display,
                saveChecked: document.getElementById('ssh-save-session').checked
            })"""
        )
        check(history_selection['host'] == 'recent.example', 'history selection did not populate the SSH form')
        check(history_selection['indicatorDisplay'] == 'none', 'history selection displayed a profile label')
        check(history_selection['saveChecked'] is False, 'history selection enabled Save session implicitly')

        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        page.wait_for_function("() => !document.getElementById('ssh-profile-save').disabled")
        preloaded_editor = page.evaluate(
            """() => ({
                name: document.getElementById('ssh-profile-name').value,
                summary: document.getElementById('ssh-profile-route-summary').textContent,
                saveDisabled: document.getElementById('ssh-profile-save').disabled
            })"""
        )
        check(
            preloaded_editor == {'name': 'builder@build.example', 'summary': 'New direct profile: builder@build.example:22', 'saveDisabled': False},
            'SSH Settings did not preload the active SSH tab as a create-only draft',
        )
        page.click('#ssh-profile-list button[data-profile-id="profile-a"]')
        page.get_by_label('Target Username', exact=True).fill('builder2')
        page.click('#ssh-profile-save')
        page.wait_for_function(
            """() => document.getElementById('ssh-profile-status').innerText === 'Saved Build Server. Changes apply to the next connection.'""",
            timeout=5000,
        )
        updated = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(len(updated['profiles']) == 2, 'Save created a copy of a loaded profile')
        profile_a = next(profile for profile in updated['profiles'] if profile['id'] == 'profile-a')
        check(profile_a['username'] == 'builder2', 'Save did not update the loaded stable ID')

        page.click('#ssh-profile-down')
        page.wait_for_function(
            """async () => (await window.terminalTest.getSshSessionState()).profiles[1].id === 'profile-a'""",
            timeout=5000,
        )
        reordered = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check([profile['id'] for profile in reordered['profiles']] == ['profile-b', 'profile-a'], 'profile move used list index as identity')

        page.click('#ssh-profile-create')
        page.fill('#ssh-profile-name', 'Build Server Copy')
        page.get_by_label('Target Host', exact=True).fill('build.example')
        page.get_by_label('Target Username', exact=True).fill('builder')
        page.get_by_label('Target Port', exact=True).fill('2222')
        page.click('#ssh-profile-save')
        page.wait_for_function(
            """async () => (await window.terminalTest.getSshSessionState()).profiles.length === 3""",
            timeout=5000,
        )
        created = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(created['profiles'][-1]['name'] == 'Build Server Copy', 'Create did not add a separate profile')
        check(created['profiles'][-1]['host'] == 'build.example', 'New session did not save its edited target')
        check(created['profiles'][-1]['port'] == '2222', 'New session did not retain its port')
        created_profile_id = created['profiles'][-1]['id']
        check(created_profile_id != 'profile-a', 'Create reused the loaded stable ID')
        original_profile = next(profile for profile in created['profiles'] if profile['id'] == 'profile-a')
        check(original_profile['port'] == '22', 'Create modified the loaded profile')

        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        persisted = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(
            created_profile_id in [profile['id'] for profile in persisted['profiles']],
            'SSH profiles did not persist in IndexedDB across reload',
        )

        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        profile_count_before_clear = len(persisted['profiles'])
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#ssh-history-clear')
        page.wait_for_function(
            """async () => (await window.terminalTest.getSshSessionState()).history.length === 0""",
            timeout=5000,
        )
        history_cleared = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(len(history_cleared['profiles']) == profile_count_before_clear, 'Clear History deleted SSH profiles')
        check(page.locator('#ssh-history-clear').is_disabled() is True, 'Clear History remained enabled when empty')
        page.click(f'#ssh-profile-list button[data-profile-id="{created_profile_id}"]')
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#ssh-profile-delete')
        page.wait_for_function(
            """async () => (await window.terminalTest.getSshSessionState()).profiles.length === 2""",
            timeout=5000,
        )
        deleted = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(
            created_profile_id not in [profile['id'] for profile in deleted['profiles']],
            'Delete Profile did not remove the selected stable ID',
        )

        page.evaluate(
            """() => {
                document.getElementById('settings-close').click();
                const saveHistory = document.getElementById('ssh-save-history');
                saveHistory.checked = false;
                saveHistory.dispatchEvent(new Event('change', { bubbles: true }));
                document.getElementById('ssh-save-session').checked = true;
                document.getElementById('new-tab-btn').click();
            }"""
        )
        check(
            page.locator('#ssh-save-session').is_checked() is False,
            'new terminal tab retained the previous Save session choice',
        )
        check(
            page.locator('#ssh-save-history').is_checked() is False,
            'new terminal tab did not retain the Save history preference',
        )
        check(
            page.evaluate("() => JSON.parse(localStorage.getItem('terminal.pref.v1')).saveSshHistory") is False,
            'Save history preference was not persisted immediately',
        )
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        check(
            page.locator('#ssh-save-history').is_checked() is False,
            'Save history preference did not persist across reload',
        )
    finally:
        close_context(context)


def test_browser_ssh_key_lifecycle_and_settings_transfer(browser, access_url):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    try:
        page.goto(debug_url(access_url), wait_until='domcontentloaded')
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        page.evaluate(
            """async () => {
                await window.terminalTest.setSshSessionState({
                    profiles: [
                        { id: 'profile-primary', sortOrder: 0, name: 'Primary', host: 'primary.example', port: '22', username: 'alice', keyId: null },
                        { id: 'profile-imported', sortOrder: 1, name: 'Imported', host: 'imported.example', port: '2200', username: 'bob', keyId: null }
                    ],
                    history: [
                        { id: 'history-imported', host: 'recent.example', port: '22', username: 'recent', lastUsedAt: '2026-08-26T01:00:00.000Z' }
                    ]
                });
                const policy = window.terminalTest.getTerminalPolicy();
                policy.force_connection = null;
                policy.default_connection = 'ssh';
                const ssh = policy.connection_options.find(option => option.connection_type === 'ssh');
                ssh.allowed = true;
                ssh.browser_key_allowed = true;
                window.terminalTest.applyTerminalPolicy(policy);
                const sshMode = document.querySelector('input[name="connection_type"][value="ssh"]');
                sshMode.checked = true;
                sshMode.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )

        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        page.click('#ssh-profile-list button[data-profile-id="profile-primary"]')
        page.wait_for_function(
            "() => document.getElementById('ssh-profile-name').value === 'Primary'",
            timeout=5000,
        )
        # Keep coverage of credentials owned by legacy profiles. New node keys
        # are exercised through the managed route editor in its dedicated suite.
        page.evaluate("() => window.terminalTest.createBrowserSshKeyForProfileForTest('profile-primary')")
        metadata = page.evaluate(
            "() => window.terminalTest.getBrowserSshKeyMetadataForTest('profile-primary')"
        )
        check(metadata['algorithm'] == 'Ed25519', 'browser SSH key did not use Ed25519')
        check(metadata['privateKeyExtractable'] is False, 'browser SSH private key was extractable')
        check(len(base64.b64decode(metadata['publicKeyRawB64'])) == 32, 'Ed25519 public key was not 32 bytes')
        key_type, public_blob_b64 = metadata['publicKeyOpenSsh'].split()
        public_blob = base64.b64decode(public_blob_b64)
        key_type_length = int.from_bytes(public_blob[:4], 'big')
        raw_length_offset = 4 + key_type_length
        raw_length = int.from_bytes(public_blob[raw_length_offset:raw_length_offset + 4], 'big')
        check(key_type == 'ssh-ed25519', 'OpenSSH public key used the wrong key type')
        check(public_blob[4:raw_length_offset] == b'ssh-ed25519', 'OpenSSH public key blob omitted its key type')
        check(raw_length == 32 and len(public_blob) == raw_length_offset + 4 + raw_length, 'OpenSSH public key blob is invalid')
        expected_fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(public_blob).digest()).decode('ascii').rstrip('=')
        check(metadata['fingerprint'] == expected_fingerprint, 'browser SSH key fingerprint is not OpenSSH-compatible')

        challenge = b'StandTerm browser-owned SSH signer smoke challenge'
        signature_b64 = page.evaluate(
            """args => window.terminalTest.signBrowserSshChallengeForTest(
                args.profileId, args.challenge
            )""",
            {'profileId': 'profile-primary', 'challenge': base64.b64encode(challenge).decode('ascii')},
        )
        signature = base64.b64decode(signature_b64)
        check(len(signature) == 64, 'browser returned an invalid Ed25519 signature length')
        Ed25519PublicKey.from_public_bytes(base64.b64decode(metadata['publicKeyRawB64'])).verify(
            signature,
            challenge,
        )

        page.click('#settings-close')
        check(
            page.evaluate("() => window.terminalTest.setConnectionTypeForTest('ssh')") == 'ssh',
            'test policy did not select SSH Quick Connect',
        )
        page.evaluate(
            """() => {
                document.getElementById('ssh-session-picker-toggle').click();
                document.querySelector('.ssh-session-picker-entry[data-entry-id="profile-primary"]').click();
            }"""
        )
        page.wait_for_function(
            "() => !document.getElementById('ssh-use-browser-key-label').hidden",
            timeout=5000,
        )
        check(page.locator('#ssh-use-browser-key').is_checked(), 'exact keyed profile did not default Use key on')
        check(page.locator('#password').is_disabled(), 'Use key did not disable the password field')
        form_data = page.evaluate('() => window.terminalTest.getConnectionFormDataForTest()')
        check(
            form_data.get('use_browser_key') is True,
            f'Quick Connect omitted the browser key control field: {form_data!r}',
        )
        check(form_data['password'] == '', 'Quick Connect sent a password with browser key authentication')
        check(form_data['profile_id'] == 'profile-primary', 'Quick Connect sent the wrong key owner profile')
        check(form_data['key_id'] == metadata['keyId'], 'Quick Connect sent the wrong browser key ID')

        page.evaluate(
            """metadata => {
                window.terminalTest.stageSshConnectionForTest({
                    host: 'primary.example', port: '22', username: 'alice',
                    useKey: true, profileId: 'profile-primary', keyId: metadata.keyId,
                    publicKeyFingerprintHex: metadata.publicKeyFingerprintHex
                });
                window.terminalTest.clearEmitted();
            }""",
            metadata,
        )
        request_payload = {
            'request_id': 'request-valid-signature',
            'terminal_id': 'main',
            'profile_id': 'profile-primary',
            'key_id': metadata['keyId'],
            'public_key_fingerprint': metadata['publicKeyFingerprintHex'],
            'algorithm': 'ssh-ed25519',
            'challenge': base64.b64encode(challenge).decode('ascii'),
            'challenge_sha256': hashlib.sha256(challenge).hexdigest(),
            'timeout_seconds': 10,
            'expires_at': time.time() - 60,
        }
        page.evaluate(
            'payload => window.terminalTest.handleBrowserSshSignRequestForTest(payload)',
            request_payload,
        )
        response = page.evaluate(
            """() => window.terminalTest.getEmitted()
                .filter(entry => entry.event === 'ssh_browser_sign_response').at(-1).args[0]"""
        )
        check(
            response['status'] == 'ok',
            'Windows/WSL wall-clock skew invalidated a fresh relative signing request',
        )
        Ed25519PublicKey.from_public_bytes(base64.b64decode(metadata['publicKeyRawB64'])).verify(
            base64.b64decode(response['signature']),
            challenge,
        )

        page.fill('#port', '2222')
        page.locator('#port').dispatch_event('input')
        page.wait_for_function(
            "() => !document.getElementById('ssh-use-browser-key').checked && !document.querySelector('#ssh-direct-key .ssh-node-public-key').value",
            timeout=5000,
        )
        check(page.locator('#password').is_enabled(), 'modified profile target kept key-only authentication active')

        envelope = page.evaluate('() => window.terminalTest.createBrowserSettingsEnvelopeForTest()')
        exported = page.evaluate(
            'envelope => window.terminalTest.decodeBrowserSettingsEnvelopeForTest(envelope)',
            envelope,
        )
        check(exported['format'] == 'standterm-browser-settings', 'settings ZIP payload format is incorrect')
        check('keys' not in exported, 'settings export included an SSH key collection')
        check(
            all('keyId' not in profile for profile in exported['ssh']['profiles']),
            'settings export included SSH profile key IDs',
        )
        exported_text = repr(exported)
        check('ssh-ed25519 ' not in exported_text, 'settings export included an SSH public key')
        check(metadata['keyId'] not in exported_text, 'settings export included an SSH key ID')

        page.evaluate(
            """async keyId => {
                await window.terminalTest.setSshSessionState({
                    profiles: [
                        { id: 'profile-primary', sortOrder: 0, name: 'Changed Locally', host: 'primary.example', port: '22', username: 'alice', keyId },
                        { id: 'profile-local', sortOrder: 1, name: 'Local Only', host: 'local.example', port: '22', username: 'local', keyId: null }
                    ],
                    history: [
                        { id: 'history-local', host: 'local-recent.example', port: '22', username: 'local', lastUsedAt: '2026-08-26T02:00:00.000Z' }
                    ]
                });
                const saveHistory = document.getElementById('ssh-save-history');
                saveHistory.checked = false;
                saveHistory.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            metadata['keyId'],
        )
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_navigation(wait_until='domcontentloaded', timeout=10000):
            page.evaluate(
                'envelope => window.terminalTest.importBrowserSettingsEnvelopeForTest(envelope)',
                envelope,
            )
        page.wait_for_function('() => !!window.terminalTest', timeout=10000)
        merged = page.evaluate('() => window.terminalTest.getSshSessionState()')
        check(
            len(merged['profiles']) == 4
            and [profile['id'] for profile in merged['profiles'][:2]] == ['profile-primary', 'profile-local']
            and all(profile['id'] not in {'profile-primary', 'profile-local', 'profile-imported'} for profile in merged['profiles'][2:]),
            'settings import did not preserve existing Entries and remap imported IDs',
        )
        primary = next(profile for profile in merged['profiles'] if profile['id'] == 'profile-primary')
        check(primary['name'] == 'Changed Locally', 'settings import overwrote an existing Entry')
        check(primary['keyId'] == metadata['keyId'], 'settings import changed the existing browser key link')
        check(len(merged['history']) == 2, 'settings import did not merge SSH history')
        check(page.locator('#ssh-save-history').is_checked(), 'settings import did not restore browser preferences')
        check(
            page.evaluate("keyId => window.terminalTest.browserSshKeyRecordExistsForTest(keyId)", metadata['keyId']),
            'settings import removed the existing browser private key',
        )

        page.click('#quick-settings')
        page.click('.settings-nav-item[data-tab="ssh-sessions"]')
        page.click('#ssh-profile-list button[data-profile-id="profile-primary"]')
        page.click('#ssh-profile-create')
        page.fill('#ssh-profile-name', 'Primary Copy')
        page.get_by_label('Target Host', exact=True).fill('copy.example')
        page.get_by_label('Target Username', exact=True).fill('copy')
        page.click('#ssh-profile-save')
        page.wait_for_function(
            """async () => (await window.terminalTest.getSshSessionState()).profiles
                .some(profile => profile.name === 'Primary Copy')""",
            timeout=5000,
        )
        copied_state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        copied = next(profile for profile in copied_state['profiles'] if profile['name'] == 'Primary Copy')
        check(copied['keyId'] is None, 'Create copied a browser key from the loaded profile')

        page.click('#ssh-profile-list button[data-profile-id="profile-primary"]')
        page.once('dialog', lambda dialog: dialog.accept())
        page.click('#ssh-profile-delete')
        page.wait_for_function(
            """async () => !(await window.terminalTest.getSshSessionState()).profiles
                .some(profile => profile.id === 'profile-primary')""",
            timeout=5000,
        )
        check(
            page.evaluate("keyId => window.terminalTest.browserSshKeyRecordExistsForTest(keyId)", metadata['keyId']) is False,
            'deleting a keyed profile left its private key orphaned',
        )
    finally:
        close_context(context)


def main():
    sync_playwright, PlaywrightError, _ = load_playwright()
    tests = [
        test_access_required_page_accepts_token_login,
        test_initial_access_login_falls_back_without_localization_or_javascript,
        test_browser_authorization_gate_hides_connection_controls,
        test_server_unavailable_waits_for_reconnect,
        test_retry_now_resubscribes_after_socket_disconnect,
        test_invalid_session_reconnect_prompts_for_current_token,
        test_browser_access_and_recovery_in_traditional_chinese,
        test_platform_passkey_recovers_live_session_without_access_token,
        test_agent_panel_can_be_dragged,
        test_toolbar_pause_targets_main_tab_not_panel_override,
        test_agent_mint_quick_action_applies_saved_permission,
        test_agent_language_preview_preserves_access_and_applies_on_next_page,
        test_terminal_pip_hides_selected_tab_and_keeps_background_tab,
        test_sftp_status_actions_and_terminal_pip_transition,
        test_sftp_send_context_action_is_limited_to_connected_ssh_tabs,
        test_restored_terminal_list_allocates_next_new_tab_id,
        test_operator_observation_warning_ui,
        test_hidden_mirror_ignores_visible_scroll,
        test_privacy_states_block_snapshots_and_agent_runs,
        test_agent_panel_status_gates_and_external_hint,
        test_external_token_tab_indicator_tracks_background_lifecycle,
        test_session_recovery_new_tab_can_renew_external_agent_token,
        test_rendered_viewport_snapshot_returns_png,
        test_background_terminal_render_uses_mirror_canvas_png,
        test_paste_review_approve_and_cancel,
        test_clipboard_paste_targets_and_native_review,
        test_clipboard_paste_encoding_is_consistent,
        test_approval_payload_and_stale_rejections,
        test_localized_input_decisions_preserve_exact_proposal,
        test_file_copy_approval_shows_canonical_plan,
        test_agent_transfer_stop_states_and_dismissal,
        test_agent_copy_and_transfer_states_in_traditional_chinese,
        test_file_copy_approval_is_global_and_decision_is_single_shot,
        test_file_copy_approval_keeps_controls_visible_with_long_paths,
        test_ime_anchor_poc_loads_and_fails_open,
        test_cjk_width_compatibility_defaults_off,
        test_windows_font_fallback_defaults_and_migrates_legacy,
        test_powerline_symbol_fallback_defaults_on_and_preserves_opt_out,
        test_webgl_renderer_closes_block_glyph_row_gaps,
        test_unicode_provider_keeps_emoji_text_in_separate_cells,
        test_cursor_type_setting_updates_existing_and_new_terminals,
        test_webgl_bar_cursor_is_visible_at_first_column,
        test_osc_title_updates_fixed_status_column,
        test_settings_server_tab_loads_readonly_snapshot,
        test_connection_diagnostics_are_session_scoped_and_redacted,
        test_settings_access_recovery_fetches_access_url_on_demand,
        test_access_url_token_is_remembered_only_for_recovery,
        test_connection_controls_follow_start_fields_without_legacy_payload,
        test_terminal_payload_text_is_not_control,
        test_ssh_host_key_prompts_default_to_cancel_and_bind_actions,
        test_core_agent_connect_info_can_be_copied_and_confirmed,
        test_agent_tunnel_uses_panel_permissions_and_keeps_focus,
        test_remote_agent_info_tracks_ssh_carrier_and_rejects_late_replies,
        test_ssh_history_records_success_without_saving_profiles,
        test_ssh_profile_picker_and_settings_save_semantics,
        test_browser_ssh_key_lifecycle_and_settings_transfer,
    ]
    proc = None
    browser = None
    try:
        proc, access_url = start_server()
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                message = str(exc)
                if 'Executable doesn' in message or 'playwright install' in message:
                    raise RuntimeError(f'Playwright Chromium browser is not installed. {SETUP_HINT}') from exc
                raise
            try:
                for test in tests:
                    test(browser, access_url)
                    print(f'{test.__name__}: ok')
            finally:
                if browser is not None:
                    browser.close()
    finally:
        if proc is not None:
            stop_server(proc)


if __name__ == '__main__':
    try:
        main()
    except SmokeFailure as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(2)
