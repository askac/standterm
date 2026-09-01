import base64
import hashlib
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
    env = os.environ.copy()
    env.update({
        'STANDTERM_HOST': '127.0.0.1',
        'STANDTERM_PORT': str(port),
        'STANDTERM_DISABLE_AUTO_HTTPS': '1',
        'STANDTERM_DISABLE_AGENTINFO_CURRENT': '1',
        'STANDTERM_ASYNC_MODE': 'threading',
        'STANDTERM_ACCESS_UI': 'off',
        'STANDTERM_OPERATOR_OBSERVATION_DIR': tempfile.mkdtemp(prefix='standterm-observation-smoke-'),
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


def new_page(browser, access_url):
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    page.goto(debug_url(access_url), wait_until='domcontentloaded')
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


def test_access_required_page_accepts_token_login(browser, access_url):
    parsed = urllib.parse.urlparse(access_url)
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    base_url = urllib.parse.urlunparse(parsed._replace(query='', fragment=''))
    login_url = debug_url(base_url)
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    try:
        page.goto(login_url, wait_until='domcontentloaded')
        page.wait_for_selector('#access-token', timeout=5000)
        page.fill('#access-token', token)
        page.click('button[type="submit"]')
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


def test_browser_authorization_gate_hides_connection_controls(browser, access_url):
    context = browser.new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
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
                    warning: document.getElementById('browser-auth-warning').innerText,
                    message: document.getElementById('browser-auth-message').innerText,
                    connectionDisplay: getComputedStyle(document.getElementById('connection-form')).display,
                    sshVisible: document.getElementById('ssh-fields').getClientRects().length > 0,
                    actionsInsideControls: actions.left >= controls.left && actions.right <= controls.right
                };
            }"""
        )
        check(state['title'] == 'StandTerm', 'authorization gate does not show the StandTerm product name')
        check(state['sessionId'].startswith('Session ID: '), 'authorization gate does not show the launcher session ID')
        check(len(state['sessionId']) > len('Session ID: '), 'authorization gate launcher session ID is empty')
        check(state['warning'] == 'YOU SHALL NOT PASS!!', 'authorization gate warning is missing')
        check(state['message'] == 'First time? Please use an Auth URL.', 'authorization gate first-use hint is missing')
        check(state['connectionDisplay'] == 'none', 'authorization gate left connection controls visible')
        check(state['sshVisible'] is False, 'authorization gate left SSH fields visible')
        check(state['actionsInsideControls'] is True, 'authorization gate actions overflow the controls panel')

        page.fill('#browser-auth-url-input', 'https://example.test/?token=abc')
        page.click('#browser-auth-url-submit')
        check(
            'one-time authorization code' in page.locator('#browser-auth-url-error').inner_text(),
            'authorization gate accepted a URL without an authorization grant',
        )

        page.click('#browser-auth-help-btn')
        page.wait_for_selector('#browser-auth-help-modal.open', timeout=5000)
        check(
            'checks for the file automatically' in page.locator('.browser-auth-help-body').inner_text(),
            'manual authorization help does not explain automatic checking',
        )
        page.click('#browser-auth-help-close')
        check(
            page.locator('#browser-auth-help-modal').get_attribute('aria-hidden') == 'true',
            'manual authorization help did not close',
        )
    finally:
        close_context(context)


def close_context(context):
    try:
        context.close()
    except Exception:
        pass


def test_server_unavailable_waits_for_reconnect(browser, access_url):
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
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
        check(unavailable['socketStatus'] == 'Server not available (retrying)', 'socket status did not identify server unavailability')
        check('keep checking and reconnect automatically' in unavailable['message'], 'server unavailable guidance did not explain automatic recovery')
        check(unavailable['messageDisplay'] == 'block', 'server unavailable guidance was not visible')
        check(unavailable['connectionFormDisplay'] == 'none', 'connection picker remained visible while the server was unavailable')
        check(unavailable['connectDisabled'] is True, 'terminal connect button remained enabled while the server was unavailable')
        check(page.locator('#server-retry-now').is_visible(), 'Retry Now was not visible with the disconnect warning')
        page.click('#server-retry-now')
        check(
            page.locator('#server-retry-now').inner_text() == 'Retrying...',
            'Retry Now did not trigger an immediate reconnect attempt',
        )
        check(
            any(
                event['event'] == 'socket.retry_now'
                for event in page.evaluate('() => window.terminalTest.getConnectionDiagnostics()')
            ),
            'Retry Now did not record an explicit reconnect attempt',
        )

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


def test_invalid_session_reconnect_prompts_for_current_token(browser, access_url):
    parsed = urllib.parse.urlparse(access_url)
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    context = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = context.new_page()
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
        check(recovery['title'] == 'Access token required', 'session recovery did not ask for an access token')
        check('server restarted or your session expired' in recovery['detail'], 'session recovery did not explain why the token is required')
        check('current StandTerm launcher' in recovery['message'], 'session recovery did not request the current launcher token')

        page.fill('#session-recovery-token', token)
        page.click('#session-recovery-form button[type="submit"]')
        page.wait_for_function(
            "() => window.terminalTest.getSocketState().connected === true",
            timeout=10000,
        )
        check(
            page.evaluate("() => window.terminalTest.getSocketState().serverConnectionState") == 'available',
            'valid current token did not restore the server connection',
        )
    finally:
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


def test_terminal_pip_hides_selected_tab_and_keeps_background_tab(browser, access_url):
    context, page = new_page(browser, access_url)
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
        check(pip_status['mintText'] == 'Mint' and pip_status['mintHidden'] is False, 'Terminal PiP did not show Mint')
        check(pip_status['mint3xText'] == 'Mint+' and pip_status['mint3xHidden'] is False, 'Terminal PiP did not show Mint+')
        check(pip_status['agentPanelText'] == 'Show Agent Panel', 'Terminal PiP Agent panel control was incorrect')

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


def test_sftp_status_actions_and_terminal_pip_transition(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.evaluate(
            """() => window.terminalTest.applyTerminalListForTest({
                terminals: [
                    {
                        terminal_id: 'main',
                        connection_type: 'ssh',
                        terminal_label: 'SSH',
                        term: 'xterm-256color',
                        connected: true
                    },
                    {
                        terminal_id: 'term-2',
                        connection_type: 'local_shell',
                        terminal_label: 'bash',
                        term: 'xterm-256color',
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
                'title': 'Open SFTP File Manager',
                'text': '📁',
            },
            'connected SSH status bar did not expose the SFTP action',
        )
        page.click('#sftp-status-btn')
        page.wait_for_function(
            "() => documentPictureInPicture.window?.document.querySelector('.sftp-pip-title')?.textContent === 'SFTP File Manager'",
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
        check(unavailable_status['title'] == 'SFTP not available', 'unavailable SFTP status hint was unclear')
        check('×' in unavailable_status['text'], 'unavailable SFTP status action omitted its cross mark')
        check(unavailable_status['markColor'] == 'rgb(255, 69, 58)', 'unavailable SFTP cross was not red')
        unavailable_menu = page.evaluate("() => window.terminalTest.showContextMenuForTest('main')")
        check(unavailable_menu['sftpVisible'] is True, 'unavailable SSH context action disappeared')
        check(unavailable_menu['sftpDisabled'] is True, 'unavailable SSH context action remained enabled')
        check('SFTP not available' in unavailable_menu['sftpText'], 'unavailable SSH context action hint was unclear')

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
                'title': 'Open SFTP File Manager',
                'text': '📁',
            },
            'Terminal PiP did not expose the SFTP action',
        )

        page.evaluate("() => documentPictureInPicture.window.document.querySelector('.pip-sftp-button').click()")
        page.wait_for_function(
            "() => documentPictureInPicture.window?.document.querySelector('.sftp-pip-title')?.textContent === 'SFTP File Manager'",
            timeout=5000,
        )
        restored = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        restored_main = next(item for item in restored['tabs'] if item['id'] == 'main')
        check(restored_main['inPip'] is False, 'opening SFTP from Terminal PiP did not restore the terminal')
        page.evaluate('() => documentPictureInPicture.window.close()')
        page.wait_for_function('() => !window.documentPictureInPicture.window', timeout=5000)
    finally:
        close_context(context)


def test_sftp_send_context_action_is_limited_to_connected_ssh_tabs(browser, access_url):
    context, page = new_page(browser, access_url)
    browser_console = []
    page.on('console', lambda message: browser_console.append(message.text))
    try:
        page.evaluate(
            """() => window.terminalTest.applyTerminalListForTest({
                terminals: [{
                    terminal_id: 'main',
                    connection_type: 'ssh',
                    terminal_label: 'SSH',
                    term: 'xterm-256color',
                    connected: true
                }]
            })"""
        )
        ssh_menu = page.evaluate("() => window.terminalTest.showContextMenuForTest('main')")
        check(ssh_menu['terminalId'] == 'main', 'SFTP context action targeted the wrong terminal')
        check(ssh_menu['sftpVisible'] is True, 'connected SSH tab did not show SFTP send action')
        check('SFTP File Manager' in ssh_menu['sftpText'], 'SFTP context action label was unclear')
        check(page.evaluate('() => !!window.documentPictureInPicture'), 'Document PiP is unavailable in the test browser')
        page.click('#sftp-send-option')
        page.wait_for_function('() => !!window.documentPictureInPicture.window', timeout=5000)
        pip_state = page.evaluate(
            """() => ({
                title: documentPictureInPicture.window.document.querySelector('.sftp-pip-title')?.textContent,
                hint: documentPictureInPicture.window.document.querySelector('.sftp-direct-hint')?.textContent,
                hasDropZone: !!documentPictureInPicture.window.document.querySelector('.sftp-drop-zone'),
                hasPathInput: !!documentPictureInPicture.window.document.querySelector('.sftp-path-input')
            })"""
        )
        check(pip_state['title'] == 'SFTP File Manager', 'SFTP PiP title was missing')
        check('Nested SSH sessions' in pip_state['hint'], 'SFTP PiP did not explain the direct endpoint boundary')
        check(pip_state['hasDropZone'] is True, 'SFTP PiP did not expose a file drop zone')
        check(pip_state['hasPathInput'] is True, 'SFTP PiP did not expose destination path navigation')

        page.wait_for_function(
            "() => documentPictureInPicture.window.document.querySelector('.sftp-transfer-status')?.textContent !== 'Opening SFTP…'",
            timeout=5000,
        )
        rendered = page.evaluate(
            """() => window.terminalTest.renderSftpEntriesForTest({
                path: '/home/tester',
                directories: [{ name: 'docs' }],
                files: [
                    { file_id: 'sftpf_random_a', name: 'reference.txt', size: 9, mtime: 25 },
                    { file_id: 'sftpf_random_b', name: 'existing.txt', size: 4, mtime: 26 }
                ]
            })"""
        )
        check(rendered is True, 'SFTP PiP test fixture could not render remote files')
        clear_emitted(page)
        file_ui = page.evaluate(
            """() => {
                const pipDocument = documentPictureInPicture.window.document;
                const files = [...pipDocument.querySelectorAll('.sftp-file-entry')];
                files[0].click();
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
                    operationVisible: pipDocument.querySelector('.sftp-file-operation-box').classList.contains('visible'),
                    actions: [...pipDocument.querySelectorAll('.sftp-file-operation-actions button')].map(button => button.innerText),
                    preparing,
                    downloadReady: !pipDocument.querySelector('.sftp-file-download').disabled
                };
            }"""
        )
        check(file_ui['fileCount'] == 2, 'SFTP PiP did not list regular files')
        check(file_ui['operationVisible'] is True, 'selecting an SFTP file did not open file actions')
        check(file_ui['actions'] == ['Download', 'Rename…', 'Delete…', 'Close'], 'SFTP file actions were incomplete')
        check(file_ui['preparing'] == {'disabled': True, 'text': 'Preparing…'}, 'SFTP Download was enabled before its ticket was ready')
        check(file_ui['downloadReady'] is True, 'SFTP Download was not enabled after its ticket became ready')

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
        check(browser_download['buttonDisabled'] is True and browser_download['buttonText'] == 'Downloaded', 'used SFTP download ticket remained actionable')
        check(any(message.startswith('[sftp] Download ticket requested') for message in browser_console), 'SFTP browser log omitted the ticket request')
        check(any(message.startswith('[sftp] Download ticket ready') for message in browser_console), 'SFTP browser log omitted the ready ticket')
        check(any(message.startswith('[sftp] Download button clicked') for message in browser_console), 'SFTP browser log omitted the explicit click')
        check(any(message.startswith('[sftp] Download link dispatched') for message in browser_console), 'SFTP browser log omitted the link dispatch')

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
        check('already exists' in rename_initial['hint'], 'Rename did not explain the duplicate name')
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
        check(first_delete['question'] == 'Do you want to delete this file?', 'first delete warning was unclear')
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
        check('cannot be recovered' in second_delete['question'], 'second delete warning did not state permanent loss')
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
                    connected: true
                }]
            })"""
        )
        local_menu = page.evaluate("() => window.terminalTest.showContextMenuForTest('main')")
        check(local_menu['sftpVisible'] is False, 'local shell tab exposed the SFTP send action')
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
                text: document.getElementById('operator-observation-state').innerText
            })"""
        )
        check(ui_state['body'] is True, 'operator observation did not set body warning class')
        check(ui_state['panel'] is True, 'operator observation did not set panel warning class')
        check('OBSERVING' in ui_state['text'], 'operator observation status text did not warn')
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


def test_agent_panel_status_gates_and_external_hint(browser, access_url):
    context, page = new_page(browser, access_url)
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
        check('private_input' in panel_state['privacyText'], 'privacy gate chip did not show privacy state')
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
        check('locked' in human_gate['text'], 'human input gate chip did not show active lease')
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
        check(disabled_external['accessText'] == 'Enable external agent', 'agent access toggle did not offer enable in disabled mode')
        check(disabled_external['modeButtonsDisabled'] is True, 'agent permission buttons were not disabled while access was off')
        check(disabled_external['buttonDisabled'] is True, 'external token button stayed enabled in disabled mode')
        check('Enable external agent' in disabled_external['hint'], 'external token hint did not explain disabled prerequisite')
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
        check(enabled_external['accessText'] == 'Disable external agent', 'agent access toggle did not offer disable after enabling')
        check(enabled_external['modeLabels'] == ['Observer', 'Approval', 'Full'], 'agent permission buttons did not use user-facing labels')
        check(enabled_external['buttonDisabled'] is False, 'external token button did not enable in observe mode')
        check('Mint' in enabled_external['hint'], 'external token hint did not show available state')

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

        page.click('#agent-toggle-btn')
        page.wait_for_selector('#agent-panel.visible', timeout=5000)
        minted_command = page.evaluate("() => document.getElementById('agent-external-command').value")
        check('# terminal handoff:' in minted_command, '3x mint did not expose the stable terminal handoff path')
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
        check(recovered_token_ui['buttonText'] == 'Mint token', 'new terminal reused stale external token command state')
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


def test_paste_review_approve_and_cancel(browser, access_url):
    context, page = new_page(browser, access_url)
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


def test_approval_payload_and_stale_rejections(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)
        set_agent_mode(page, 'approval', 'approval_pending')
        action = request_agent_action(page, ':\n')

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
        wait_for_last_action_error(page, 'agent_stale_proposal')

        set_privacy(page, 'normal')
        wait_for_agent(page, "state.privacy_state === 'normal'")
        mode_action = request_agent_action(page, ':\n')
        stale_mode_payload = approval_payload_from_action(mode_action)
        emit_socket(page, 'agent_mode_set', {'terminal_id': TERMINAL_ID, 'mode': 'observe'})
        wait_for_agent(page, f"state.mode === 'observe' && state.mode_version > {mode_action['mode_version']}")
        emit_socket(page, 'agent_action_approve', stale_mode_payload)
        wait_for_last_action_error(page, 'agent_stale_mode_version')
    finally:
        close_context(context)


def test_file_copy_approval_shows_canonical_plan(browser, access_url):
    context, page = new_page(browser, access_url)
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
                conflict: document.getElementById('agent-file-copy-conflict').innerText,
                warning: document.getElementById('agent-file-copy-warning').innerText,
                approve: document.getElementById('agent-approve-btn').innerText,
                approveDisabled: document.getElementById('agent-approve-btn').disabled
            })"""
        )
        check(details['visible'] is True, 'file copy approval details were hidden')
        check(details['source'] == 'builder@source.example:22:/srv/releases/image.bin', 'source plan was not exact')
        check(details['destination'] == 'Local Shell (bash):/tmp/image.bin', 'destination plan was not exact')
        check(details['size'] == '1.50 KiB', 'source size was not rendered')
        check(details['conflict'] == 'replace', 'replace mode was not rendered')
        check('atomically replace' in details['warning'], 'replace warning was not explicit')
        check(details['approve'] == 'Approve copy', 'copy approval button was not explicit')
        check(details['approveDisabled'] is False, 'copy approval button was unexpectedly disabled')
        page.evaluate(
            """payload => window.terminalTest.applyAgentActionPayloadForTest(payload)""",
            {
                'action_id': 'copy-action-1',
                'action_type': 'file_copy',
                'status': 'failed',
                'terminal_id': TERMINAL_ID,
                'error_code': 'file_copy_publish_outcome_unknown',
            },
        )
        status_detail = page.locator('#agent-status-detail').inner_text()
        check(
            'destination may have changed; inspect it before retrying' in status_detail,
            'publish outcome warning was not explicit',
        )
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
    expected = 'Consolas, "Cascadia Mono", "Courier New", monospace'
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
        check(preserved == custom, 'custom terminal font face was overwritten by default migration')
    finally:
        close_context(context)


def test_powerline_symbol_fallback_is_optional_and_applies_immediately(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        initial = page.evaluate("() => window.terminalTest.getActiveTerminalOptions()")
        check(
            not initial['fontFamily'].startswith('"StandTerm Powerline Symbols"'),
            'Powerline symbol fallback defaulted on',
        )

        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="appearance"]')
        check(
            page.locator('#pref-powerlineSymbols').is_checked() is False,
            'Powerline symbol fallback checkbox defaulted on',
        )
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


def test_settings_server_tab_loads_readonly_snapshot(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        page.click('#quick-settings')
        page.wait_for_selector('#settings-modal.open', timeout=5000)
        page.click('.settings-nav-item[data-tab="server"]')
        page.wait_for_function(
            "() => ['Read-only', 'Writable'].includes(document.getElementById('server-settings-status')?.textContent)",
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
        check(state['version'] == '1', 'settings server tab did not show settings version')
        check(state['view'] == 'Allowed', 'settings server tab did not show view capability')
        check(state['low'] == 'Allowed', 'settings server tab did not expose local low-risk writes')
        check(state['high'] == 'Denied', 'settings server tab exposed high-risk writes')
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
        check(local_shell_payload.get('expected_schema_digest'), 'Local Shell update did not include expected schema digest')
        page.wait_for_function(
            """target => document.querySelector(
                    '#server-settings-mutable-controls .server-setting-input[data-setting-key="local_shell.default_kind"]'
                )?.value === target
                && document.getElementById('local-shell-kind')?.value === target""",
            arg=selected_shell_kind,
            timeout=10000,
        )
        clear_emitted(page)
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
        check(update_payload.get('expected_schema_digest'), 'UART update did not include expected schema digest')
    finally:
        close_context(context)


def test_connection_diagnostics_are_session_scoped_and_redacted(browser, access_url):
    context, page = new_page(browser, access_url)
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
        check('connection events.' in state['status'], 'diagnostics did not show event count')

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
        check(cleared['status'] == '0 connection events.', 'diagnostics clear did not update status')
    finally:
        close_context(context)


def test_settings_access_recovery_fetches_access_url_on_demand(browser, access_url):
    parsed = urllib.parse.urlparse(access_url)
    token = urllib.parse.parse_qs(parsed.query)['token'][0]
    context, page = new_page(browser, access_url)
    try:
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
        check('shown for 30 seconds' in revealed['status'], 'access URL reveal did not update status')
        check(access_url in revealed['text'], 'revealed access URL did not match server access URL')
        check('token=' not in revealed['location'], 'access URL reveal modified browser location')

        page.click('#server-access-copy-btn')
        page.wait_for_function(
            "() => document.getElementById('server-access-status')?.textContent === 'Access URL copied.'",
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


def test_ssh_history_and_auto_profile_follow_structured_success(browser, access_url):
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
        check(failed_state == {'version': 1, 'profiles': [], 'history': []}, 'failed SSH connection was stored')

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
                const before = window.terminalTest.getSshSessionState();
                await window.terminalTest.recordSuccessfulSshConnectionForTest({
                    host: 'private.example', port: '22', username: 'alice',
                    saveHistory: false, saveSession: false
                });
                return { before, after: window.terminalTest.getSshSessionState() };
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
            [profile['host'] for profile in profile_only['profiles']] == ['profile-only.example'],
            'Save session did not create a profile while history was disabled',
        )
        page.evaluate(
            """async () => window.terminalTest.setSshSessionState({
                profiles: [], history: window.terminalTest.getSshSessionState().history
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
        check(len(saved_state['profiles']) == 1, 'matching Save session connections created duplicate profiles')
        check(saved_state['profiles'][0]['name'] == 'bob@saved.example', 'automatic profile name is incorrect')
        check(saved_state['profiles'][0]['keyId'] is None, 'automatic profile did not reserve an empty key ID')
        serialized = repr(saved_state).lower()
        check('must-not-persist' not in serialized, 'SSH session storage retained a password')
        check('password' not in serialized, 'SSH session storage contains a password field')
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
                    connection_type: 'ssh', terminal_label: 'SSH - Build Server'
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
        preloaded_editor = page.evaluate(
            """() => ({
                name: document.getElementById('ssh-profile-name').value,
                host: document.getElementById('ssh-profile-host').value,
                saveDisabled: document.getElementById('ssh-profile-save').disabled
            })"""
        )
        check(
            preloaded_editor == {'name': 'recent@recent.example', 'host': 'recent.example', 'saveDisabled': True},
            'SSH Settings did not preload Quick Connect as a create-only draft',
        )
        page.click('#ssh-profile-list button[data-profile-id="profile-a"]')
        page.fill('#ssh-profile-username', 'builder2')
        page.click('#ssh-profile-save')
        page.wait_for_function(
            """() => document.getElementById('ssh-profile-status').innerText === 'Saved Build Server.'""",
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

        page.fill('#ssh-profile-name', 'Build Server Copy')
        page.fill('#ssh-profile-port', '2222')
        page.click('#ssh-profile-create')
        page.wait_for_function(
            """async () => (await window.terminalTest.getSshSessionState()).profiles.length === 3""",
            timeout=5000,
        )
        created = page.evaluate("() => window.terminalTest.getSshSessionState()")
        check(created['profiles'][-1]['name'] == 'Build Server Copy', 'Create did not add a separate profile')
        check(created['profiles'][-1]['host'] == 'build.example', 'Create did not copy the loaded profile draft')
        check(created['profiles'][-1]['port'] == '2222', 'Create did not retain edits made after Load')
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
        page.check('#ssh-profile-key-enabled')
        page.wait_for_function(
            "() => document.getElementById('ssh-profile-key-status').innerText.includes('SHA256:')",
            timeout=10000,
        )
        check(
            page.locator('#ssh-profile-key-public').input_value().startswith('ssh-ed25519 '),
            'generated browser SSH key did not expose an OpenSSH public key',
        )
        page.click('#ssh-profile-save')
        page.wait_for_function(
            "() => document.getElementById('ssh-profile-status').innerText === 'Saved Primary.'",
            timeout=5000,
        )
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
            'expires_at': time.time() + 10,
        }
        page.evaluate(
            'payload => window.terminalTest.handleBrowserSshSignRequestForTest(payload)',
            request_payload,
        )
        response = page.evaluate(
            """() => window.terminalTest.getEmitted()
                .filter(entry => entry.event === 'ssh_browser_sign_response').at(-1).args[0]"""
        )
        check(response['status'] == 'ok', 'structured browser SSH signing request failed')
        Ed25519PublicKey.from_public_bytes(base64.b64decode(metadata['publicKeyRawB64'])).verify(
            base64.b64decode(response['signature']),
            challenge,
        )

        page.fill('#port', '2222')
        page.locator('#port').dispatch_event('input')
        page.wait_for_function(
            "() => document.getElementById('ssh-use-browser-key-label').hidden",
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
            [profile['id'] for profile in merged['profiles']]
            == ['profile-primary', 'profile-local', 'profile-imported'],
            'settings import did not update by stable ID and append new profiles',
        )
        primary = next(profile for profile in merged['profiles'] if profile['id'] == 'profile-primary')
        check(primary['name'] == 'Primary', 'settings import did not update the matching stable profile ID')
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
        page.wait_for_function("() => document.getElementById('ssh-profile-key-enabled').checked", timeout=5000)
        page.fill('#ssh-profile-name', 'Primary Copy')
        page.click('#ssh-profile-create')
        page.wait_for_function(
            """async () => (await window.terminalTest.getSshSessionState()).profiles
                .some(profile => profile.name === 'Primary Copy')""",
            timeout=5000,
        )
        copied_state = page.evaluate('() => window.terminalTest.getSshSessionState()')
        copied = next(profile for profile in copied_state['profiles'] if profile['name'] == 'Primary Copy')
        check(copied['keyId'] is None, 'Create copied a browser key from the loaded profile')

        page.click('#ssh-profile-list button[data-profile-id="profile-primary"]')
        page.wait_for_function("() => document.getElementById('ssh-profile-key-enabled').checked", timeout=5000)
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
        test_browser_authorization_gate_hides_connection_controls,
        test_server_unavailable_waits_for_reconnect,
        test_retry_now_resubscribes_after_socket_disconnect,
        test_invalid_session_reconnect_prompts_for_current_token,
        test_agent_panel_can_be_dragged,
        test_terminal_pip_hides_selected_tab_and_keeps_background_tab,
        test_sftp_status_actions_and_terminal_pip_transition,
        test_sftp_send_context_action_is_limited_to_connected_ssh_tabs,
        test_restored_terminal_list_allocates_next_new_tab_id,
        test_operator_observation_warning_ui,
        test_hidden_mirror_ignores_visible_scroll,
        test_privacy_states_block_snapshots_and_agent_runs,
        test_agent_panel_status_gates_and_external_hint,
        test_session_recovery_new_tab_can_renew_external_agent_token,
        test_rendered_viewport_snapshot_returns_png,
        test_background_terminal_render_uses_mirror_canvas_png,
        test_paste_review_approve_and_cancel,
        test_approval_payload_and_stale_rejections,
        test_file_copy_approval_shows_canonical_plan,
        test_cjk_width_compatibility_defaults_off,
        test_windows_font_fallback_defaults_and_migrates_legacy,
        test_powerline_symbol_fallback_is_optional_and_applies_immediately,
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
        test_ssh_history_and_auto_profile_follow_structured_success,
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
