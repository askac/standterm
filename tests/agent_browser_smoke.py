import os
import queue
import re
import socket
import subprocess
import sys
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
        'WEBSSH_HOST': '127.0.0.1',
        'WEBSSH_PORT': str(port),
        'WEBSSH_DISABLE_AUTO_HTTPS': '1',
        'WEBSSH_ASYNC_MODE': 'threading',
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
            raise RuntimeError('WebSSH server exited early:\n' + '\n'.join(lines[-40:]))
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
        raise RuntimeError('Timed out waiting for WebSSH access URL:\n' + '\n'.join(lines[-40:]))

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
    raise RuntimeError(f'Timed out waiting for WebSSH HTTP readiness: {last_error}')


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
    page.wait_for_function('() => !!window.websshTest', timeout=10000)
    page.wait_for_function(
        "() => document.getElementById('socketStatus')?.innerText === 'Connected'",
        timeout=10000,
    )
    page.wait_for_selector('#connectBtn:not([disabled])', timeout=10000)
    page.click('#connectBtn')
    page.wait_for_function(
        '() => window.websshTest.getActiveAgentState()?.connected === true',
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
        """args => window.websshTest.emitSocket(args.event_name, args.payload)""",
        js_arg_object(event_name, payload),
    )


def set_privacy(page, privacy_state):
    page.evaluate('privacyState => window.websshTest.setPrivacy(privacyState)', privacy_state)


def clear_emitted(page):
    page.evaluate('() => window.websshTest.clearEmitted()')


def get_emitted(page, event_name=None):
    emitted = page.evaluate('() => window.websshTest.getEmitted()')
    if event_name is None:
        return emitted
    return [entry for entry in emitted if entry.get('event') == event_name]


def active_agent_state(page):
    return page.evaluate('() => window.websshTest.getActiveAgentState()')


def wait_for_agent(page, predicate, timeout=10000):
    page.wait_for_function(
        """source => {
            const state = window.websshTest.getActiveAgentState();
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
            """payload => window.websshTest.writeTerminalOutput(payload.data, payload.output_seq)""",
            {'data': output, 'output_seq': 90},
        )
        page.wait_for_function(
            "() => window.websshTest.getMirrorSnapshot()?.lines?.join('\\n').includes('mirror-089')",
            timeout=10000,
        )
        before = page.evaluate('() => window.websshTest.getMirrorSnapshot()')
        page.evaluate('() => window.websshTest.scrollVisibleTerminal(-60)')
        page.wait_for_timeout(100)
        after = page.evaluate('() => window.websshTest.getMirrorSnapshot()')
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
        page.evaluate('() => window.websshTest.sendAgentSnapshot()')
        check(not get_emitted(page, 'agent_viewport_snapshot'), 'private_input allowed a snapshot emit')
        emit_socket(page, 'agent_provider_run_request', {'terminal_id': TERMINAL_ID})
        wait_for_last_action_error(page, 'agent_privacy_blocked')

        set_privacy(page, 'normal')
        wait_for_agent(page, "state.privacy_state === 'normal'")
        page.evaluate("() => window.websshTest.startPasteReview(':\\n:\\n')")
        wait_for_agent(page, "state.privacy_state === 'paste_review'")
        clear_emitted(page)
        page.evaluate('() => window.websshTest.sendAgentSnapshot()')
        check(not get_emitted(page, 'agent_viewport_snapshot'), 'paste_review allowed a snapshot emit')
        emit_socket(page, 'agent_provider_run_request', {'terminal_id': TERMINAL_ID})
        wait_for_last_action_error(page, 'agent_privacy_blocked')
        page.evaluate("() => document.getElementById('paste-review-cancel').click()")
        wait_for_agent(page, "state.privacy_state === 'normal'")

        set_privacy(page, 'paused')
        wait_for_agent(page, "state.privacy_state === 'paused' && state.mode === 'paused'")
        clear_emitted(page)
        page.evaluate('() => window.websshTest.sendAgentSnapshot()')
        check(not get_emitted(page, 'agent_viewport_snapshot'), 'paused allowed a snapshot emit')
        emit_socket(page, 'agent_provider_run_request', {'terminal_id': TERMINAL_ID})
        wait_for_last_action_error(page, 'agent_paused')
    finally:
        close_context(context)


def test_paste_review_approve_and_cancel(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        attach_agent(page)

        clear_emitted(page)
        page.evaluate("() => window.websshTest.startPasteReview(':\\n:\\n')")
        wait_for_agent(page, "state.privacy_state === 'paste_review'")
        page.evaluate("() => document.getElementById('paste-review-cancel').click()")
        wait_for_agent(page, "state.privacy_state === 'normal'")
        check(not get_emitted(page, 'ssh_input'), 'paste review cancel emitted ssh_input')

        clear_emitted(page)
        page.evaluate("() => window.websshTest.startPasteReview(':\\n:\\n')")
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
            "() => window.websshTest.getEmitted().some(entry => entry.event === 'agent_action_approve')",
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


def test_terminal_payload_text_is_not_control(browser, access_url):
    context, page = new_page(browser, access_url)
    try:
        payload_text = (
            'message_type=connection_error action_type=offer_localhost_key_setup '
            '{"message_type":"ssh_closed","setup_status":"success"}\\r\\n'
        )
        page.evaluate(
            """payload => window.websshTest.handleSshOutput(payload)""",
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
                connected: window.websshTest.getActiveAgentState().connected,
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
        test_hidden_mirror_ignores_visible_scroll,
        test_privacy_states_block_snapshots_and_agent_runs,
        test_paste_review_approve_and_cancel,
        test_approval_payload_and_stale_rejections,
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
