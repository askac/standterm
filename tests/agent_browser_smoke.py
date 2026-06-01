import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
        "() => document.getElementById('socketStatus')?.innerText === 'Connected'",
        timeout=10000,
    )
    page.wait_for_selector('#connectBtn:not([disabled])', timeout=10000)
    page.click('#connectBtn')
    page.wait_for_function(
        '() => window.terminalTest.getActiveAgentState()?.connected === true',
        timeout=10000,
    )
    return context, page


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
                commandTag: document.getElementById('agent-external-command').tagName
            })"""
        )
        check(disabled_external['buttonDisabled'] is True, 'external token button stayed enabled in disabled mode')
        check('Select Observe' in disabled_external['hint'], 'external token hint did not explain disabled prerequisite')
        check(disabled_external['commandTag'] == 'TEXTAREA', 'external token command output is not a textarea')

        set_agent_mode(page, 'observe', 'observe')
        enabled_external = page.evaluate(
            """() => ({
                buttonDisabled: document.getElementById('agent-external-token-btn').disabled,
                hint: document.getElementById('agent-external-hint').innerText
            })"""
        )
        check(enabled_external['buttonDisabled'] is False, 'external token button did not enable in observe mode')
        check('available' in enabled_external['hint'], 'external token hint did not show available state')
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
        page.evaluate("() => window.terminalTest.startPasteReview(':\\n:\\n')")
        wait_for_agent(page, "state.privacy_state === 'paste_review'")
        page.evaluate("() => document.getElementById('paste-review-cancel').click()")
        wait_for_agent(page, "state.privacy_state === 'normal'")
        check(not get_emitted(page, 'ssh_input'), 'paste review cancel emitted ssh_input')

        clear_emitted(page)
        page.evaluate("() => window.terminalTest.startPasteReview(':\\n:\\n')")
        wait_for_agent(page, "state.privacy_state === 'paste_review'")
        page.evaluate("() => document.getElementById('paste-review-approve').click()")
        wait_for_agent(page, "state.privacy_state === 'normal'")
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
        test_agent_panel_can_be_dragged,
        test_operator_observation_warning_ui,
        test_hidden_mirror_ignores_visible_scroll,
        test_privacy_states_block_snapshots_and_agent_runs,
        test_agent_panel_status_gates_and_external_hint,
        test_rendered_viewport_snapshot_returns_png,
        test_paste_review_approve_and_cancel,
        test_approval_payload_and_stale_rejections,
        test_settings_server_tab_loads_readonly_snapshot,
        test_terminal_payload_text_is_not_control,
    ]
    proc = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                message = str(exc)
                if 'Executable doesn' in message or 'playwright install' in message:
                    raise RuntimeError(f'Playwright Chromium browser is not installed. {SETUP_HINT}') from exc
                raise
            try:
                proc, access_url = start_server()
                for test in tests:
                    test(browser, access_url)
                    print(f'{test.__name__}: ok')
            finally:
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
