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
PYTHON = ROOT / 'tools' / '.venv_wsl' / 'bin' / 'python'
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
        'STANDTERM_ASYNC_MODE': 'threading',
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


def close_context(context):
    try:
        context.close()
    except Exception:
        pass


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

        moved = page.evaluate("terminalId => window.terminalTest.setTerminalPipModeForTest(terminalId, true)", active_id)
        check(moved is True, 'test hook could not move terminal into PiP mode')
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

        target_set = page.evaluate("terminalId => window.terminalTest.setAgentPanelTargetForTest(terminalId)", active_id)
        check(target_set is True, 'test hook could not target Agent panel at PiP terminal')
        agent_target = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        check(agent_target['agentPanelTerminalId'] == active_id, 'Agent panel did not target PiP terminal')

        page.evaluate("terminalId => window.terminalTest.setTerminalPipModeForTest(terminalId, false)", active_id)
        restored = page.evaluate("() => window.terminalTest.getTerminalTabsState()")
        restored_tab = next(item for item in restored['tabs'] if item['id'] == active_id)
        check(restored_tab['inPip'] is False and restored_tab['hidden'] is False, 'restored PiP tab did not return to tab list')
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
            "() => window.terminalTest.getSocketState().connected === true",
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


def main():
    sync_playwright, PlaywrightError, _ = load_playwright()
    tests = [
        test_access_required_page_accepts_token_login,
        test_agent_panel_can_be_dragged,
        test_terminal_pip_hides_selected_tab_and_keeps_background_tab,
        test_restored_terminal_list_allocates_next_new_tab_id,
        test_operator_observation_warning_ui,
        test_hidden_mirror_ignores_visible_scroll,
        test_privacy_states_block_snapshots_and_agent_runs,
        test_agent_panel_status_gates_and_external_hint,
        test_session_recovery_new_tab_can_renew_external_agent_token,
        test_rendered_viewport_snapshot_returns_png,
        test_paste_review_approve_and_cancel,
        test_approval_payload_and_stale_rejections,
        test_cjk_width_compatibility_defaults_off,
        test_cursor_type_setting_updates_existing_and_new_terminals,
        test_settings_server_tab_loads_readonly_snapshot,
        test_settings_access_recovery_fetches_access_url_on_demand,
        test_access_url_token_is_remembered_only_for_recovery,
        test_connection_controls_follow_start_fields_without_legacy_payload,
        test_terminal_payload_text_is_not_control,
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
