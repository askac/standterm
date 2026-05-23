import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as webssh


class DummyBridge(webssh.TerminalBridge):
    connection_type = webssh.CONNECTION_TYPE_LOCAL_SHELL
    terminal_kind = 'local'
    terminal_label = 'Dummy'

    def __init__(self, owner_session, terminal_id):
        super().__init__(owner_session, terminal_id)
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closing = True


class RecordingProvider(webssh.AgentProvider):
    name = 'recording'
    version = 'test-1'

    def __init__(self, terminal_input='provider\n'):
        self.terminal_input = terminal_input
        self.contexts = []
        self.runs = []

    def create_terminal_input_proposal(self, context, run):
        self.contexts.append(context)
        self.runs.append(run)
        return {
            'action_type': webssh.AGENT_ACTION_TERMINAL_INPUT,
            'terminal_id': context['terminal_id'],
            'data': self.terminal_input,
        }


class FailingProvider(webssh.AgentProvider):
    name = 'failing'
    version = 'test-1'

    def create_terminal_input_proposal(self, context, run):
        raise webssh.AgentProviderError(webssh.AGENT_ERROR_PROVIDER_FAILED, 'provider failed')


class InvalidProvider(webssh.AgentProvider):
    name = 'invalid'
    version = 'test-1'

    def create_terminal_input_proposal(self, context, run):
        return {
            'action_type': 'unsupported',
            'terminal_id': context['terminal_id'],
            'data': 'invalid\n',
        }


def reset_state():
    webssh.bridges.clear()
    webssh.pending_localhost_key_setups.clear()
    webssh.active_sessions.clear()
    webssh.socket_session_tokens.clear()
    webssh.socket_client_ips.clear()
    webssh.socket_browser_identities.clear()
    webssh.socket_browser_authorized.clear()
    webssh.socket_browser_auth_challenges.clear()
    webssh.agent_states.clear()
    webssh.agent_session_ids.clear()
    webssh.agent_viewer_ids.clear()
    webssh.agent_audit_store.clear()
    webssh.agent_transcript_store.clear()
    webssh.agent_user_input_metadata_store.clear()
    webssh.agent_viewport_snapshot_store.clear()
    webssh.agent_viewport_render_request_store.clear()
    webssh.external_agent_attach_store.clear()
    webssh.set_agent_provider_for_test(webssh.MockAgentProvider())


def make_client():
    flask_client = webssh.app.test_client()
    response = flask_client.get('/?token=' + webssh.ACCESS_TOKEN)
    assert response.status_code == 200, response.status_code
    socket_client = webssh.socketio.test_client(webssh.app, flask_test_client=flask_client)
    assert socket_client.is_connected()
    return socket_client


def make_flask_client():
    flask_client = webssh.app.test_client()
    response = flask_client.get('/?token=' + webssh.ACCESS_TOKEN)
    assert response.status_code == 200, response.status_code
    return flask_client


def make_socket_client(flask_client):
    socket_client = webssh.socketio.test_client(webssh.app, flask_test_client=flask_client)
    assert socket_client.is_connected()
    return socket_client


def current_session_token():
    assert webssh.socket_session_tokens
    return next(reversed(webssh.socket_session_tokens.values()))


def current_sid_for_session(session_token):
    matches = [
        sid for sid, token in webssh.socket_session_tokens.items()
        if token == session_token
    ]
    assert matches
    return matches[-1]


def received_events(client, name):
    return [event for event in client.get_received() if event['name'] == name]


def last_payload(client, name):
    events = received_events(client, name)
    assert events, name
    return events[-1]['args'][0]

def wait_for_event(client, name, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = received_events(client, name)
        if events:
            return events[-1]
        time.sleep(0.01)
    raise AssertionError(name)


def wait_until(predicate, description, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(description)


def add_dummy_bridge(session_token):
    bridge = DummyBridge(session_token, webssh.TERMINAL_ID_MAIN)
    webssh.set_bridge(session_token, webssh.TERMINAL_ID_MAIN, bridge)
    return bridge


def valid_viewport_snapshot(seq=1, rows=2, cols=4, fill='line', lines=None):
    if lines is not None:
        rows = len(lines)
    return {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'cols': cols,
        'rows': rows,
        'viewport_y': 0,
        'base_y': 0,
        'snapshot_seq': seq,
        'output_seq': 0,
        'captured_at': '2026-05-22T00:00:00.000Z',
        'lines': list(lines) if lines is not None else [fill for _ in range(rows)],
    }


def test_pause_blocks_pending_approval():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    assert last_payload(client, webssh.AGENT_EVENT_STATE)['mode'] == webssh.AGENT_MODE_OBSERVE

    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'blocked\n',
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)

    client.emit(webssh.AGENT_EVENT_PAUSE, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    assert last_payload(client, webssh.AGENT_EVENT_STATE)['mode'] == webssh.AGENT_MODE_PAUSED

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    result = last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] in {webssh.AGENT_ERROR_PAUSED, webssh.AGENT_ERROR_ACTION_NOT_PENDING}

    client.disconnect()


def test_approval_and_direct_writes_use_gate():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    state = last_payload(client, webssh.AGENT_EVENT_STATE)
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'approved\n',
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)
    assert action['requires_approval'] is True
    assert action['session_id'].startswith('ags_')
    assert action['viewer_id'].startswith('agv_')
    assert action['agent_binding_id'].startswith('agb_')
    assert action['proposal_id'].startswith('agp_')
    assert action['mode_version'] == state['mode_version']
    assert action['privacy_state'] == webssh.AGENT_PRIVACY_NORMAL
    assert action['privacy_version'] == state['privacy_version']
    assert action['escaped_preview'] == 'approved\\n'

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'session_id': action['session_id'],
        'viewer_id': action['viewer_id'],
        'agent_binding_id': action['agent_binding_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    })
    assert ''.join(bridge.writes) == 'approved\n'
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['status'] == webssh.AGENT_STATUS_COMPLETED

    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'direct\n',
    })
    assert ''.join(bridge.writes) == 'approved\ndirect\n'
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['status'] == webssh.AGENT_STATUS_COMPLETED

    client.disconnect()


def test_provider_run_uses_agent_gate():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    assert last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'

    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)
    assert action['requires_approval'] is True
    assert action['escaped_preview'] == 'pwd\\n'
    assert bridge.writes == []

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert ''.join(bridge.writes) == 'pwd\n'

    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert ''.join(bridge.writes) == 'pwd\npwd\n'
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['status'] == webssh.AGENT_STATUS_COMPLETED

    client.disconnect()


def test_provider_adapter_receives_context_and_exposes_run_metadata():
    provider = RecordingProvider('adapter\n')
    webssh.set_agent_provider_for_test(provider)
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(fill='screen'))
    assert last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'

    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)

    assert provider.contexts
    assert provider.contexts[-1]['terminal_id'] == webssh.TERMINAL_ID_MAIN
    assert provider.contexts[-1]['active_screen']['lines'] == ['screen', 'screen']
    assert provider.contexts[-1]['terminal_session']['session_id'] == action['session_id']
    assert provider.runs[-1]['run_id'].startswith('agr_')
    assert action['run_id'] == provider.runs[-1]['run_id']
    assert action['provider_name'] == 'recording'
    assert action['provider_version'] == 'test-1'
    assert action['provider_status'] == webssh.AGENT_RUN_STATUS_COMPLETED
    assert action['escaped_preview'] == 'adapter\\n'
    assert bridge.writes == []

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'session_id': action['session_id'],
        'viewer_id': action['viewer_id'],
        'agent_binding_id': action['agent_binding_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    })
    assert ''.join(bridge.writes) == 'adapter\n'

    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    event_types = [event['event_type'] for event in audit_events]
    assert webssh.AGENT_AUDIT_PROVIDER_RUN_REQUEST in event_types
    assert webssh.AGENT_AUDIT_PROVIDER_RUN_START in event_types
    assert webssh.AGENT_AUDIT_PROVIDER_RUN_COMPLETE in event_types
    provider_events = [
        event for event in audit_events
        if event['event_type'] in {
            webssh.AGENT_AUDIT_PROVIDER_RUN_REQUEST,
            webssh.AGENT_AUDIT_PROVIDER_RUN_START,
            webssh.AGENT_AUDIT_PROVIDER_RUN_COMPLETE,
        }
    ]
    assert provider_events
    for event in provider_events:
        assert event['provider_name'] == 'recording'
        assert event['provider_version'] == 'test-1'
        assert event['run_id'] == action['run_id']

    client.disconnect()


def test_static_env_provider_is_explicit_adapter():
    provider = webssh.StaticEnvAgentProvider('static-adapter\n')
    webssh.set_agent_provider_for_test(provider)
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })

    assert ''.join(bridge.writes) == 'static-adapter\n'
    result = last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)
    assert result['status'] == webssh.AGENT_STATUS_COMPLETED
    assert result['provider_name'] == 'static_env'

    client.disconnect()


def test_provider_failure_is_typed_and_does_not_write():
    webssh.set_agent_provider_for_test(FailingProvider())
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })

    assert bridge.writes == []
    result = last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] == webssh.AGENT_ERROR_PROVIDER_FAILED
    assert received_events(client, webssh.AGENT_EVENT_ACTION_REQUEST) == []
    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    errors = [
        event for event in audit_events
        if event['event_type'] == webssh.AGENT_AUDIT_PROVIDER_RUN_ERROR
    ]
    assert errors
    assert errors[-1]['error_code'] == webssh.AGENT_ERROR_PROVIDER_FAILED
    assert errors[-1]['status'] == webssh.AGENT_RUN_STATUS_FAILED

    client.disconnect()


def test_invalid_provider_proposal_is_rejected_before_action_creation():
    webssh.set_agent_provider_for_test(InvalidProvider())
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })

    assert bridge.writes == []
    result = last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] == webssh.AGENT_ERROR_PROVIDER_INVALID_PROPOSAL
    assert received_events(client, webssh.AGENT_EVENT_ACTION_REQUEST) == []
    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert not [
        event for event in audit_events
        if event['event_type'] == webssh.AGENT_AUDIT_PROPOSAL_CREATED
    ]

    client.disconnect()


def test_external_agent_token_requires_enabled_agent_panel():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    token, record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert token is None
    assert record is None
    assert error_code == webssh.AGENT_ERROR_NOT_ATTACHED

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'disabled',
    })
    token, record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert token is None
    assert record is None
    assert error_code == webssh.AGENT_ERROR_EXTERNAL_AGENT_DISABLED

    client.disconnect()


def test_external_agent_can_attach_and_read_authorized_screen():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(fill='screen'))
    assert last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'terminal-output\n',
    })

    token, record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    assert record['idle_timeout_seconds'] == webssh.AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS
    assert record['expires_at'] > webssh.time.time()
    attach = webssh.process_external_agent_command({
        'op': 'attach',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert attach['status'] == 'ok'
    assert attach['external_agent_id'] == record['external_agent_id']
    assert attach['mode'] == webssh.AGENT_MODE_OBSERVE

    screen = webssh.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert screen['status'] == 'ok'
    assert screen['screen']['lines'] == ['screen', 'screen']
    assert screen['state']['session_id'] == attach['session_id']

    tail = webssh.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
    })
    assert tail['status'] == 'ok'
    assert tail['since_output_seq'] == 0
    assert tail['first_available_output_seq'] == 1
    assert tail['dropped_before_output_seq'] == 0
    assert tail['gap']['detected'] is False
    assert tail['events'][-1]['data'] == 'terminal-output\n'

    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    event_types = [event['event_type'] for event in audit_events]
    assert webssh.AGENT_AUDIT_EXTERNAL_AGENT_TOKEN_CREATED in event_types
    assert webssh.AGENT_AUDIT_EXTERNAL_AGENT_ATTACHED in event_types
    assert webssh.AGENT_AUDIT_EXTERNAL_AGENT_SCREEN in event_types
    assert webssh.AGENT_AUDIT_EXTERNAL_AGENT_TAIL in event_types
    for event in audit_events:
        assert 'token' not in event
        assert 'token_hash' not in event

    client.disconnect()


def test_external_agent_screen_tail_lines_and_region_reduce_viewport_payload():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    lines = ['line-0', 'line-1', 'line-2', 'line-3', 'line-4']
    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(
        webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT,
        valid_viewport_snapshot(seq=1, cols=10, lines=lines),
    )
    assert last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    tail_screen = webssh.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'tail_lines': 2,
    })
    assert tail_screen['status'] == 'ok'
    assert tail_screen['screen']['lines'] == ['line-3', 'line-4']
    assert tail_screen['screen']['line_count'] == 2
    assert tail_screen['screen']['original_line_count'] == 5
    assert tail_screen['screen']['truncated'] is True
    assert tail_screen['screen']['region'] == {
        'top': 3,
        'bottom': 5,
        'tail_lines': 2,
    }
    assert tail_screen['screen']['provisional'] is True
    assert tail_screen['screen']['snapshot_seq'] == 1

    region_screen = webssh.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'region': {
            'top': 1,
            'bottom': 4,
        },
    })
    assert region_screen['status'] == 'ok'
    assert region_screen['screen']['lines'] == ['line-1', 'line-2', 'line-3']
    assert region_screen['screen']['line_count'] == 3
    assert region_screen['screen']['original_line_count'] == 5
    assert region_screen['screen']['region'] == {'top': 1, 'bottom': 4}

    invalid = webssh.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'tail_lines': 1,
        'region': {
            'top': 0,
            'bottom': 1,
        },
    })
    assert invalid['status'] == webssh.AGENT_STATUS_FAILED
    assert invalid['error_code'] == webssh.AGENT_ERROR_ACTION_INVALID_DATA

    client.disconnect()


def test_external_agent_render_requests_browser_viewport_png():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)
    one_pixel_png = (
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8'
        '/x8AAwMCAO+/p9sAAAAASUVORK5CYII='
    )

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    bridge.update_terminal_size(100, 30)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'render-source\n',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    hello = webssh.process_external_agent_command({
        'op': 'hello',
        'token': token,
    })
    assert 'render' in hello['capabilities']

    result_box = {}

    def request_render():
        result_box['render'] = webssh.process_external_agent_command({
            'op': 'render',
            'token': token,
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'wait_ms': 1000,
        })

    thread = threading.Thread(target=request_render)
    thread.start()
    request_event = wait_for_event(client, webssh.AGENT_EVENT_VIEWPORT_RENDER_REQUEST)
    request_payload = request_event['args'][0]
    assert request_payload['terminal_id'] == webssh.TERMINAL_ID_MAIN
    assert request_payload['render_type'] == 'xterm_viewport'
    assert request_payload['mime_type'] == 'image/png'
    assert request_payload['cols'] == 100
    assert request_payload['rows'] == 30
    assert request_payload['output_seq'] == bridge.output_seq

    client.emit(webssh.AGENT_EVENT_VIEWPORT_RENDER_RESULT, {
        'request_id': request_payload['request_id'],
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'render_type': 'xterm_viewport',
        'mime_type': 'image/png',
        'image_base64': one_pixel_png,
        'cols': 100,
        'rows': 30,
        'pixel_width': 1,
        'pixel_height': 1,
        'output_seq': bridge.output_seq,
        'captured_at': '2026-05-22T00:00:00.000Z',
    })
    thread.join(timeout=2)
    assert not thread.is_alive()

    result = result_box['render']
    assert result['status'] == 'ok'
    assert result['render']['request_id'] == request_payload['request_id']
    assert result['render']['render_type'] == 'xterm_viewport'
    assert result['render']['mime_type'] == 'image/png'
    assert result['render']['image_base64'] == one_pixel_png
    assert result['render']['image_byte_length'] > 0
    assert result['render']['output_seq'] == bridge.output_seq

    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    render_audit = [
        event for event in audit_events
        if event['event_type'] == webssh.AGENT_AUDIT_EXTERNAL_AGENT_RENDER
    ][-1]
    assert render_audit['request_id'] == request_payload['request_id']
    assert render_audit['image_byte_length'] == result['render']['image_byte_length']
    assert 'image_base64' not in render_audit

    client.disconnect()


def test_external_agent_render_timeout_is_typed():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = webssh.process_external_agent_command({
        'op': 'render',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'wait_ms': 10,
    })
    assert result['status'] == webssh.AGENT_STATUS_FAILED
    assert result['error_code'] == webssh.AGENT_ERROR_RENDER_TIMEOUT

    client.disconnect()


def test_external_agent_tail_reports_gap_metadata():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    bridge.output_seq = 5
    bridge.replay_buffer.clear()
    bridge.replay_buffer.append({
        'message_type': 'terminal',
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'fifth\n',
        'output_seq': 5,
    })
    bridge.replay_buffer_bytes = len('fifth\n')
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    tail = webssh.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'since_output_seq': 1,
        'limit': 10,
    })
    assert tail['status'] == 'ok'
    assert tail['output_seq'] == 5
    assert tail['since_output_seq'] == 1
    assert tail['first_available_output_seq'] == 5
    assert tail['dropped_before_output_seq'] == 4
    assert tail['gap'] == {
        'detected': True,
        'from_output_seq': 2,
        'to_output_seq': 4,
        'missing_count': 3,
    }
    assert tail['events'] == [{
        'message_type': 'terminal',
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'fifth\n',
        'output_seq': 5,
    }]

    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    tail_audit = [
        event for event in audit_events
        if event['event_type'] == webssh.AGENT_AUDIT_EXTERNAL_AGENT_TAIL
    ][-1]
    assert tail_audit['gap']['detected'] is True

    client.disconnect()


def test_external_agent_tail_limit_preserves_cursor_order():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    for index in range(1, 5):
        bridge.emit_output({
            'message_type': 'terminal',
            'data': f'line-{index}\n',
        })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    first_page = webssh.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
        'limit': 2,
    })
    assert first_page['status'] == 'ok'
    assert first_page['gap']['detected'] is False
    assert [event['output_seq'] for event in first_page['events']] == [1, 2]

    second_page = webssh.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'since_output_seq': first_page['events'][-1]['output_seq'],
        'limit': 2,
    })
    assert second_page['status'] == 'ok'
    assert second_page['gap']['detected'] is False
    assert [event['output_seq'] for event in second_page['events']] == [3, 4]

    client.disconnect()


def test_external_agent_tail_wait_returns_after_new_output():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_tail():
        result_box['tail'] = webssh.process_external_agent_command({
            'op': 'tail',
            'token': token,
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'since_output_seq': 0,
            'limit': 10,
            'wait_ms': 1000,
        })

    thread = threading.Thread(target=request_tail)
    thread.start()
    time.sleep(0.05)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'waited-line\n',
    })
    thread.join(timeout=2)
    assert not thread.is_alive()
    tail = result_box['tail']
    assert tail['status'] == 'ok'
    assert tail['wait_ms'] == 1000
    assert [event['data'] for event in tail['events']] == ['waited-line\n']

    client.disconnect()


def test_external_agent_tail_wait_times_out_without_output():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    started = time.monotonic()
    tail = webssh.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
        'limit': 10,
        'wait_ms': 20,
    })
    elapsed = time.monotonic() - started
    assert tail['status'] == 'ok'
    assert tail['wait_ms'] == 20
    assert tail['events'] == []
    assert elapsed < 0.5

    client.disconnect()


def test_external_agent_tail_wait_stops_on_pause():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_tail():
        result_box['tail'] = webssh.process_external_agent_command({
            'op': 'tail',
            'token': token,
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'since_output_seq': 0,
            'limit': 10,
            'wait_ms': 1000,
        })

    thread = threading.Thread(target=request_tail)
    thread.start()
    time.sleep(0.05)
    client.emit(webssh.AGENT_EVENT_PAUSE, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result_box['tail']['status'] == webssh.AGENT_STATUS_FAILED
    assert result_box['tail']['error_code'] == webssh.AGENT_ERROR_PAUSED

    client.disconnect()


def test_external_agent_observe_cannot_send():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'blocked\n',
    })
    assert result['error_code'] == webssh.AGENT_ERROR_MODE_NOT_WRITABLE
    assert bridge.writes == []

    client.disconnect()


def test_external_agent_approval_send_waits_for_human_approval():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'approved-external\n',
    })
    assert result['status'] == webssh.AGENT_STATUS_PENDING_APPROVAL
    assert result['requires_approval'] is True
    assert result['provider_name'] == 'external_agent'
    assert bridge.writes == []
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)
    assert action['proposal_id'] == result['proposal_id']

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'session_id': action['session_id'],
        'viewer_id': action['viewer_id'],
        'agent_binding_id': action['agent_binding_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    })
    assert ''.join(bridge.writes) == 'approved-external\n'

    client.disconnect()


def test_external_agent_direct_send_uses_agent_gate():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'direct-external\n',
    })
    assert result['status'] == webssh.AGENT_STATUS_COMPLETED
    assert result['bytes_written'] == len('direct-external\n')
    assert ''.join(bridge.writes) == 'direct-external\n'
    action_result = last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)
    assert action_result['proposal_id'] == result['proposal_id']

    client.disconnect()


def test_external_agent_direct_send_capture_returns_tail_after_write():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'old-output\n',
    })

    result_box = {}

    def request_send_capture():
        result_box['send'] = webssh.process_external_agent_command({
            'op': 'send',
            'token': token,
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'data': 'pwd\n',
            'capture': True,
            'wait_ms': 1000,
            'settle_ms': 10,
        })

    thread = threading.Thread(target=request_send_capture)
    thread.start()
    wait_until(lambda: ''.join(bridge.writes) == 'pwd\n', 'send capture did not write input')
    assert ''.join(bridge.writes) == 'pwd\n'
    bridge.emit_output({
        'message_type': 'terminal',
        'data': '/tmp/project\n',
    })
    thread.join(timeout=2)
    assert not thread.is_alive()

    result = result_box['send']
    assert result['status'] == webssh.AGENT_STATUS_COMPLETED
    assert result['bytes_written'] == len('pwd\n')
    assert result['before_output_seq'] == 1
    assert result['after_output_seq'] == 2
    assert result['capture']['requested'] is True
    assert result['capture']['status'] == 'ok'
    assert result['capture']['mode'] == 'tail'
    assert result['capture']['timed_out'] is False
    assert result['capture']['settled'] is True
    assert result['capture']['before_output_seq'] == 1
    assert result['capture']['after_output_seq'] == 2
    assert [event['data'] for event in result['capture']['events']] == ['/tmp/project\n']

    client.disconnect()


def test_external_agent_send_wait_times_out_without_output():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = webssh.process_external_agent_command({
        'op': 'send-wait',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'no-output\n',
        'wait_ms': 10,
        'settle_ms': 0,
    })
    assert result['status'] == webssh.AGENT_STATUS_COMPLETED
    assert result['bytes_written'] == len('no-output\n')
    assert ''.join(bridge.writes) == 'no-output\n'
    assert result['capture']['requested'] is True
    assert result['capture']['status'] == 'timeout'
    assert result['capture']['timed_out'] is True
    assert result['capture']['settled'] is False
    assert result['capture']['events'] == []

    client.disconnect()


def test_external_agent_approval_send_capture_is_pending_without_capture():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'needs-human\n',
        'capture': True,
    })
    assert result['status'] == webssh.AGENT_STATUS_PENDING_APPROVAL
    assert result['requires_approval'] is True
    assert result['capture'] == {
        'status': 'skipped',
        'reason': 'pending_approval',
        'requested': True,
    }
    assert bridge.writes == []

    client.disconnect()


def test_external_agent_send_capture_reports_pause_as_nested_capture_error():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_send_capture():
        result_box['send'] = webssh.process_external_agent_command({
            'op': 'send',
            'token': token,
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'data': 'pause-after-write\n',
            'capture': True,
            'wait_ms': 1000,
            'settle_ms': 0,
        })

    thread = threading.Thread(target=request_send_capture)
    thread.start()
    wait_until(lambda: ''.join(bridge.writes) == 'pause-after-write\n', 'send capture did not write before pause')
    client.emit(webssh.AGENT_EVENT_PAUSE, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    thread.join(timeout=2)
    assert not thread.is_alive()

    result = result_box['send']
    assert result['status'] == webssh.AGENT_STATUS_COMPLETED
    assert result['bytes_written'] == len('pause-after-write\n')
    assert result['capture'] == {
        'status': webssh.AGENT_STATUS_FAILED,
        'error_code': webssh.AGENT_ERROR_PAUSED,
        'requested': True,
    }

    client.disconnect()


def test_human_input_lease_blocks_external_agent_send():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    client.emit('ssh_input', {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'human\n',
    })
    state = last_payload(client, webssh.AGENT_EVENT_STATE)
    assert state['human_activity_seq'] == 1
    assert state['human_input_lease_active'] is True
    assert ''.join(bridge.writes) == 'human\n'

    blocked = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert blocked['status'] == webssh.AGENT_STATUS_FAILED
    assert blocked['error_code'] == webssh.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert ''.join(bridge.writes) == 'human\n'

    with webssh.agent_lock:
        state = webssh.get_agent_state(session_token, webssh.TERMINAL_ID_MAIN, sid)
        state.human_input_lease_expires_at = webssh.time.time() - 1

    allowed = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert allowed['status'] == webssh.AGENT_STATUS_COMPLETED
    assert ''.join(bridge.writes) == 'human\nagent\n'

    client.disconnect()


def test_human_input_lease_is_terminal_scoped_across_viewers():
    flask_client = make_flask_client()
    client_a = make_socket_client(flask_client)
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid_a = current_sid_for_session(session_token)
    client_b = make_socket_client(flask_client)

    client_a.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client_a.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid_a,
    )
    assert error_code is None

    client_b.emit('ssh_input', {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'viewer-b\n',
    })
    state = last_payload(client_a, webssh.AGENT_EVENT_STATE)
    assert state['human_activity_seq'] == 1
    assert state['human_input_lease_active'] is True
    assert ''.join(bridge.writes) == 'viewer-b\n'

    blocked = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert blocked['status'] == webssh.AGENT_STATUS_FAILED
    assert blocked['error_code'] == webssh.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert ''.join(bridge.writes) == 'viewer-b\n'

    client_a.disconnect()
    client_b.disconnect()


def test_human_input_lease_blocks_external_approval_proposal():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    client.emit('ssh_input', {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'human\n',
    })
    client.get_received()

    blocked = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert blocked['status'] == webssh.AGENT_STATUS_FAILED
    assert blocked['error_code'] == webssh.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert received_events(client, webssh.AGENT_EVENT_ACTION_REQUEST) == []

    client.disconnect()


def test_human_input_lock_blocks_external_approval_until_lease_recorded():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    client.get_received()

    human_started = webssh.threading.Event()
    human_can_record = webssh.threading.Event()
    send_done = webssh.threading.Event()
    result_holder = {}

    def hold_human_input_lock():
        with bridge.input_lock:
            human_started.set()
            assert human_can_record.wait(timeout=2)
            with webssh.agent_lock:
                webssh.note_agent_human_input_for_terminal(session_token, webssh.TERMINAL_ID_MAIN)
            bridge.write('human\n')

    def external_send():
        result_holder['result'] = webssh.process_external_agent_command({
            'op': 'send',
            'token': token,
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'data': 'agent\n',
        })
        send_done.set()

    human_thread = webssh.threading.Thread(target=hold_human_input_lock)
    send_thread = webssh.threading.Thread(target=external_send)
    human_thread.start()
    assert human_started.wait(timeout=2)
    send_thread.start()
    webssh.time.sleep(0.05)
    assert not send_done.is_set()

    human_can_record.set()
    human_thread.join(timeout=2)
    send_thread.join(timeout=2)
    assert send_done.is_set()
    blocked = result_holder['result']
    assert blocked['status'] == webssh.AGENT_STATUS_FAILED
    assert blocked['error_code'] == webssh.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert received_events(client, webssh.AGENT_EVENT_ACTION_REQUEST) == []
    assert ''.join(bridge.writes) == 'human\n'

    client.disconnect()


def test_external_agent_privacy_and_disabled_state_block_visibility_and_send():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    client.emit(webssh.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'privacy_state': webssh.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    screen = webssh.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    render = webssh.process_external_agent_command({
        'op': 'render',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'wait_ms': 10,
    })
    send = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'blocked\n',
    })
    assert screen['error_code'] == webssh.AGENT_ERROR_PRIVACY_BLOCKED
    assert render['error_code'] == webssh.AGENT_ERROR_PRIVACY_BLOCKED
    assert send['error_code'] == webssh.AGENT_ERROR_PRIVACY_BLOCKED
    assert bridge.writes == []

    client.emit(webssh.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'privacy_state': webssh.AGENT_PRIVACY_NORMAL,
    })
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'disabled',
    })
    result = webssh.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == webssh.AGENT_ERROR_EXTERNAL_AGENT_DISABLED

    client.disconnect()


def test_external_agent_token_revoke_and_terminal_close_invalidate_access():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    revoked = webssh.process_external_agent_command({
        'op': 'revoke',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert revoked['status'] == 'ok'
    result = webssh.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == webssh.AGENT_ERROR_EXTERNAL_AGENT_REVOKED

    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    client.emit('close_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    result = webssh.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == webssh.AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED

    client.disconnect()


def test_external_agent_expired_and_wrong_terminal_tokens_are_rejected():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
        idle_timeout_seconds=-1,
    )
    assert error_code is None
    result = webssh.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == webssh.AGENT_ERROR_EXTERNAL_AGENT_EXPIRED

    token, record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
        idle_timeout_seconds=60,
    )
    assert error_code is None
    first_expires_at = record['expires_at']
    stored = webssh.external_agent_attach_store._tokens[record['token_hash']]
    stored['expires_at'] = webssh.time.time() + 1
    result = webssh.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert result['status'] == 'ok'
    assert stored['expires_at'] > first_expires_at

    token, _record, error_code = webssh.mint_external_agent_attach_token(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    result = webssh.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': 'other',
    })
    assert result['error_code'] == webssh.AGENT_ERROR_TERMINAL_MISMATCH

    client.disconnect()


def test_external_agent_http_bridge_mints_token_and_accepts_cli_command():
    flask_client = make_flask_client()
    client = make_socket_client(flask_client)
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    state = last_payload(client, webssh.AGENT_EVENT_STATE)

    original_handoff_path = webssh.EXTERNAL_AGENT_HANDOFF_PATH
    with tempfile.TemporaryDirectory(prefix='webssh-agent-smoke-') as handoff_dir:
        webssh.EXTERNAL_AGENT_HANDOFF_PATH = Path(handoff_dir) / 'webssh_external_agent_handoff.json'
        try:
            response = flask_client.post('/agent/external/token', json={
                'terminal_id': webssh.TERMINAL_ID_MAIN,
                'viewer_id': state['viewer_id'],
                'agent_binding_id': state['agent_binding_id'],
                'mode_version': state['mode_version'],
                'privacy_version': state['privacy_version'],
            })
            assert response.status_code == 200
            token_payload = response.get_json()
            assert token_payload['status'] == 'ok'
            assert token_payload['token'].startswith('agt_')
            assert token_payload['handoff_schema'] == 'webssh_external_agent_handoff'
            assert token_payload['schema_version'] == 1
            assert token_payload['protocol_version'] == webssh.EXTERNAL_AGENT_PROTOCOL_VERSION
            assert 'render' in token_payload['capabilities']
            assert 'send_capture' in token_payload['capabilities']
            assert token_payload['transport']['type'] == 'loopback_http_json'
            assert token_payload['transport']['loopback_only'] is True
            assert token_payload['operations']['render']['op'] == 'render'
            assert token_payload['operations']['screen_tail'] == {'op': 'screen', 'tail_lines': 12}
            assert token_payload['operations']['screen_region'] == {
                'op': 'screen',
                'region': {
                    'top': 0,
                    'bottom': 12,
                },
            }
            assert token_payload['operations']['tail']['wait_ms'] == webssh.AGENT_EXTERNAL_TAIL_MAX_WAIT_MS
            assert token_payload['operations']['send_wait']['op'] == 'send-wait'
            assert token_payload['expires_at'] > webssh.time.time()
            assert token_payload['security']['token_lifetime'] == 'idle_timeout'
            assert token_payload['security']['idle_timeout_seconds'] == (
                webssh.AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS
            )
            assert token_payload['security']['remote_use_requires_loopback_tunnel'] is True
            assert token_payload['cli_command'].endswith("send --text 'pwd\n'")
            assert token_payload['cli_commands']['hello'].endswith('hello')
            assert token_payload['cli_commands']['render'].endswith('render')
            assert token_payload['cli_commands']['screen_tail'].endswith("screen --tail-lines 12")
            assert token_payload['cli_commands']['screen_region'].endswith("screen --region 0:12")
            assert 'scripts/webssh_agent_repl.py' in token_payload['cli_commands']['repl']
            assert 'scripts/webssh_agent_jsonl.py' in token_payload['cli_commands']['jsonl']
            handoff = Path(token_payload['handoff_path'])
            assert handoff == webssh.EXTERNAL_AGENT_HANDOFF_PATH
            assert handoff.parent == Path(handoff_dir)
            assert handoff.is_file()
            handoff_payload = webssh.json.loads(handoff.read_text(encoding='utf-8'))
            assert handoff_payload['token'] == token_payload['token']
            assert handoff_payload['cli_command'] == token_payload['cli_command']
            assert handoff_payload['capabilities'] == token_payload['capabilities']
            assert handoff_payload['cli_commands']['render'] == token_payload['cli_commands']['render']
        finally:
            webssh.EXTERNAL_AGENT_HANDOFF_PATH = original_handoff_path

    response = flask_client.post('/agent/external/command', json={
        'op': 'send',
        'token': token_payload['token'],
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'http-external\n',
    })
    assert response.status_code == 200
    result = response.get_json()
    assert result['status'] == webssh.AGENT_STATUS_COMPLETED
    assert ''.join(bridge.writes) == 'http-external\n'

    webssh.AGENT_EXTERNAL_DEV_TOKEN_ENABLED = True
    try:
        response = flask_client.post('/agent/external/dev-command', json={
            'op': 'send',
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'data': 'http-dev-external\n',
        })
    finally:
        webssh.AGENT_EXTERNAL_DEV_TOKEN_ENABLED = False
    assert response.status_code == 200
    result = response.get_json()
    assert result['status'] == webssh.AGENT_STATUS_COMPLETED
    assert result['dev_token'] is True
    assert ''.join(bridge.writes) == 'http-external\nhttp-dev-external\n'

    client.disconnect()


def test_external_agent_startup_lines_point_to_launch_handoff():
    original_https_enabled = webssh.HTTPS_ENABLED
    lines = webssh.build_external_agent_startup_lines()
    joined = '\n'.join(lines)
    hello_line = next(line for line in lines if line.startswith('External Agent CLI hello: '))
    render_line = next(line for line in lines if line.startswith('External Agent CLI render: '))

    assert webssh.quote_local_command(['C:\\Program Files\\Python\\python.exe'], platform_name='win32') == (
        '"C:\\Program Files\\Python\\python.exe"'
    )
    assert str(webssh.EXTERNAL_AGENT_HANDOFF_PATH) in joined
    assert str(webssh.APP_DIR / 'scripts' / 'webssh_agent_cli.py') in joined
    assert webssh.sys.executable in joined
    assert '--handoff' in hello_line
    assert str(webssh.EXTERNAL_AGENT_HANDOFF_PATH) in hello_line
    assert hello_line.endswith(' hello')
    assert '--handoff' in render_line
    assert str(webssh.EXTERNAL_AGENT_HANDOFF_PATH) in render_line
    assert render_line.endswith(' render')
    assert 'after browser Agent attach and external token mint' in joined
    assert 'explicit --url, --token, and --terminal' in joined
    try:
        webssh.HTTPS_ENABLED = True
        tls_lines = webssh.build_external_agent_startup_lines()
    finally:
        webssh.HTTPS_ENABLED = original_https_enabled
    tls_joined = '\n'.join(tls_lines)
    if webssh.LOCAL_CA_CERT_PATH.is_file() and not (webssh.CLI_ARGS.certfile or webssh.CLI_ARGS.keyfile):
        assert '--ca-file' in tls_joined
        assert str(webssh.LOCAL_CA_CERT_PATH) in tls_joined


def test_wsl_local_shell_choice_is_structured_and_wsl_only():
    original_is_wsl = webssh.is_wsl
    plugin = webssh.LocalShellBackendPlugin()
    try:
        webssh.is_wsl = lambda: True
        with webssh.app.test_request_context('/'):
            option = plugin.build_policy_option(browser_authorized=False)
        shell_options = option['shell_options']
        assert [item['kind'] for item in shell_options] == ['bash', 'cmd', 'powershell']
        assert option['default_shell_kind'] == 'bash'

        payload, error = plugin.validate_start_payload(
            {'local_shell_kind': 'cmd'},
            webssh.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error is None
        assert payload['local_shell_config']['shell_kind'] == 'cmd'
        assert payload['local_shell_config']['terminal_label'] == 'cmd.exe'

        payload, error = plugin.validate_start_payload(
            {},
            webssh.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error is None
        assert payload['local_shell_config']['shell_kind'] == 'bash'

        _payload, error = plugin.validate_start_payload(
            {'local_shell_kind': 'zsh'},
            webssh.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error['error_code'] == 'local_shell_invalid_kind'

        webssh.is_wsl = lambda: False
        _payload, error = plugin.validate_start_payload(
            {'local_shell_kind': 'cmd'},
            webssh.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error['error_code'] == 'local_shell_kind_not_supported'
    finally:
        webssh.is_wsl = original_is_wsl


def test_agent_audit_records_typed_events_without_raw_action_data():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    assert last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'

    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)
    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'],
    })
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['status'] == webssh.AGENT_STATUS_COMPLETED

    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    event_types = [event['event_type'] for event in audit_events]
    assert webssh.AGENT_AUDIT_VIEWER_ATTACH in event_types
    assert webssh.AGENT_AUDIT_MODE_SET in event_types
    assert webssh.AGENT_AUDIT_PROVIDER_RUN_REQUEST in event_types
    assert webssh.AGENT_AUDIT_CONTEXT_BUILT in event_types
    assert webssh.AGENT_AUDIT_PROPOSAL_CREATED in event_types
    assert webssh.AGENT_AUDIT_ACTION_APPROVE in event_types
    assert webssh.AGENT_AUDIT_ACTION_RESULT in event_types

    context_events = [
        event for event in audit_events
        if event['event_type'] == webssh.AGENT_AUDIT_CONTEXT_BUILT
    ]
    assert context_events[-1]['context']['active_screen_source'] == 'browser_viewport_snapshot'
    assert context_events[-1]['context']['context_allowed'] is True

    action_events = [
        event for event in audit_events
        if event.get('action')
    ]
    assert action_events
    for event in action_events:
        assert 'data' not in event['action']
        assert 'escaped_preview' not in event['action']

    client.disconnect()


def test_wrong_sid_cannot_approve_action():
    client_a = make_client()
    session_token = current_session_token()
    client_b = make_client()
    bridge = add_dummy_bridge(session_token)

    client_a.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client_a.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client_a.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'secret\n',
    })
    action = last_payload(client_a, webssh.AGENT_EVENT_ACTION_REQUEST)

    client_b.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    assert last_payload(client_b, webssh.AGENT_EVENT_ACTION_RESULT)['error_code'] == webssh.AGENT_ERROR_NOT_ATTACHED

    client_a.disconnect()
    client_b.disconnect()


def test_stale_mode_version_cannot_approve_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'versioned\n',
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'] + 1,
    })
    assert bridge.writes == []
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['error_code'] == webssh.AGENT_ERROR_STALE_MODE_VERSION

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'],
    })
    assert bridge.writes == ['versioned\n']

    client.disconnect()


def test_stale_privacy_version_cannot_approve_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'privacy-versioned\n',
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)

    client.emit(webssh.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'privacy_state': webssh.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    assert last_payload(client, webssh.AGENT_EVENT_STATE)['privacy_version'] == action['privacy_version'] + 1

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    })
    assert bridge.writes == []
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['error_code'] == webssh.AGENT_ERROR_STALE_PROPOSAL

    client.disconnect()


def test_mode_change_cancels_pending_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'stale\n',
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)

    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'observe',
    })
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['error_code'] == webssh.AGENT_REASON_MODE_CHANGED

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['error_code'] == webssh.AGENT_ERROR_ACTION_NOT_PENDING

    client.disconnect()


def test_terminal_close_invalidates_pending_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'after-close\n',
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)
    webssh.agent_user_input_metadata_store.append_input(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        'manual-input\n',
    )

    client.emit('close_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    audit_events = webssh.agent_audit_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert any(event['event_type'] == webssh.AGENT_AUDIT_TERMINAL_CLEANUP for event in audit_events)
    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    assert webssh.agent_user_input_metadata_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN) == []
    assert last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)['error_code'] == webssh.AGENT_ERROR_NOT_ATTACHED

    client.disconnect()


def test_disconnect_invalidates_agent_state():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    assert webssh.agent_states
    client.disconnect()
    assert not webssh.agent_states


def test_stale_epoch_write_is_rejected():
    session_token = 'session-a'
    sid = 'sid-a'
    bridge = add_dummy_bridge(session_token)
    state = webssh.get_or_create_agent_state(session_token, webssh.TERMINAL_ID_MAIN, sid)
    state.mode = webssh.AGENT_MODE_DIRECT_ACTIVE
    action, error_code = webssh.build_agent_action(
        state,
        {
            'action_type': webssh.AGENT_ACTION_TERMINAL_INPUT,
            'terminal_id': webssh.TERMINAL_ID_MAIN,
            'data': 'stale\n',
        },
        requires_approval=False,
    )
    assert error_code is None
    stale_epoch = action['control_epoch']
    state.control_epoch += 1

    ok, result = webssh.write_agent_terminal_input(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
        action['action_id'],
        stale_epoch,
    )
    assert ok is False
    assert result['error_code'] == webssh.AGENT_ERROR_STALE_EPOCH
    assert bridge.writes == []


def test_transcript_store_sanitizes_terminal_output():
    session_token = 'session-a'
    bridge = add_dummy_bridge(session_token)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': '\x1b[31mred\x1b[0m\r\nnext\x00\x1b]0;title\x07\n',
    })

    transcript = webssh.agent_transcript_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert len(transcript) == 1
    assert transcript[0]['data'] == 'red\nnext\n'
    assert transcript[0]['untrusted'] is True


def test_terminal_bridge_tracks_shared_session_metadata():
    session_token = 'session-a'
    bridge = add_dummy_bridge(session_token)
    bridge.update_terminal_size(132, 43)

    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'first\n',
    })
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'second\n',
    })

    assert bridge.output_seq == 2
    assert bridge.replay_buffer[0]['output_seq'] == 1
    assert bridge.replay_buffer[1]['output_seq'] == 2
    metadata = bridge.session_metadata()
    assert metadata['cols'] == 132
    assert metadata['rows'] == 43
    assert metadata['output_seq'] == 2


def test_ssh_input_records_agent_metadata_after_validation():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit('ssh_input', {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'whoami\nnext',
    })

    metadata = webssh.agent_user_input_metadata_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert bridge.writes == ['whoami\nnext']
    assert len(metadata) == 1
    assert metadata[0]['terminal_id'] == webssh.TERMINAL_ID_MAIN
    assert metadata[0]['byte_length'] == len('whoami\nnext'.encode('utf-8'))
    assert metadata[0]['line_count'] == 2
    assert metadata[0]['contains_control_chars'] is False
    assert metadata[0]['escaped_preview'] == 'whoami\\nnext'
    assert 'data' not in metadata[0]

    client.disconnect()


def test_agent_input_metadata_bounds_and_sanitized_preview():
    session_token = 'session-a'
    long_input = 'x' * (webssh.AGENT_USER_INPUT_PREVIEW_CHARS + 20)

    webssh.agent_user_input_metadata_store.append_input(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        long_input,
    )
    metadata = webssh.agent_user_input_metadata_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert metadata[0]['escaped_preview'].endswith('...')
    assert len(metadata[0]['escaped_preview']) == webssh.AGENT_USER_INPUT_PREVIEW_CHARS + 3

    webssh.agent_user_input_metadata_store.append_input(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        'stop\x03',
    )
    metadata = webssh.agent_user_input_metadata_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert metadata[-1]['contains_control_chars'] is True
    assert 'escaped_preview' not in metadata[-1]

    for index in range(webssh.AGENT_USER_INPUT_METADATA_MAX_EVENTS + 1):
        webssh.agent_user_input_metadata_store.append_input(
            session_token,
            webssh.TERMINAL_ID_MAIN,
            f'cmd-{index}\n',
        )
    metadata = webssh.agent_user_input_metadata_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert len(metadata) == webssh.AGENT_USER_INPUT_METADATA_MAX_EVENTS
    assert metadata[0]['escaped_preview'] == 'cmd-1\\n'


def test_privacy_state_blocks_agent_context_and_redacts_input_metadata():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(webssh.AGENT_EVENT_ATTACH, {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_MODE_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(webssh.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'privacy_state': webssh.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    state = last_payload(client, webssh.AGENT_EVENT_STATE)
    assert state['privacy_state'] == webssh.AGENT_PRIVACY_PRIVATE_INPUT
    assert state['privacy_version'] == 1

    client.emit('ssh_input', {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'secret value\n',
    })
    metadata = webssh.agent_user_input_metadata_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert bridge.writes == ['secret value\n']
    assert metadata[-1]['privacy_state'] == webssh.AGENT_PRIVACY_PRIVATE_INPUT
    assert metadata[-1]['redacted'] is True
    assert 'escaped_preview' not in metadata[-1]

    context = webssh.build_agent_context(session_token, webssh.TERMINAL_ID_MAIN, current_sid_for_session(session_token))
    assert context['privacy']['context_allowed'] is False
    assert context['human_input_metadata'] == []

    client.emit(webssh.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    result = last_payload(client, webssh.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] == webssh.AGENT_ERROR_PRIVACY_BLOCKED
    assert bridge.writes == ['secret value\n']

    client.disconnect()


def test_ssh_input_does_not_record_invalid_or_oversized_metadata():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit('ssh_input', {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 123,
    })
    client.emit('ssh_input', {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'x' * (webssh.MAX_SSH_INPUT_BYTES + 1),
    })

    metadata = webssh.agent_user_input_metadata_store.get_recent(session_token, webssh.TERMINAL_ID_MAIN)
    assert metadata == []
    assert bridge.writes == []

    client.disconnect()


def test_viewport_snapshot_is_sid_scoped():
    flask_client = make_flask_client()
    client_a = make_socket_client(flask_client)
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    client_b = make_socket_client(flask_client)

    client_a.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    assert bridge.attached_sids

    client_b.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    result = last_payload(client_b, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['error_code'] == webssh.AGENT_ERROR_NOT_ATTACHED
    assert webssh.agent_viewport_snapshot_store._entries == {}

    client_a.disconnect()
    client_b.disconnect()


def test_viewport_snapshot_accepts_attached_sid():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'screen\n',
    })

    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    snapshot = valid_viewport_snapshot(seq=1)
    snapshot['output_seq'] = bridge.output_seq
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, snapshot)
    result = last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['status'] == 'accepted'
    assert result['snapshot_seq'] == 1
    assert result['line_count'] == 2
    assert result['byte_length'] == len('lineline'.encode('utf-8'))

    stored = webssh.agent_viewport_snapshot_store.get_latest(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert stored is not None
    assert stored['lines'] == ['line', 'line']
    assert stored['untrusted'] is True
    assert stored['output_seq'] == 1

    context = webssh.build_agent_context(session_token, webssh.TERMINAL_ID_MAIN, sid)
    assert context['session_id'].startswith('ags_')
    assert context['viewer_id'].startswith('agv_')
    assert context['terminal_mirror']['source'] == 'browser_viewport_snapshot'
    assert 'session_token' not in context['terminal_session']
    assert context['terminal_session']['output_seq'] == 1
    assert context['active_screen']['source'] == 'browser_viewport_snapshot'
    assert context['active_screen']['provisional'] is True
    assert context['active_screen']['output_seq'] == 1

    client.disconnect()


def test_viewport_snapshot_rejects_oversized_payload():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)

    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    oversized = valid_viewport_snapshot()
    oversized['lines'][0] = 'x' * (webssh.AGENT_VIEWPORT_SNAPSHOT_MAX_LINE_BYTES + 1)
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, oversized)

    result = last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['error_code'] == webssh.AGENT_ERROR_SNAPSHOT_TOO_LARGE
    assert webssh.agent_viewport_snapshot_store._entries == {}

    client.disconnect()


def test_viewport_snapshot_stale_sequence_is_rejected():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=2, fill='new'))
    assert last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=1, fill='old'))

    result = last_payload(client, webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['status'] == 'stale'
    assert result['error_code'] == webssh.AGENT_ERROR_SNAPSHOT_STALE
    stored = webssh.agent_viewport_snapshot_store.get_latest(
        session_token,
        webssh.TERMINAL_ID_MAIN,
        sid,
    )
    assert stored['lines'] == ['new', 'new']

    client.disconnect()


def test_viewport_snapshot_context_clears_on_terminal_close_and_disconnect():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    assert webssh.agent_viewport_snapshot_store.get_latest(session_token, webssh.TERMINAL_ID_MAIN, sid)

    client.emit('close_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    assert webssh.agent_viewport_snapshot_store.get_latest(session_token, webssh.TERMINAL_ID_MAIN, sid) is None

    add_dummy_bridge(session_token)
    client.emit('replay_terminal', {'terminal_id': webssh.TERMINAL_ID_MAIN})
    client.emit(webssh.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=2))
    assert webssh.agent_viewport_snapshot_store.get_latest(session_token, webssh.TERMINAL_ID_MAIN, sid)

    client.disconnect()
    assert webssh.agent_viewport_snapshot_store.get_latest(session_token, webssh.TERMINAL_ID_MAIN, sid) is None


def main():
    tests = [
        test_pause_blocks_pending_approval,
        test_approval_and_direct_writes_use_gate,
        test_provider_run_uses_agent_gate,
        test_provider_adapter_receives_context_and_exposes_run_metadata,
        test_static_env_provider_is_explicit_adapter,
        test_provider_failure_is_typed_and_does_not_write,
        test_invalid_provider_proposal_is_rejected_before_action_creation,
        test_external_agent_token_requires_enabled_agent_panel,
        test_external_agent_can_attach_and_read_authorized_screen,
        test_external_agent_screen_tail_lines_and_region_reduce_viewport_payload,
        test_external_agent_render_requests_browser_viewport_png,
        test_external_agent_render_timeout_is_typed,
        test_external_agent_tail_reports_gap_metadata,
        test_external_agent_tail_limit_preserves_cursor_order,
        test_external_agent_tail_wait_returns_after_new_output,
        test_external_agent_tail_wait_times_out_without_output,
        test_external_agent_tail_wait_stops_on_pause,
        test_external_agent_observe_cannot_send,
        test_external_agent_approval_send_waits_for_human_approval,
        test_external_agent_direct_send_uses_agent_gate,
        test_external_agent_direct_send_capture_returns_tail_after_write,
        test_external_agent_send_wait_times_out_without_output,
        test_external_agent_approval_send_capture_is_pending_without_capture,
        test_external_agent_send_capture_reports_pause_as_nested_capture_error,
        test_human_input_lease_blocks_external_agent_send,
        test_human_input_lease_is_terminal_scoped_across_viewers,
        test_human_input_lease_blocks_external_approval_proposal,
        test_human_input_lock_blocks_external_approval_until_lease_recorded,
        test_external_agent_privacy_and_disabled_state_block_visibility_and_send,
        test_external_agent_token_revoke_and_terminal_close_invalidate_access,
        test_external_agent_expired_and_wrong_terminal_tokens_are_rejected,
        test_external_agent_http_bridge_mints_token_and_accepts_cli_command,
        test_external_agent_startup_lines_point_to_launch_handoff,
        test_wsl_local_shell_choice_is_structured_and_wsl_only,
        test_agent_audit_records_typed_events_without_raw_action_data,
        test_wrong_sid_cannot_approve_action,
        test_stale_mode_version_cannot_approve_action,
        test_stale_privacy_version_cannot_approve_action,
        test_mode_change_cancels_pending_action,
        test_terminal_close_invalidates_pending_action,
        test_disconnect_invalidates_agent_state,
        test_stale_epoch_write_is_rejected,
        test_transcript_store_sanitizes_terminal_output,
        test_terminal_bridge_tracks_shared_session_metadata,
        test_ssh_input_records_agent_metadata_after_validation,
        test_agent_input_metadata_bounds_and_sanitized_preview,
        test_privacy_state_blocks_agent_context_and_redacts_input_metadata,
        test_ssh_input_does_not_record_invalid_or_oversized_metadata,
        test_viewport_snapshot_is_sid_scoped,
        test_viewport_snapshot_accepts_attached_sid,
        test_viewport_snapshot_rejects_oversized_payload,
        test_viewport_snapshot_stale_sequence_is_rejected,
        test_viewport_snapshot_context_clears_on_terminal_close_and_disconnect,
    ]
    for test in tests:
        reset_state()
        test()
        print(f'{test.__name__}: ok')


if __name__ == '__main__':
    main()
