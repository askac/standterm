import sys
import tempfile
import threading
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as standterm


class DummyBridge(standterm.TerminalBridge):
    connection_type = standterm.CONNECTION_TYPE_LOCAL_SHELL
    terminal_kind = 'local'
    terminal_label = 'Dummy'

    def __init__(self, owner_session, terminal_id):
        super().__init__(owner_session, terminal_id)
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closing = True


class RecordingProvider(standterm.AgentProvider):
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
            'action_type': standterm.AGENT_ACTION_TERMINAL_INPUT,
            'terminal_id': context['terminal_id'],
            'data': self.terminal_input,
        }


class FailingProvider(standterm.AgentProvider):
    name = 'failing'
    version = 'test-1'

    def create_terminal_input_proposal(self, context, run):
        raise standterm.AgentProviderError(standterm.AGENT_ERROR_PROVIDER_FAILED, 'provider failed')


class InvalidProvider(standterm.AgentProvider):
    name = 'invalid'
    version = 'test-1'

    def create_terminal_input_proposal(self, context, run):
        return {
            'action_type': 'unsupported',
            'terminal_id': context['terminal_id'],
            'data': 'invalid\n',
        }


def reset_state():
    standterm.bridges.clear()
    standterm.pending_localhost_key_setups.clear()
    standterm.active_sessions.clear()
    standterm.socket_session_tokens.clear()
    standterm.socket_client_ips.clear()
    standterm.socket_browser_identities.clear()
    standterm.socket_browser_authorized.clear()
    standterm.socket_browser_auth_challenges.clear()
    standterm.socket_settings_admin_grant_ids.clear()
    standterm.settings_admin_grants.clear()
    standterm.settings_audit_store.clear()
    standterm.reset_runtime_settings_for_test()
    standterm.agent_states.clear()
    standterm.agent_session_ids.clear()
    standterm.agent_viewer_ids.clear()
    standterm.agent_audit_store.clear()
    standterm.agent_transcript_store.clear()
    standterm.agent_user_input_metadata_store.clear()
    standterm.agent_headless_terminal_mirror_store.clear()
    standterm.agent_viewport_snapshot_store.clear()
    standterm.agent_viewport_render_request_store.clear()
    standterm.external_agent_attach_store.clear()
    standterm.operator_observations.clear()
    standterm.set_agent_provider_for_test(standterm.MockAgentProvider())


def make_client():
    flask_client = standterm.app.test_client()
    response = flask_client.get('/?token=' + standterm.ACCESS_TOKEN)
    assert response.status_code == 200, response.status_code
    socket_client = standterm.socketio.test_client(standterm.app, flask_test_client=flask_client)
    assert socket_client.is_connected()
    return socket_client


def make_flask_client():
    flask_client = standterm.app.test_client()
    response = flask_client.get('/?token=' + standterm.ACCESS_TOKEN)
    assert response.status_code == 200, response.status_code
    return flask_client


def make_socket_client(flask_client):
    socket_client = standterm.socketio.test_client(standterm.app, flask_test_client=flask_client)
    assert socket_client.is_connected()
    return socket_client


def current_session_token():
    assert standterm.socket_session_tokens
    return next(reversed(standterm.socket_session_tokens.values()))


def current_sid_for_session(session_token):
    matches = [
        sid for sid, token in standterm.socket_session_tokens.items()
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
    bridge = DummyBridge(session_token, standterm.TERMINAL_ID_MAIN)
    standterm.set_bridge(session_token, standterm.TERMINAL_ID_MAIN, bridge)
    return bridge


def valid_viewport_snapshot(seq=1, rows=2, cols=4, fill='line', lines=None):
    if lines is not None:
        rows = len(lines)
    return {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    assert last_payload(client, standterm.AGENT_EVENT_STATE)['mode'] == standterm.AGENT_MODE_OBSERVE

    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'blocked\n',
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)

    client.emit(standterm.AGENT_EVENT_PAUSE, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    assert last_payload(client, standterm.AGENT_EVENT_STATE)['mode'] == standterm.AGENT_MODE_PAUSED

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    result = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] in {standterm.AGENT_ERROR_PAUSED, standterm.AGENT_ERROR_ACTION_NOT_PENDING}

    client.disconnect()


def test_operator_observation_logs_metadata_without_input_preview():
    previous_dir = standterm.OPERATOR_OBSERVATION_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        standterm.OPERATOR_OBSERVATION_DIR = Path(temp_dir)
        client = make_client()
        session_token = current_session_token()
        bridge = add_dummy_bridge(session_token)

        client.emit(standterm.OPERATOR_OBSERVATION_EVENT_START, {
            'terminal_id': standterm.TERMINAL_ID_MAIN,
        })
        state = last_payload(client, standterm.OPERATOR_OBSERVATION_EVENT_STATE)
        assert state['active'] is True
        assert state['enabled'] is True
        observation_id = state['observation_id']

        client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
        client.emit(standterm.AGENT_EVENT_MODE_SET, {
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'mode': 'observe',
        })
        client.emit('ssh_input', {
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'data': 'secret command\n',
        })
        client.emit(standterm.OPERATOR_OBSERVATION_EVENT_MARK, {
            'terminal_id': standterm.TERMINAL_ID_MAIN,
        })
        client.emit(standterm.OPERATOR_OBSERVATION_EVENT_STOP, {
            'terminal_id': standterm.TERMINAL_ID_MAIN,
        })
        state = last_payload(client, standterm.OPERATOR_OBSERVATION_EVENT_STATE)
        assert state['active'] is False

        paths = list(Path(temp_dir).glob(f'*/{observation_id}.jsonl'))
        assert len(paths) == 1
        lines = [json.loads(line) for line in paths[0].read_text(encoding='utf-8').splitlines()]
        kinds = [line.get('kind') for line in lines if line.get('event_type') == 'operator_observation_event']
        assert 'agent_mode_set' in kinds
        assert 'terminal_input' in kinds
        assert 'operator_mark' in kinds
        joined = json.dumps(lines, ensure_ascii=False)
        assert 'secret command' not in joined
        input_event = next(line for line in lines if line.get('kind') == 'terminal_input')
        assert input_event['metadata']['byte_length'] == len('secret command\n')
        assert input_event['metadata']['raw_preview_recorded'] is False
    standterm.OPERATOR_OBSERVATION_DIR = previous_dir


def test_operator_observation_state_syncs_across_viewers():
    previous_dir = standterm.OPERATOR_OBSERVATION_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        standterm.OPERATOR_OBSERVATION_DIR = Path(temp_dir)
        flask_client = standterm.app.test_client()
        response = flask_client.get('/?token=' + standterm.ACCESS_TOKEN)
        assert response.status_code == 200
        client_a = make_socket_client(flask_client)
        client_b = make_socket_client(flask_client)
        session_token = current_session_token()
        add_dummy_bridge(session_token)

        client_a.emit(standterm.OPERATOR_OBSERVATION_EVENT_START, {
            'terminal_id': standterm.TERMINAL_ID_MAIN,
        })
        state_a = last_payload(client_a, standterm.OPERATOR_OBSERVATION_EVENT_STATE)
        state_b = last_payload(client_b, standterm.OPERATOR_OBSERVATION_EVENT_STATE)
        assert state_a['active'] is True
        assert state_b['active'] is True
        assert state_a['observation_id'] == state_b['observation_id']

        client_b.emit(standterm.OPERATOR_OBSERVATION_EVENT_STOP, {
            'terminal_id': standterm.TERMINAL_ID_MAIN,
        })
        assert last_payload(client_a, standterm.OPERATOR_OBSERVATION_EVENT_STATE)['active'] is False
        assert last_payload(client_b, standterm.OPERATOR_OBSERVATION_EVENT_STATE)['active'] is False
    standterm.OPERATOR_OBSERVATION_DIR = previous_dir


def test_approval_and_direct_writes_use_gate():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    state = last_payload(client, standterm.AGENT_EVENT_STATE)
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'approved\n',
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)
    assert action['requires_approval'] is True
    assert action['session_id'].startswith('ags_')
    assert action['viewer_id'].startswith('agv_')
    assert action['agent_binding_id'].startswith('agb_')
    assert action['proposal_id'].startswith('agp_')
    assert action['mode_version'] == state['mode_version']
    assert action['privacy_state'] == standterm.AGENT_PRIVACY_NORMAL
    assert action['privacy_version'] == state['privacy_version']
    assert action['escaped_preview'] == 'approved\\n'

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'session_id': action['session_id'],
        'viewer_id': action['viewer_id'],
        'agent_binding_id': action['agent_binding_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    })
    assert ''.join(bridge.writes) == 'approved\n'
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['status'] == standterm.AGENT_STATUS_COMPLETED

    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'direct\n',
    })
    assert ''.join(bridge.writes) == 'approved\ndirect\n'
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['status'] == standterm.AGENT_STATUS_COMPLETED

    client.disconnect()


def test_provider_run_uses_agent_gate():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'

    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)
    assert action['requires_approval'] is True
    assert action['escaped_preview'] == 'pwd\\n'
    assert bridge.writes == []

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert ''.join(bridge.writes) == 'pwd\n'

    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert ''.join(bridge.writes) == 'pwd\npwd\n'
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['status'] == standterm.AGENT_STATUS_COMPLETED

    client.disconnect()


def test_provider_adapter_receives_context_and_exposes_run_metadata():
    provider = RecordingProvider('adapter\n')
    standterm.set_agent_provider_for_test(provider)
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(fill='screen'))
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'

    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)

    assert provider.contexts
    assert provider.contexts[-1]['terminal_id'] == standterm.TERMINAL_ID_MAIN
    assert provider.contexts[-1]['active_screen']['lines'] == ['screen', 'screen']
    assert provider.contexts[-1]['terminal_session']['session_id'] == action['session_id']
    assert provider.runs[-1]['run_id'].startswith('agr_')
    assert action['run_id'] == provider.runs[-1]['run_id']
    assert action['provider_name'] == 'recording'
    assert action['provider_version'] == 'test-1'
    assert action['provider_status'] == standterm.AGENT_RUN_STATUS_COMPLETED
    assert action['escaped_preview'] == 'adapter\\n'
    assert bridge.writes == []

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'session_id': action['session_id'],
        'viewer_id': action['viewer_id'],
        'agent_binding_id': action['agent_binding_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    })
    assert ''.join(bridge.writes) == 'adapter\n'

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    event_types = [event['event_type'] for event in audit_events]
    assert standterm.AGENT_AUDIT_PROVIDER_RUN_REQUEST in event_types
    assert standterm.AGENT_AUDIT_PROVIDER_RUN_START in event_types
    assert standterm.AGENT_AUDIT_PROVIDER_RUN_COMPLETE in event_types
    provider_events = [
        event for event in audit_events
        if event['event_type'] in {
            standterm.AGENT_AUDIT_PROVIDER_RUN_REQUEST,
            standterm.AGENT_AUDIT_PROVIDER_RUN_START,
            standterm.AGENT_AUDIT_PROVIDER_RUN_COMPLETE,
        }
    ]
    assert provider_events
    for event in provider_events:
        assert event['provider_name'] == 'recording'
        assert event['provider_version'] == 'test-1'
        assert event['run_id'] == action['run_id']

    client.disconnect()


def test_static_env_provider_is_explicit_adapter():
    provider = standterm.StaticEnvAgentProvider('static-adapter\n')
    standterm.set_agent_provider_for_test(provider)
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })

    assert ''.join(bridge.writes) == 'static-adapter\n'
    result = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['provider_name'] == 'static_env'

    client.disconnect()


def test_provider_failure_is_typed_and_does_not_write():
    standterm.set_agent_provider_for_test(FailingProvider())
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })

    assert bridge.writes == []
    result = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] == standterm.AGENT_ERROR_PROVIDER_FAILED
    assert received_events(client, standterm.AGENT_EVENT_ACTION_REQUEST) == []
    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    errors = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_PROVIDER_RUN_ERROR
    ]
    assert errors
    assert errors[-1]['error_code'] == standterm.AGENT_ERROR_PROVIDER_FAILED
    assert errors[-1]['status'] == standterm.AGENT_RUN_STATUS_FAILED

    client.disconnect()


def test_invalid_provider_proposal_is_rejected_before_action_creation():
    standterm.set_agent_provider_for_test(InvalidProvider())
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })

    assert bridge.writes == []
    result = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] == standterm.AGENT_ERROR_PROVIDER_INVALID_PROPOSAL
    assert received_events(client, standterm.AGENT_EVENT_ACTION_REQUEST) == []
    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert not [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_PROPOSAL_CREATED
    ]

    client.disconnect()


def test_external_agent_token_requires_enabled_agent_panel():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    token, record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert token is None
    assert record is None
    assert error_code == standterm.AGENT_ERROR_NOT_ATTACHED

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'disabled',
    })
    token, record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert token is None
    assert record is None
    assert error_code == standterm.AGENT_ERROR_EXTERNAL_AGENT_DISABLED

    client.disconnect()


def test_external_agent_can_attach_and_read_authorized_screen():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(fill='screen'))
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'terminal-output\n',
    })

    token, record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    assert record['idle_timeout_seconds'] == standterm.AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS
    assert record['expires_at'] > standterm.time.time()
    attach = standterm.process_external_agent_command({
        'op': 'attach',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert attach['status'] == 'ok'
    assert attach['external_agent_id'] == record['external_agent_id']
    assert attach['mode'] == standterm.AGENT_MODE_OBSERVE
    assert attach['terminal_session']['output_seq'] == 1
    assert attach['terminal_session']['last_output_at'] is not None
    assert attach['terminal_session']['terminal_quiet_ms'] >= 0
    assert attach['external_agent_token']['token_lifetime'] == 'idle_timeout'
    assert attach['external_agent_token']['idle_timeout_seconds'] == standterm.AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS
    assert attach['external_agent_token']['remaining_idle_ms'] > 0

    screen = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert screen['status'] == 'ok'
    assert screen['screen']['lines'] == ['screen', 'screen']
    assert screen['state']['session_id'] == attach['session_id']

    tail = standterm.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
    })
    assert tail['status'] == 'ok'
    assert tail['since_output_seq'] == 0
    assert tail['first_available_output_seq'] == 1
    assert tail['dropped_before_output_seq'] == 0
    assert tail['gap']['detected'] is False
    assert tail['events'][-1]['data'] == 'terminal-output\n'

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    event_types = [event['event_type'] for event in audit_events]
    assert standterm.AGENT_AUDIT_EXTERNAL_AGENT_TOKEN_CREATED in event_types
    assert standterm.AGENT_AUDIT_EXTERNAL_AGENT_ATTACHED in event_types
    assert standterm.AGENT_AUDIT_EXTERNAL_AGENT_SCREEN in event_types
    assert standterm.AGENT_AUDIT_EXTERNAL_AGENT_TAIL in event_types
    for event in audit_events:
        assert 'token' not in event
        assert 'token_hash' not in event

    client.disconnect()


def test_external_agent_screen_falls_back_to_headless_grid_without_browser_snapshot():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)
    bridge.update_terminal_size(12, 4)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'old prompt\r\n',
    })
    bridge.emit_output({
        'message_type': 'terminal',
        'data': '\x1b[?1049hfirst\r\nsecond',
    })
    bridge.emit_output({
        'message_type': 'terminal',
        'data': '\r\x1b[2Kdone',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    screen = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert screen['status'] == 'ok'
    assert screen['screen']['source'] == standterm.AGENT_HEADLESS_SCREEN_SOURCE
    assert screen['screen']['provisional'] is True
    assert screen['screen']['cols'] == 12
    assert screen['screen']['rows'] == 4
    assert screen['screen']['output_seq'] == 3
    assert screen['screen']['screen_seq'] == 3
    assert screen['screen']['lines'] == ['first', 'done', '', '']
    assert screen['screen']['cursor_x'] == len('done')
    assert screen['screen']['cursor_y'] == 1

    tail_screen = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'tail_lines': 2,
    })
    assert tail_screen['status'] == 'ok'
    assert tail_screen['screen']['lines'] == ['', '']
    assert tail_screen['screen']['original_line_count'] == 4
    assert tail_screen['screen']['region'] == {
        'top': 2,
        'bottom': 4,
        'tail_lines': 2,
    }

    quiet_screen = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'wait_ms': 1000,
        'quiet_ms': 20,
    })
    assert quiet_screen['status'] == 'ok'
    assert quiet_screen['screen_wait']['wait_ms'] == 1000
    assert quiet_screen['screen_wait']['quiet_ms'] == 20
    assert quiet_screen['screen_wait']['settled'] is True
    assert quiet_screen['screen_wait']['timed_out'] is False
    assert quiet_screen['screen_wait']['terminal_quiet_ms'] >= 20

    client.disconnect()


def test_external_agent_screen_tail_lines_and_region_reduce_viewport_payload():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    lines = ['line-0', 'line-1', 'line-2', 'line-3', 'line-4']
    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(
        standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT,
        valid_viewport_snapshot(seq=1, cols=10, lines=lines),
    )
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    tail_screen = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
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

    region_screen = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
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

    invalid = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'tail_lines': 1,
        'region': {
            'top': 0,
            'bottom': 1,
        },
    })
    assert invalid['status'] == standterm.AGENT_STATUS_FAILED
    assert invalid['error_code'] == standterm.AGENT_ERROR_ACTION_INVALID_DATA

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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    bridge.update_terminal_size(100, 30)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'render-source\n',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    hello = standterm.process_external_agent_command({
        'op': 'hello',
        'token': token,
    })
    assert 'render' in hello['capabilities']

    result_box = {}

    def request_render():
        result_box['render'] = standterm.process_external_agent_command({
            'op': 'render',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'render_mode': 'visible_xterm_png',
            'wait_ms': 1000,
        })

    thread = threading.Thread(target=request_render)
    thread.start()
    request_event = wait_for_event(client, standterm.AGENT_EVENT_VIEWPORT_RENDER_REQUEST)
    request_payload = request_event['args'][0]
    assert request_payload['terminal_id'] == standterm.TERMINAL_ID_MAIN
    assert request_payload['render_type'] == 'xterm_viewport'
    assert request_payload['render_mode'] == standterm.AGENT_RENDER_MODE_VISIBLE_XTERM_PNG
    assert request_payload['mime_type'] == 'image/png'
    assert request_payload['cols'] == 100
    assert request_payload['rows'] == 30
    assert request_payload['output_seq'] == bridge.output_seq

    client.emit(standterm.AGENT_EVENT_VIEWPORT_RENDER_RESULT, {
        'request_id': request_payload['request_id'],
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'render_type': 'xterm_viewport',
        'render_mode': standterm.AGENT_RENDER_MODE_VISIBLE_XTERM_PNG,
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
    assert result['render']['render_mode'] == standterm.AGENT_RENDER_MODE_VISIBLE_XTERM_PNG
    assert result['render']['mime_type'] == 'image/png'
    assert result['render']['image_base64'] == one_pixel_png
    assert result['render']['image_byte_length'] > 0
    assert result['render']['output_seq'] == bridge.output_seq

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    render_audit = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_EXTERNAL_AGENT_RENDER
    ][-1]
    assert render_audit['request_id'] == request_payload['request_id']
    assert render_audit['image_byte_length'] == result['render']['image_byte_length']
    assert 'image_base64' not in render_audit

    client.disconnect()

def test_external_agent_render_mirror_screen_returns_structured_screen_without_png_request():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'mirror-render\n',
    })

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'render',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'render_mode': 'mirror_screen',
    })
    assert result['status'] == 'ok'
    assert result['output_seq'] == bridge.output_seq
    assert result['render']['render_mode'] == standterm.AGENT_RENDER_MODE_MIRROR_SCREEN
    assert result['render']['render_type'] == 'terminal_screen'
    assert result['render']['data_format'] == 'terminal_screen'
    assert result['render']['mime_type'] == 'application/vnd.standterm.screen+json'
    assert result['render']['source'] == standterm.AGENT_HEADLESS_SCREEN_SOURCE
    assert result['render']['output_seq'] == bridge.output_seq
    assert 'image_base64' not in result['render']

    render_requests = received_events(client, standterm.AGENT_EVENT_VIEWPORT_RENDER_REQUEST)
    assert render_requests == []

    invalid = standterm.process_external_agent_command({
        'op': 'render',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'render_mode': 'unknown',
    })
    assert invalid['status'] == standterm.AGENT_STATUS_FAILED
    assert invalid['error_code'] == standterm.AGENT_ERROR_ACTION_INVALID_DATA

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    render_audit = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_EXTERNAL_AGENT_RENDER
    ][-1]
    assert render_audit['render_mode'] == standterm.AGENT_RENDER_MODE_MIRROR_SCREEN
    assert render_audit['render_type'] == 'terminal_screen'
    assert render_audit['line_count'] == result['render']['line_count']
    assert 'lines' not in render_audit

    client.disconnect()


def test_external_agent_render_auto_uses_mirror_screen_without_png_request():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'auto-mirror-render\n',
    })

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'render',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert result['status'] == 'ok'
    assert result['render']['render_mode'] == standterm.AGENT_RENDER_MODE_MIRROR_SCREEN
    assert result['render']['render_type'] == 'terminal_screen'
    assert result['render']['data_format'] == 'terminal_screen'
    assert result['render']['mime_type'] == 'application/vnd.standterm.screen+json'
    assert result['render']['output_seq'] == bridge.output_seq
    assert 'image_base64' not in result['render']

    render_requests = received_events(client, standterm.AGENT_EVENT_VIEWPORT_RENDER_REQUEST)
    assert render_requests == []

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    render_audit = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_EXTERNAL_AGENT_RENDER
    ][-1]
    assert render_audit['requested_render_mode'] == standterm.AGENT_RENDER_MODE_AUTO
    assert render_audit['render_mode'] == standterm.AGENT_RENDER_MODE_MIRROR_SCREEN

    client.disconnect()


def test_external_agent_render_timeout_is_typed():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'render',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'render_mode': 'visible_xterm_png',
        'wait_ms': 10,
    })
    assert result['status'] == standterm.AGENT_STATUS_FAILED
    assert result['error_code'] == standterm.AGENT_ERROR_RENDER_TIMEOUT

    client.disconnect()


def test_external_agent_tail_reports_gap_metadata():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    bridge.output_seq = 5
    bridge.replay_buffer.clear()
    bridge.replay_buffer.append({
        'message_type': 'terminal',
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'fifth\n',
        'output_seq': 5,
    })
    bridge.replay_buffer_bytes = len('fifth\n')
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    tail = standterm.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
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
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'fifth\n',
        'output_seq': 5,
    }]

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    tail_audit = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_EXTERNAL_AGENT_TAIL
    ][-1]
    assert tail_audit['gap']['detected'] is True

    client.disconnect()


def test_external_agent_tail_strip_ansi_is_explicit_plain_format():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    raw_data = '\x1b[31mred\x1b[0m\r\nnext\x1b]0;title\x07\n'
    bridge.emit_output({
        'message_type': 'terminal',
        'data': raw_data,
    })

    raw_tail = standterm.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
    })
    assert raw_tail['status'] == 'ok'
    assert raw_tail['events'][-1]['data'] == raw_data
    assert 'strip_ansi' not in raw_tail

    plain_tail = standterm.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
        'strip_ansi': True,
    })
    assert plain_tail['status'] == 'ok'
    assert plain_tail['strip_ansi'] is True
    assert plain_tail['data_format'] == 'plain'
    assert plain_tail['events'][-1]['data'] == 'red\nnext\n'

    client.disconnect()


def test_external_agent_tail_limit_preserves_cursor_order():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    for index in range(1, 5):
        bridge.emit_output({
            'message_type': 'terminal',
            'data': f'line-{index}\n',
        })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    first_page = standterm.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
        'limit': 2,
    })
    assert first_page['status'] == 'ok'
    assert first_page['gap']['detected'] is False
    assert [event['output_seq'] for event in first_page['events']] == [1, 2]

    second_page = standterm.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_tail():
        result_box['tail'] = standterm.process_external_agent_command({
            'op': 'tail',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    started = time.monotonic()
    tail = standterm.process_external_agent_command({
        'op': 'tail',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_tail():
        result_box['tail'] = standterm.process_external_agent_command({
            'op': 'tail',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'since_output_seq': 0,
            'limit': 10,
            'wait_ms': 1000,
        })

    thread = threading.Thread(target=request_tail)
    thread.start()
    time.sleep(0.05)
    client.emit(standterm.AGENT_EVENT_PAUSE, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result_box['tail']['status'] == standterm.AGENT_STATUS_FAILED
    assert result_box['tail']['error_code'] == standterm.AGENT_ERROR_PAUSED

    client.disconnect()


def test_external_agent_wait_output_returns_structured_result_without_display_by_default():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_wait():
        result_box['wait'] = standterm.process_external_agent_command({
            'op': 'wait',
            'condition': 'output',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'since_output_seq': 0,
            'wait_ms': 1000,
        })

    thread = threading.Thread(target=request_wait)
    thread.start()
    time.sleep(0.05)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'typed-wait-output\n',
    })
    thread.join(timeout=2)
    assert not thread.is_alive()

    result = result_box['wait']
    assert result['status'] == 'ok'
    assert result['wait']['condition'] == 'output'
    assert result['wait']['status'] == 'changed'
    assert result['wait']['timed_out'] is False
    assert result['wait']['changed'] is True
    assert result['wait']['event_count'] == 1
    assert 'events' not in result['wait']

    with_events = standterm.process_external_agent_command({
        'op': 'wait',
        'condition': 'output',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
        'wait_ms': 0,
        'include_events': True,
    })
    assert with_events['wait']['status'] == 'changed'
    assert with_events['wait']['events'][0]['data'] == 'typed-wait-output\n'

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    wait_audit = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_EXTERNAL_AGENT_WAIT
    ][-1]
    assert wait_audit['condition'] == 'output'
    assert wait_audit['status'] == 'changed'

    client.disconnect()


def test_external_agent_wait_output_timeout_and_wait_quiet_are_typed():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    output_wait = standterm.process_external_agent_command({
        'op': 'wait',
        'condition': 'output',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'since_output_seq': 0,
        'wait_ms': 20,
    })
    assert output_wait['status'] == 'ok'
    assert output_wait['wait']['condition'] == 'output'
    assert output_wait['wait']['status'] == 'timeout'
    assert output_wait['wait']['timed_out'] is True
    assert output_wait['wait']['event_count'] == 0

    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'quiet-source\n',
    })
    quiet_wait = standterm.process_external_agent_command({
        'op': 'wait',
        'condition': 'quiet',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'wait_ms': 1000,
        'quiet_ms': 20,
    })
    assert quiet_wait['status'] == 'ok'
    assert quiet_wait['wait']['condition'] == 'quiet'
    assert quiet_wait['wait']['status'] == 'settled'
    assert quiet_wait['wait']['settled'] is True
    assert quiet_wait['wait']['timed_out'] is False
    assert quiet_wait['wait']['terminal_quiet_ms'] >= 20

    invalid = standterm.process_external_agent_command({
        'op': 'wait',
        'condition': 'quiet',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'wait_ms': 20,
    })
    assert invalid['status'] == standterm.AGENT_STATUS_FAILED
    assert invalid['error_code'] == standterm.AGENT_ERROR_ACTION_INVALID_DATA

    client.disconnect()

def test_external_agent_sequence_runs_steps_and_stops_on_timeout():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_sequence():
        result_box['sequence'] = standterm.process_external_agent_command({
            'op': 'sequence',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'steps': [
                {'op': 'send', 'kind': 'text', 'text': 'seq-input\n'},
                {
                    'op': 'wait',
                    'condition': 'output',
                    'since_output_seq': 0,
                    'wait_ms': 1000,
                },
            ],
        })

    thread = threading.Thread(target=request_sequence)
    thread.start()
    wait_until(lambda: ''.join(bridge.writes) == 'seq-input\n', 'sequence did not write input')
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'seq-output\n',
    })
    thread.join(timeout=2)
    assert not thread.is_alive()

    result = result_box['sequence']
    assert result['status'] == 'ok'
    assert result['sequence']['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['sequence']['completed'] is True
    assert result['sequence']['stop_reason'] is None
    assert result['sequence']['executed_count'] == 2
    assert result['sequence']['results'][0]['op'] == 'send'
    assert result['sequence']['results'][0]['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['sequence']['results'][1]['op'] == 'wait'
    assert result['sequence']['results'][1]['result']['wait']['status'] == 'changed'
    assert 'events' not in result['sequence']['results'][1]['result']['wait']

    timeout_result = standterm.process_external_agent_command({
        'op': 'sequence',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'steps': [
            {
                'op': 'wait',
                'condition': 'output',
                'since_output_seq': bridge.output_seq,
                'wait_ms': 10,
            },
            {'op': 'send', 'kind': 'text', 'text': 'should-not-run\n'},
        ],
    })
    assert timeout_result['status'] == 'ok'
    assert timeout_result['sequence']['status'] == 'stopped'
    assert timeout_result['sequence']['completed'] is False
    assert timeout_result['sequence']['stop_reason'] == 'timeout'
    assert timeout_result['sequence']['stopped_step_index'] == 0
    assert timeout_result['sequence']['executed_count'] == 1
    assert ''.join(bridge.writes) == 'seq-input\n'

    invalid = standterm.process_external_agent_command({
        'op': 'sequence',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'steps': [{'op': 'wait', 'terminal_id': standterm.TERMINAL_ID_MAIN}],
    })
    assert invalid['status'] == standterm.AGENT_STATUS_FAILED
    assert invalid['error_code'] == standterm.AGENT_ERROR_ACTION_INVALID_DATA

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    sequence_audit = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_EXTERNAL_AGENT_SEQUENCE
    ][-1]
    assert sequence_audit['stop_reason'] == 'timeout'
    assert sequence_audit['executed_count'] == 1

    client.disconnect()


def test_external_agent_observe_cannot_send():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'blocked\n',
    })
    assert result['error_code'] == standterm.AGENT_ERROR_MODE_NOT_WRITABLE
    assert bridge.writes == []

    client.disconnect()


def test_external_agent_approval_send_waits_for_human_approval():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'approved-external\n',
    })
    assert result['status'] == standterm.AGENT_STATUS_PENDING_APPROVAL
    assert result['requires_approval'] is True
    assert result['provider_name'] == 'external_agent'
    assert bridge.writes == []
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)
    assert action['proposal_id'] == result['proposal_id']

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'direct-external\n',
    })
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['input_kind'] == 'legacy_data'
    assert result['bytes_written'] == len('direct-external\n')
    assert ''.join(bridge.writes) == 'direct-external\n'
    action_result = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert action_result['proposal_id'] == result['proposal_id']

    client.disconnect()


def test_external_agent_submit_after_writes_discrete_enter():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'codex prompt',
        'submit_after': True,
    })
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['input_kind'] == 'legacy_data'
    assert result['submit_after'] is True
    assert result['bytes_written'] == len('codex prompt\r')
    assert bridge.writes == ['codex prompt', '\r']

    client.disconnect()


def test_external_agent_structured_text_and_keys_send_use_backend_input_kind():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    text_result = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'kind': 'text',
        'text': 'typed-text\n',
    })
    assert text_result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert text_result['input_kind'] == 'text'
    assert text_result['key_names'] == []

    key_result = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'input': {
            'kind': 'keys',
            'keys': ['Down', 'Enter'],
        },
    })
    assert key_result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert key_result['input_kind'] == 'keys'
    assert key_result['key_names'] == ['Down', 'Enter']
    assert key_result['key_count'] == 2
    assert bridge.writes == ['typed-text\n', '\x1b[B\r']

    invalid_key = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'kind': 'keys',
        'keys': ['NoSuchKey'],
    })
    assert invalid_key['status'] == standterm.AGENT_STATUS_FAILED
    assert invalid_key['error_code'] == standterm.AGENT_ERROR_ACTION_INVALID_DATA

    ambiguous = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'kind': 'text',
        'text': 'x',
        'data': 'legacy',
    })
    assert ambiguous['status'] == standterm.AGENT_STATUS_FAILED
    assert ambiguous['error_code'] == standterm.AGENT_ERROR_ACTION_INVALID_DATA

    client.disconnect()


def test_external_agent_direct_send_capture_returns_tail_after_write():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    bridge.emit_output({
        'message_type': 'terminal',
        'data': 'old-output\n',
    })

    result_box = {}

    def request_send_capture():
        result_box['send'] = standterm.process_external_agent_command({
            'op': 'send',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
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
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
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


def test_external_agent_send_wait_strip_ansi_formats_capture_only_when_requested():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_send_wait():
        result_box['send'] = standterm.process_external_agent_command({
            'op': 'send-wait',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'data': 'pwd\n',
            'wait_ms': 1000,
            'settle_ms': 10,
            'strip_ansi': True,
        })

    thread = threading.Thread(target=request_send_wait)
    thread.start()
    wait_until(lambda: ''.join(bridge.writes) == 'pwd\n', 'send-wait did not write input')
    bridge.emit_output({
        'message_type': 'terminal',
        'data': '\x1b[32m/tmp/project\x1b[0m\r\n',
    })
    thread.join(timeout=2)
    assert not thread.is_alive()

    result = result_box['send']
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['capture']['requested'] is True
    assert result['capture']['status'] == 'ok'
    assert result['capture']['strip_ansi'] is True
    assert result['capture']['data_format'] == 'plain'
    assert [event['data'] for event in result['capture']['events']] == ['/tmp/project\n']

    client.disconnect()


def test_external_agent_send_wait_times_out_without_output():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'send-wait',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'no-output\n',
        'wait_ms': 10,
        'settle_ms': 0,
    })
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'needs-human\n',
        'capture': True,
    })
    assert result['status'] == standterm.AGENT_STATUS_PENDING_APPROVAL
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

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    result_box = {}

    def request_send_capture():
        result_box['send'] = standterm.process_external_agent_command({
            'op': 'send',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'data': 'pause-after-write\n',
            'capture': True,
            'wait_ms': 1000,
            'settle_ms': 0,
        })

    thread = threading.Thread(target=request_send_capture)
    thread.start()
    wait_until(lambda: ''.join(bridge.writes) == 'pause-after-write\n', 'send capture did not write before pause')
    client.emit(standterm.AGENT_EVENT_PAUSE, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    thread.join(timeout=2)
    assert not thread.is_alive()

    result = result_box['send']
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['bytes_written'] == len('pause-after-write\n')
    assert result['capture'] == {
        'status': standterm.AGENT_STATUS_FAILED,
        'error_code': standterm.AGENT_ERROR_PAUSED,
        'requested': True,
    }

    client.disconnect()


def test_human_input_lease_blocks_external_agent_send():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    client.emit('ssh_input', {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'human\n',
    })
    state = last_payload(client, standterm.AGENT_EVENT_STATE)
    assert state['human_activity_seq'] == 1
    assert state['human_input_lease_active'] is True
    assert ''.join(bridge.writes) == 'human\n'

    blocked = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert blocked['status'] == standterm.AGENT_STATUS_FAILED
    assert blocked['error_code'] == standterm.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert ''.join(bridge.writes) == 'human\n'

    with standterm.agent_lock:
        state = standterm.get_agent_state(session_token, standterm.TERMINAL_ID_MAIN, sid)
        state.human_input_lease_expires_at = standterm.time.time() - 1

    allowed = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert allowed['status'] == standterm.AGENT_STATUS_COMPLETED
    assert ''.join(bridge.writes) == 'human\nagent\n'

    client.disconnect()


def test_human_input_lease_is_terminal_scoped_across_viewers():
    flask_client = make_flask_client()
    client_a = make_socket_client(flask_client)
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid_a = current_sid_for_session(session_token)
    client_b = make_socket_client(flask_client)

    client_a.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client_a.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid_a,
    )
    assert error_code is None

    client_b.emit('ssh_input', {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'viewer-b\n',
    })
    state = last_payload(client_a, standterm.AGENT_EVENT_STATE)
    assert state['human_activity_seq'] == 1
    assert state['human_input_lease_active'] is True
    assert ''.join(bridge.writes) == 'viewer-b\n'

    blocked = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert blocked['status'] == standterm.AGENT_STATUS_FAILED
    assert blocked['error_code'] == standterm.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert ''.join(bridge.writes) == 'viewer-b\n'

    client_a.disconnect()
    client_b.disconnect()


def test_human_input_lease_blocks_external_approval_proposal():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    client.emit('ssh_input', {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'human\n',
    })
    client.get_received()

    blocked = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'agent\n',
    })
    assert blocked['status'] == standterm.AGENT_STATUS_FAILED
    assert blocked['error_code'] == standterm.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert received_events(client, standterm.AGENT_EVENT_ACTION_REQUEST) == []

    client.disconnect()


def test_human_input_lock_blocks_external_approval_until_lease_recorded():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    client.get_received()

    human_started = standterm.threading.Event()
    human_can_record = standterm.threading.Event()
    send_done = standterm.threading.Event()
    result_holder = {}

    def hold_human_input_lock():
        with bridge.input_lock:
            human_started.set()
            assert human_can_record.wait(timeout=2)
            with standterm.agent_lock:
                standterm.note_agent_human_input_for_terminal(session_token, standterm.TERMINAL_ID_MAIN)
            bridge.write('human\n')

    def external_send():
        result_holder['result'] = standterm.process_external_agent_command({
            'op': 'send',
            'token': token,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'data': 'agent\n',
        })
        send_done.set()

    human_thread = standterm.threading.Thread(target=hold_human_input_lock)
    send_thread = standterm.threading.Thread(target=external_send)
    human_thread.start()
    assert human_started.wait(timeout=2)
    send_thread.start()
    standterm.time.sleep(0.05)
    assert not send_done.is_set()

    human_can_record.set()
    human_thread.join(timeout=2)
    send_thread.join(timeout=2)
    assert send_done.is_set()
    blocked = result_holder['result']
    assert blocked['status'] == standterm.AGENT_STATUS_FAILED
    assert blocked['error_code'] == standterm.AGENT_ERROR_HUMAN_INPUT_ACTIVE
    assert received_events(client, standterm.AGENT_EVENT_ACTION_REQUEST) == []
    assert ''.join(bridge.writes) == 'human\n'

    client.disconnect()


def test_external_agent_privacy_and_disabled_state_block_visibility_and_send():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    screen = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    render = standterm.process_external_agent_command({
        'op': 'render',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'wait_ms': 10,
    })
    send = standterm.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'blocked\n',
    })
    assert screen['error_code'] == standterm.AGENT_ERROR_PRIVACY_BLOCKED
    assert render['error_code'] == standterm.AGENT_ERROR_PRIVACY_BLOCKED
    assert send['error_code'] == standterm.AGENT_ERROR_PRIVACY_BLOCKED
    assert bridge.writes == []

    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_NORMAL,
    })
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'disabled',
    })
    result = standterm.process_external_agent_command({
        'op': 'screen',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == standterm.AGENT_ERROR_EXTERNAL_AGENT_DISABLED

    client.disconnect()


def test_external_agent_token_revoke_and_terminal_close_invalidate_access():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None

    revoked = standterm.process_external_agent_command({
        'op': 'revoke',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert revoked['status'] == 'ok'
    result = standterm.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == standterm.AGENT_ERROR_EXTERNAL_AGENT_REVOKED

    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    client.emit('close_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    result = standterm.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == standterm.AGENT_ERROR_EXTERNAL_AGENT_DISCONNECTED

    client.disconnect()


def test_external_agent_expired_and_wrong_terminal_tokens_are_rejected():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
        idle_timeout_seconds=-1,
    )
    assert error_code is None
    result = standterm.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == standterm.AGENT_ERROR_EXTERNAL_AGENT_EXPIRED

    token, record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
        idle_timeout_seconds=60,
    )
    assert error_code is None
    first_expires_at = record['expires_at']
    stored = standterm.external_agent_attach_store._tokens[record['token_hash']]
    stored['expires_at'] = standterm.time.time() + 1
    result = standterm.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    assert result['status'] == 'ok'
    assert stored['expires_at'] > first_expires_at
    assert result['external_agent_token']['expires_at'] == stored['expires_at']
    assert result['external_agent_token']['remaining_idle_ms'] > 0

    token, _record, error_code = standterm.mint_external_agent_attach_token(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert error_code is None
    result = standterm.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': 'other',
    })
    assert result['error_code'] == standterm.AGENT_ERROR_TERMINAL_MISMATCH

    client.disconnect()


def test_external_agent_http_bridge_mints_token_and_accepts_cli_command():
    flask_client = make_flask_client()
    client = make_socket_client(flask_client)
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'direct',
    })
    state = last_payload(client, standterm.AGENT_EVENT_STATE)

    original_handoff_path = standterm.EXTERNAL_AGENT_HANDOFF_PATH
    with tempfile.TemporaryDirectory(prefix='standterm-agent-smoke-') as handoff_dir:
        standterm.EXTERNAL_AGENT_HANDOFF_PATH = Path(handoff_dir) / 'standterm_external_agent_handoff.json'
        try:
            response = flask_client.post('/agent/external/token', json={
                'terminal_id': standterm.TERMINAL_ID_MAIN,
                'viewer_id': state['viewer_id'],
                'agent_binding_id': state['agent_binding_id'],
                'mode_version': state['mode_version'],
                'privacy_version': state['privacy_version'],
            })
            assert response.status_code == 200
            token_payload = response.get_json()
            assert token_payload['status'] == 'ok'
            assert token_payload['token'].startswith('agt_')
            assert token_payload['handoff_schema'] == 'standterm_external_agent_handoff'
            assert token_payload['schema_version'] == 1
            assert token_payload['protocol_version'] == standterm.EXTERNAL_AGENT_PROTOCOL_VERSION
            assert 'headless_screen' in token_payload['capabilities']
            assert 'screen_wait' in token_payload['capabilities']
            assert 'wait' in token_payload['capabilities']
            assert 'sequence' in token_payload['capabilities']
            assert 'render' in token_payload['capabilities']
            assert 'render_visible_xterm_png' in token_payload['capabilities']
            assert 'render_mirror_screen' in token_payload['capabilities']
            assert 'typed_send' in token_payload['capabilities']
            assert 'send_capture' in token_payload['capabilities']
            assert 'submit_after' in token_payload['capabilities']
            assert 'strip_ansi' in token_payload['capabilities']
            assert token_payload['transport']['type'] == 'loopback_http_json'
            assert token_payload['transport']['loopback_only'] is True
            assert token_payload['transport']['command_endpoint'].endswith('/agent/external/command')
            assert token_payload['url'] == token_payload['transport']['command_endpoint'].rsplit('/agent/external/command', 1)[0]
            assert token_payload['browser_url'].startswith('http://')
            assert token_payload['render_policy']['default_mode'] == standterm.AGENT_RENDER_DEFAULT_MODE
            assert token_payload['render_policy']['effective_auto_mode'] == standterm.AGENT_RENDER_EFFECTIVE_AUTO_MODE
            assert standterm.AGENT_RENDER_MODE_VISIBLE_XTERM_PNG in token_payload['render_policy']['supported_modes']
            assert standterm.AGENT_RENDER_MODE_MIRROR_SCREEN in token_payload['render_policy']['supported_modes']
            assert token_payload['render_policy']['visible_xterm_png']['save_supported'] is True
            assert token_payload['render_policy']['mirror_screen']['save_supported'] is False
            assert token_payload['operations']['render'] == {
                'op': 'render',
                'render_mode': standterm.AGENT_RENDER_DEFAULT_MODE,
                'wait_ms': standterm.AGENT_VIEWPORT_RENDER_WAIT_MS,
            }
            assert token_payload['operations']['render_visible_xterm_png']['render_mode'] == 'visible_xterm_png'
            assert token_payload['operations']['render_mirror_screen'] == {
                'op': 'render',
                'render_mode': 'mirror_screen',
            }
            assert token_payload['operations']['screen_tail'] == {'op': 'screen', 'tail_lines': 12}
            assert token_payload['operations']['screen_region'] == {
                'op': 'screen',
                'region': {
                    'top': 0,
                    'bottom': 12,
                },
            }
            assert token_payload['operations']['screen_wait'] == {
                'op': 'screen',
                'wait_ms': 3000,
                'quiet_ms': 500,
            }
            assert token_payload['operations']['wait_output'] == {
                'op': 'wait',
                'condition': 'output',
                'since_output_seq': 0,
                'wait_ms': standterm.AGENT_EXTERNAL_TAIL_MAX_WAIT_MS,
            }
            assert token_payload['operations']['wait_quiet'] == {
                'op': 'wait',
                'condition': 'quiet',
                'wait_ms': 3000,
                'quiet_ms': 500,
            }
            assert token_payload['operations']['sequence']['op'] == 'sequence'
            assert token_payload['operations']['sequence']['steps'][0] == {
                'op': 'send',
                'kind': 'text',
                'text': 'pwd\n',
            }
            assert token_payload['operations']['sequence']['steps'][1]['op'] == 'wait'
            assert token_payload['operations']['tail']['wait_ms'] == standterm.AGENT_EXTERNAL_TAIL_MAX_WAIT_MS
            assert token_payload['operations']['tail_plain']['strip_ansi'] is True
            assert token_payload['operations']['send'] == {'op': 'send', 'kind': 'text', 'text': 'pwd\n'}
            assert token_payload['operations']['send_keys'] == {
                'op': 'send',
                'kind': 'keys',
                'keys': ['Down', 'Enter'],
            }
            assert token_payload['operations']['send_submit']['submit_after'] is True
            assert token_payload['operations']['send_wait']['op'] == 'send-wait'
            assert token_payload['operations']['send_wait']['kind'] == 'text'
            assert token_payload['operations']['send_wait_plain']['strip_ansi'] is True
            assert token_payload['expires_at'] > standterm.time.time()
            assert token_payload['security']['token_lifetime'] == 'idle_timeout'
            assert token_payload['security']['idle_timeout_seconds'] == (
                standterm.AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS
            )
            assert token_payload['security']['remote_use_requires_loopback_tunnel'] is True
            assert token_payload['cli_command'].endswith("send --text 'pwd\n'")
            assert 'scripts/agent_cli.py' in token_payload['cli_command']
            assert f"--url {token_payload['url']}" in token_payload['cli_command']
            assert token_payload['cli_commands']['hello'].endswith('hello')
            assert token_payload['cli_commands']['render'].endswith('render')
            assert token_payload['cli_commands']['render_visible_xterm_png'].endswith('render --mode visible-xterm-png')
            assert token_payload['cli_commands']['render_mirror_screen'].endswith('render --mode mirror-screen')
            assert token_payload['cli_commands']['screen_tail'].endswith("screen --tail-lines 12")
            assert token_payload['cli_commands']['screen_region'].endswith("screen --region 0:12")
            assert token_payload['cli_commands']['screen_wait'].endswith("screen --wait-ms 3000 --quiet-ms 500")
            assert token_payload['cli_commands']['tail_wait'].endswith(
                f"tail --wait-ms {standterm.AGENT_EXTERNAL_TAIL_MAX_WAIT_MS}"
            )
            assert token_payload['cli_commands']['tail_plain'].endswith('tail --strip-ansi')
            assert token_payload['cli_commands']['send_submit'].endswith("send --text 'codex prompt' --submit")
            assert token_payload['cli_commands']['send_wait_plain_pwd'].endswith("send-wait --text 'pwd\n' --strip-ansi")
            assert 'scripts/agent_repl.py' in token_payload['cli_commands']['repl']
            assert 'scripts/agent_jsonl.py' in token_payload['cli_commands']['jsonl']
            handoff = Path(token_payload['handoff_path'])
            assert handoff == standterm.EXTERNAL_AGENT_HANDOFF_PATH
            assert handoff.parent == Path(handoff_dir)
            assert handoff.is_file()
            handoff_payload = standterm.json.loads(handoff.read_text(encoding='utf-8'))
            assert handoff_payload['token'] == token_payload['token']
            assert handoff_payload['url'] == token_payload['url']
            assert handoff_payload['browser_url'] == token_payload['browser_url']
            assert handoff_payload['cli_command'] == token_payload['cli_command']
            assert handoff_payload['capabilities'] == token_payload['capabilities']
            assert handoff_payload['cli_commands']['render'] == token_payload['cli_commands']['render']
            assert 'legacy_handoff_path' not in token_payload
        finally:
            standterm.EXTERNAL_AGENT_HANDOFF_PATH = original_handoff_path

    response = flask_client.post('/agent/external/command', json={
        'op': 'send',
        'token': token_payload['token'],
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'http-external\n',
    })
    assert response.status_code == 200
    result = response.get_json()
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert ''.join(bridge.writes) == 'http-external\n'

    standterm.AGENT_EXTERNAL_DEV_TOKEN_ENABLED = True
    try:
        response = flask_client.post('/agent/external/dev-command', json={
            'op': 'send',
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'data': 'http-dev-external\n',
        })
    finally:
        standterm.AGENT_EXTERNAL_DEV_TOKEN_ENABLED = False
    assert response.status_code == 200
    result = response.get_json()
    assert result['status'] == standterm.AGENT_STATUS_COMPLETED
    assert result['dev_token'] is True
    assert ''.join(bridge.writes) == 'http-external\nhttp-dev-external\n'

    client.disconnect()


def test_external_agent_handoff_uses_loopback_command_url_for_non_loopback_browser_url():
    payload = standterm.build_external_agent_discovery_payload(
        'https://172.17.186.221:5000',
        'agt_unit',
        standterm.TERMINAL_ID_MAIN,
    )
    assert payload['transport']['command_endpoint'] == 'https://127.0.0.1:5000/agent/external/command'
    assert "--url https://127.0.0.1:5000" in payload['cli_commands']['hello']
    assert "--url https://127.0.0.1:5000" in payload['cli_commands']['jsonl']


def assert_agentinfo_is_tokenless(payload):
    assert payload['schema'] == 'standterm_agentinfo'
    assert payload['schema_version'] == 1
    assert payload['protocol_version'] == standterm.EXTERNAL_AGENT_PROTOCOL_VERSION
    assert payload['loopback_only'] is True
    assert payload['security']['tokenless'] is True
    assert payload['security']['contains_secret'] is False
    assert payload['security']['terminal_display_included'] is False
    assert payload['security']['token_bearing_commands_included'] is False
    assert payload['handoff_contains_secret'] is True
    assert payload['handoff_path'].endswith('standterm_external_agent_handoff.json')
    assert payload['transport']['command_endpoint'].endswith('/agent/external/command')
    assert payload['agentinfo_url'].endswith('/agentinfo')
    assert 'agt_' not in standterm.json.dumps(payload)
    for command in payload['recommended_commands'].values():
        assert '--token' not in command


def test_external_agentinfo_payload_route_and_pointer_are_tokenless():
    flask_client = standterm.app.test_client()

    response = flask_client.get('/agentinfo')
    assert response.status_code == 200
    payload = response.get_json()
    assert_agentinfo_is_tokenless(payload)
    assert payload['base_url'].startswith('http://localhost')
    assert payload['command_endpoint'] == payload['base_url'].rstrip('/') + '/agent/external/command'
    assert '--agentinfo' in payload['recommended_commands']['discover']
    assert '--handoff' in payload['recommended_commands']['hello_after_token_mint']
    assert '--handoff' in payload['recommended_commands']['render_after_token_mint']

    blocked = flask_client.get('/agentinfo', environ_overrides={'REMOTE_ADDR': '203.0.113.10'})
    assert blocked.status_code == 403
    assert blocked.get_json()['error_code'] == standterm.AGENT_ERROR_EXTERNAL_AGENT_ORIGIN_BLOCKED

    original_info_path = standterm.EXTERNAL_AGENT_INFO_PATH
    original_current_path = standterm.EXTERNAL_AGENT_CURRENT_INFO_PATH
    with tempfile.TemporaryDirectory(prefix='standterm-agentinfo-smoke-') as temp_dir:
        info_path = Path(temp_dir) / 'standterm_agentinfo.json'
        current_path = Path(temp_dir) / 'current_agentinfo.json'
        standterm.EXTERNAL_AGENT_INFO_PATH = info_path
        standterm.EXTERNAL_AGENT_CURRENT_INFO_PATH = current_path
        try:
            written = standterm.write_external_agentinfo_files(base_url='https://172.17.186.221:5000')
            assert written == [str(info_path), str(current_path)]
            launch_payload = standterm.json.loads(info_path.read_text(encoding='utf-8'))
            current_payload = standterm.json.loads(current_path.read_text(encoding='utf-8'))
            assert_agentinfo_is_tokenless(launch_payload)
            assert_agentinfo_is_tokenless(current_payload)
            assert launch_payload['base_url'] == 'https://127.0.0.1:5000'
            assert launch_payload['agentinfo_path'] == str(info_path)
            assert current_payload['agentinfo_path'] == str(info_path)
            assert current_payload['current_agentinfo_path'] == str(current_path)
        finally:
            standterm.EXTERNAL_AGENT_INFO_PATH = original_info_path
            standterm.EXTERNAL_AGENT_CURRENT_INFO_PATH = original_current_path


def test_external_agent_startup_lines_point_to_launch_handoff():
    original_https_enabled = standterm.HTTPS_ENABLED
    lines = standterm.build_external_agent_startup_lines()
    joined = '\n'.join(lines)
    discover_line = next(line for line in lines if line.startswith('External Agent CLI discover: '))
    hello_line = next(line for line in lines if line.startswith('External Agent CLI hello: '))
    render_line = next(line for line in lines if line.startswith('External Agent CLI render: '))

    assert standterm.quote_local_command(['C:\\Program Files\\Python\\python.exe'], platform_name='win32') == (
        '"C:\\Program Files\\Python\\python.exe"'
    )
    assert str(standterm.EXTERNAL_AGENT_INFO_PATH) in joined
    assert str(standterm.EXTERNAL_AGENT_HANDOFF_PATH) in joined
    assert str(standterm.APP_DIR / 'scripts' / 'agent_cli.py') in joined
    assert standterm.sys.executable in joined
    assert '--agentinfo' in discover_line
    assert str(standterm.EXTERNAL_AGENT_INFO_PATH) in discover_line
    assert discover_line.endswith(' discover')
    assert '--handoff' in hello_line
    assert f'--url http://127.0.0.1:{standterm.DEFAULT_PORT}' in hello_line
    assert str(standterm.EXTERNAL_AGENT_HANDOFF_PATH) in hello_line
    assert hello_line.endswith(' hello')
    assert '--handoff' in render_line
    assert f'--url http://127.0.0.1:{standterm.DEFAULT_PORT}' in render_line
    assert str(standterm.EXTERNAL_AGENT_HANDOFF_PATH) in render_line
    assert render_line.endswith(' render')
    assert 'after browser Agent attach and external token mint' in joined
    assert 'explicit --url, --token, and --terminal' in joined
    try:
        standterm.HTTPS_ENABLED = True
        tls_lines = standterm.build_external_agent_startup_lines()
    finally:
        standterm.HTTPS_ENABLED = original_https_enabled
    tls_joined = '\n'.join(tls_lines)
    assert f'--url https://127.0.0.1:{standterm.DEFAULT_PORT}' in tls_joined
    if standterm.LOCAL_CA_CERT_PATH.is_file() and not (standterm.CLI_ARGS.certfile or standterm.CLI_ARGS.keyfile):
        assert '--ca-file' in tls_joined
        assert str(standterm.LOCAL_CA_CERT_PATH) in tls_joined


def test_wsl_local_shell_choice_is_structured_and_wsl_only():
    original_is_wsl = standterm.is_wsl
    plugin = standterm.TERMINAL_BACKEND_REGISTRY.get(standterm.CONNECTION_TYPE_LOCAL_SHELL)
    try:
        standterm.is_wsl = lambda: True
        with standterm.app.test_request_context('/'):
            option = plugin.build_policy_option(browser_authorized=False)
        shell_options = option['shell_options']
        assert [item['kind'] for item in shell_options] == ['bash', 'cmd', 'powershell']
        assert option['default_shell_kind'] == 'bash'

        payload, error = plugin.validate_start_payload(
            {'local_shell_kind': 'cmd'},
            standterm.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error is None
        assert payload['local_shell_config']['shell_kind'] == 'cmd'
        assert payload['local_shell_config']['terminal_label'] == 'cmd.exe'

        payload, error = plugin.validate_start_payload(
            {},
            standterm.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error is None
        assert payload['local_shell_config']['shell_kind'] == 'bash'

        _payload, error = plugin.validate_start_payload(
            {'local_shell_kind': 'zsh'},
            standterm.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error['error_code'] == 'local_shell_invalid_kind'

        standterm.is_wsl = lambda: False
        _payload, error = plugin.validate_start_payload(
            {'local_shell_kind': 'cmd'},
            standterm.TERMINAL_ID_MAIN,
            '127.0.0.1',
            browser_authorized=False,
        )
        assert error['error_code'] == 'local_shell_kind_not_supported'
    finally:
        standterm.is_wsl = original_is_wsl


def test_terminal_policy_creates_authorized_dir_for_fresh_checkout():
    original_authorized_dir = standterm.AUTHORIZED_DIR
    original_authorized_browsers_path = standterm.AUTHORIZED_BROWSERS_PATH
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            authorized_dir = Path(temp_dir) / 'authorized'
            standterm.AUTHORIZED_DIR = authorized_dir
            standterm.AUTHORIZED_BROWSERS_PATH = authorized_dir / 'browsers.json'
            assert not authorized_dir.exists()

            with standterm.app.test_request_context('/'):
                policy = standterm.build_terminal_policy(browser_authorized=False)

            assert authorized_dir.is_dir()
            assert policy['authorized_dir'] == str(authorized_dir)
            assert policy['authorized_dir_ready'] is True
    finally:
        standterm.AUTHORIZED_DIR = original_authorized_dir
        standterm.AUTHORIZED_BROWSERS_PATH = original_authorized_browsers_path


def test_wsl_client_ips_require_explicit_trust_for_local_resources():
    original_is_wsl = standterm.is_wsl
    original_get_wsl_host_addresses = standterm.get_wsl_host_addresses
    original_get_wsl_ip = standterm.get_wsl_ip
    original_standterm_env = standterm.os.environ.get('STANDTERM_TRUST_WSL_CLIENT_IPS')
    try:
        standterm.is_wsl = lambda: True
        standterm.get_wsl_host_addresses = lambda: {standterm.ipaddress.ip_address('172.20.0.1')}
        standterm.get_wsl_ip = lambda: '172.20.5.10'
        standterm.os.environ.pop('STANDTERM_TRUST_WSL_CLIENT_IPS', None)

        assert standterm.is_local_client_ip('127.0.0.1') is True
        assert standterm.is_local_client_ip('172.20.0.1') is False
        assert standterm.is_local_client_ip('172.20.5.20') is False
        assert standterm.is_local_shell_allowed_for_client('172.20.0.1', browser_authorized=False) is False
        assert standterm.is_uart_allowed_for_client('172.20.0.1', browser_authorized=False) is False
        assert standterm.is_local_shell_allowed_for_client('172.20.0.1', browser_authorized=True) is True
        assert standterm.is_uart_allowed_for_client('172.20.0.1', browser_authorized=True) is True

        standterm.os.environ['STANDTERM_TRUST_WSL_CLIENT_IPS'] = '1'
        assert standterm.is_local_client_ip('172.20.0.1') is True
        assert standterm.is_local_client_ip('172.20.5.20') is True
    finally:
        standterm.is_wsl = original_is_wsl
        standterm.get_wsl_host_addresses = original_get_wsl_host_addresses
        standterm.get_wsl_ip = original_get_wsl_ip
        if original_standterm_env is None:
            standterm.os.environ.pop('STANDTERM_TRUST_WSL_CLIENT_IPS', None)
        else:
            standterm.os.environ['STANDTERM_TRUST_WSL_CLIENT_IPS'] = original_standterm_env


def test_settings_capabilities_are_separate_from_local_resource_access():
    original_is_wsl = standterm.is_wsl
    original_get_wsl_host_addresses = standterm.get_wsl_host_addresses
    original_get_wsl_ip = standterm.get_wsl_ip
    original_standterm_env = standterm.os.environ.get('STANDTERM_TRUST_WSL_CLIENT_IPS')
    try:
        standterm.is_wsl = lambda: True
        standterm.get_wsl_host_addresses = lambda: {standterm.ipaddress.ip_address('172.20.0.1')}
        standterm.get_wsl_ip = lambda: '172.20.5.10'
        standterm.os.environ.pop('STANDTERM_TRUST_WSL_CLIENT_IPS', None)

        assert standterm.is_settings_view_allowed_for_client('172.20.0.1', browser_authorized=False) is False
        assert standterm.is_settings_view_allowed_for_client('172.20.0.1', browser_authorized=True) is True
        assert standterm.is_settings_update_low_risk_allowed_for_client('172.20.0.1', browser_authorized=True) is False
        assert standterm.is_settings_update_high_risk_allowed_for_client('127.0.0.1', browser_authorized=True) is False
        assert standterm.is_settings_update_low_risk_allowed_for_client('127.0.0.1', browser_authorized=False) is True
    finally:
        standterm.is_wsl = original_is_wsl
        standterm.get_wsl_host_addresses = original_get_wsl_host_addresses
        standterm.get_wsl_ip = original_get_wsl_ip
        if original_standterm_env is None:
            standterm.os.environ.pop('STANDTERM_TRUST_WSL_CLIENT_IPS', None)
        else:
            standterm.os.environ['STANDTERM_TRUST_WSL_CLIENT_IPS'] = original_standterm_env


def test_readonly_settings_snapshot_socket_event_is_typed():
    client = make_client()

    client.emit(standterm.SETTINGS_EVENT_SNAPSHOT_REQUEST)
    snapshot = last_payload(client, standterm.SETTINGS_EVENT_SNAPSHOT)
    assert snapshot['status'] == 'ok'
    assert snapshot['settings_version'] == standterm.SETTINGS_VERSION
    assert snapshot['schema_version'] == standterm.SETTINGS_VERSION
    assert snapshot['settings_schema_version'] == standterm.SETTINGS_VERSION
    assert len(snapshot['settings_schema_digest']) == 64
    assert snapshot['settings_versions']['schema_digest'] == snapshot['settings_schema_digest']
    assert snapshot['read_only'] is False
    assert snapshot['capabilities'][standterm.CAPABILITY_SETTINGS_VIEW]['allowed'] is True
    assert snapshot['capabilities'][standterm.CAPABILITY_SETTINGS_UPDATE_HIGH_RISK]['allowed'] is False
    assert snapshot['capabilities'][standterm.CAPABILITY_SETTINGS_AUTH_MANAGE]['allowed'] is True
    assert snapshot['effective_settings']['default_connection_type'] in {
        standterm.CONNECTION_TYPE_SSH,
        standterm.CONNECTION_TYPE_LOCAL_SHELL,
        standterm.CONNECTION_TYPE_UART,
    }
    assert snapshot['mutable_settings'][standterm.SETTING_DEFAULT_CONNECTION_TYPE]['risk_level'] == 'low'
    assert snapshot['mutable_settings'][standterm.SETTING_UART_DEFAULT_BAUD_RATE]['value'] == standterm.DEFAULT_UART_BAUD_RATE
    assert snapshot['mutable_settings'][standterm.SETTING_UART_DEFAULT_BAUD_RATE]['apply_scope'] == 'next_connection'
    assert snapshot['effective_settings'][standterm.SETTING_UART_DEFAULT_BAUD_RATE] == standterm.DEFAULT_UART_BAUD_RATE
    assert snapshot['settings_schema']['core'][0]['setting_key'] == standterm.SETTING_DEFAULT_CONNECTION_TYPE
    assert snapshot['settings_schema']['core'][0]['mutable'] is True
    assert snapshot['settings_schema']['core'][0]['storage_owner'] == 'core'
    assert [item['connection_type'] for item in snapshot['effective_settings']['connection_types']]
    assert 'authorized_dir' not in snapshot['effective_settings']

    client.disconnect()


def test_backend_settings_schema_is_declared_and_typed():
    original_is_wsl = standterm.is_wsl
    try:
        standterm.is_wsl = lambda: True
        schema = standterm.TERMINAL_BACKEND_REGISTRY.build_settings_schema()
    finally:
        standterm.is_wsl = original_is_wsl

    by_key = {item['setting_key']: item for item in schema}
    assert by_key['ssh.default_host']['connection_type'] == standterm.CONNECTION_TYPE_SSH
    assert by_key['ssh.default_port']['value_type'] == 'integer'
    assert by_key['local_shell.default_kind']['allowed_values'] == ['bash', 'cmd', 'powershell']
    assert by_key['local_shell.default_kind']['apply_scope'] == 'next_connection'
    assert by_key['local_shell.default_kind']['storage_owner'] == 'core'
    assert by_key['local_shell.remote_access']['risk_level'] == 'high'
    assert by_key['local_shell.remote_access']['apply_scope'] == 'restart'
    assert by_key['uart.default_baud_rate']['allowed_values'] == standterm.UART_BAUD_RATES
    assert by_key['uart.default_baud_rate']['mutable'] is True
    assert by_key['uart.default_baud_rate']['storage_owner'] == 'core'
    assert by_key['uart.manual_port_policy']['required_capability'] == standterm.CAPABILITY_SETTINGS_UPDATE_HIGH_RISK
    assert all('required_capability' in item for item in schema)
    assert all('value' not in item for item in schema)
    assert [item['setting_key'] for item in schema if item['mutable']] == [standterm.SETTING_UART_DEFAULT_BAUD_RATE]


def test_backend_settings_schema_rejects_unsafe_capability_mapping():
    class FakePlugin:
        connection_type = 'fake'
        label = 'Fake'

        def get_settings_schema(self):
            return [{
                'setting_key': 'fake.remote_access',
                'value_type': 'boolean',
                'risk_level': 'high',
                'required_capability': standterm.CAPABILITY_SETTINGS_UPDATE_LOW_RISK,
            }]

    registry = standterm.TerminalBackendRegistry(
        [FakePlugin()],
        lambda value: value,
        known_settings_capabilities=standterm.SETTINGS_KNOWN_UPDATE_CAPABILITIES,
        risk_capability_rules=standterm.SETTINGS_RISK_CAPABILITY_RULES,
    )
    try:
        registry.build_settings_schema()
    except ValueError as exc:
        assert 'mismatched setting risk/capability' in str(exc)
    else:
        raise AssertionError('unsafe high-risk capability mapping was accepted')


def test_settings_snapshot_exposes_plugin_schema_without_high_risk_write():
    client = make_client()

    client.emit(standterm.SETTINGS_EVENT_SNAPSHOT_REQUEST)
    snapshot = last_payload(client, standterm.SETTINGS_EVENT_SNAPSHOT)
    plugin_schema = snapshot['settings_schema']['plugins']
    by_key = {item['setting_key']: item for item in plugin_schema}

    assert by_key['uart.remote_access']['risk_level'] == 'high'
    assert by_key['uart.remote_access']['required_capability'] == standterm.CAPABILITY_SETTINGS_UPDATE_HIGH_RISK
    assert snapshot['capabilities'][standterm.CAPABILITY_SETTINGS_UPDATE_HIGH_RISK]['allowed'] is False
    assert standterm.SETTING_DEFAULT_CONNECTION_TYPE in snapshot['mutable_settings']
    assert 'uart.remote_access' not in snapshot['mutable_settings']

    client.disconnect()


def test_settings_snapshot_requires_local_or_browser_authorized_client():
    client = make_client()
    session_token = current_session_token()
    sid = current_sid_for_session(session_token)

    standterm.socket_client_ips[sid] = '203.0.113.10'
    standterm.socket_browser_authorized[sid] = False
    client.emit(standterm.SETTINGS_EVENT_SNAPSHOT_REQUEST)
    denied = last_payload(client, standterm.SETTINGS_EVENT_SNAPSHOT)
    assert denied['status'] == 'failed'
    assert denied['error_code'] == 'settings_view_unauthorized'

    standterm.socket_browser_authorized[sid] = True
    client.emit(standterm.SETTINGS_EVENT_SNAPSHOT_REQUEST)
    allowed = last_payload(client, standterm.SETTINGS_EVENT_SNAPSHOT)
    assert allowed['status'] == 'ok'
    assert allowed['capabilities'][standterm.CAPABILITY_SETTINGS_VIEW]['allowed'] is True
    assert allowed['capabilities'][standterm.CAPABILITY_SETTINGS_UPDATE_LOW_RISK]['allowed'] is False

    client.disconnect()


def test_low_risk_settings_update_is_versioned_and_audited():
    client = make_client()

    client.emit(standterm.SETTINGS_EVENT_SNAPSHOT_REQUEST)
    snapshot = last_payload(client, standterm.SETTINGS_EVENT_SNAPSHOT)
    assert snapshot['settings_version'] == 1

    client.emit(standterm.SETTINGS_EVENT_UPDATE_REQUEST, {
        'request_id': 'settings-schema-stale',
        'setting_key': standterm.SETTING_DEFAULT_CONNECTION_TYPE,
        'value': standterm.CONNECTION_TYPE_SSH,
        'expected_version': snapshot['settings_version'],
        'expected_schema_digest': '0' * 64,
    })
    schema_stale = last_payload(client, standterm.SETTINGS_EVENT_UPDATE_RESULT)
    assert schema_stale['status'] == 'failed'
    assert schema_stale['error_code'] == 'settings_schema_conflict'
    assert schema_stale['settings_version'] == 1

    target = standterm.CONNECTION_TYPE_SSH
    client.emit(standterm.SETTINGS_EVENT_UPDATE_REQUEST, {
        'request_id': 'settings-update-ok',
        'setting_key': standterm.SETTING_DEFAULT_CONNECTION_TYPE,
        'value': target,
        'expected_version': snapshot['settings_version'],
        'expected_schema_digest': snapshot['settings_schema_digest'],
    })
    result = last_payload(client, standterm.SETTINGS_EVENT_UPDATE_RESULT)
    assert result['status'] == 'ok'
    assert result['setting_key'] == standterm.SETTING_DEFAULT_CONNECTION_TYPE
    assert result['value'] == target
    assert result['settings_version'] == 2

    client.emit(standterm.SETTINGS_EVENT_UPDATE_REQUEST, {
        'request_id': 'settings-update-stale',
        'setting_key': standterm.SETTING_DEFAULT_CONNECTION_TYPE,
        'value': standterm.CONNECTION_TYPE_LOCAL_SHELL,
        'expected_version': 1,
    })
    stale = last_payload(client, standterm.SETTINGS_EVENT_UPDATE_RESULT)
    assert stale['status'] == 'failed'
    assert stale['error_code'] == 'settings_version_conflict'

    events = standterm.settings_audit_store.get_recent()
    assert any(event['event_type'] == standterm.SETTINGS_AUDIT_UPDATE_SUCCEEDED for event in events)
    assert any(
        event['event_type'] == standterm.SETTINGS_AUDIT_UPDATE_FAILED
        and event.get('error_code') == 'settings_version_conflict'
        for event in events
    )

    client.disconnect()


def test_uart_default_baud_rate_runtime_update_uses_plugin_validation():
    client = make_client()

    client.emit(standterm.SETTINGS_EVENT_SNAPSHOT_REQUEST)
    snapshot = last_payload(client, standterm.SETTINGS_EVENT_SNAPSHOT)
    assert snapshot['mutable_settings'][standterm.SETTING_UART_DEFAULT_BAUD_RATE]['value'] == standterm.DEFAULT_UART_BAUD_RATE

    target = 230400
    client.emit(standterm.SETTINGS_EVENT_UPDATE_REQUEST, {
        'request_id': 'uart-baud-ok',
        'setting_key': standterm.SETTING_UART_DEFAULT_BAUD_RATE,
        'value': str(target),
        'expected_version': snapshot['settings_version'],
        'expected_schema_digest': snapshot['settings_schema_digest'],
    })
    result = last_payload(client, standterm.SETTINGS_EVENT_UPDATE_RESULT)
    assert result['status'] == 'ok'
    assert result['setting_key'] == standterm.SETTING_UART_DEFAULT_BAUD_RATE
    assert result['value'] == target
    assert result['settings_version'] == 2

    client.emit(standterm.SETTINGS_EVENT_SNAPSHOT_REQUEST)
    updated = last_payload(client, standterm.SETTINGS_EVENT_SNAPSHOT)
    assert updated['mutable_settings'][standterm.SETTING_UART_DEFAULT_BAUD_RATE]['value'] == target
    assert updated['effective_settings'][standterm.SETTING_UART_DEFAULT_BAUD_RATE] == target

    with standterm.app.test_request_context('/'):
        policy = standterm.build_terminal_policy(browser_authorized=False, client_ip='127.0.0.1')
    uart_option = next(
        item for item in policy['connection_options']
        if item['connection_type'] == standterm.CONNECTION_TYPE_UART
    )
    assert uart_option['default_baud_rate'] == target

    client.emit(standterm.SETTINGS_EVENT_UPDATE_REQUEST, {
        'request_id': 'uart-baud-invalid',
        'setting_key': standterm.SETTING_UART_DEFAULT_BAUD_RATE,
        'value': 12345,
        'expected_version': standterm.get_runtime_settings_version(),
        'expected_schema_digest': updated['settings_schema_digest'],
    })
    invalid = last_payload(client, standterm.SETTINGS_EVENT_UPDATE_RESULT)
    assert invalid['status'] == 'failed'
    assert invalid['error_code'] == 'settings_invalid_value'

    events = standterm.settings_audit_store.get_recent()
    assert any(
        event['event_type'] == standterm.SETTINGS_AUDIT_UPDATE_SUCCEEDED
        and event.get('setting_key') == standterm.SETTING_UART_DEFAULT_BAUD_RATE
        and event.get('storage_owner') == 'core'
        for event in events
    )

    client.disconnect()


def test_remote_browser_authorization_alone_cannot_update_settings():
    client = make_client()
    session_token = current_session_token()
    sid = current_sid_for_session(session_token)

    standterm.socket_client_ips[sid] = '203.0.113.10'
    standterm.socket_browser_authorized[sid] = True
    client.emit(standterm.SETTINGS_EVENT_UPDATE_REQUEST, {
        'request_id': 'settings-remote-denied',
        'setting_key': standterm.SETTING_DEFAULT_CONNECTION_TYPE,
        'value': standterm.CONNECTION_TYPE_SSH,
        'expected_version': standterm.get_runtime_settings_version(),
    })
    denied = last_payload(client, standterm.SETTINGS_EVENT_UPDATE_RESULT)
    assert denied['status'] == 'failed'
    assert denied['error_code'] == 'settings_admin_grant_required'

    client.disconnect()


def test_settings_admin_grant_is_scoped_and_revocable():
    client = make_client()
    session_token = current_session_token()
    sid = current_sid_for_session(session_token)
    standterm.socket_browser_identities[sid] = {
        'browser_id': 'a' * 64,
        'public_key': 'public-key',
    }

    client.emit(standterm.SETTINGS_EVENT_ADMIN_GRANT_REQUEST, {
        'capability': standterm.CAPABILITY_SETTINGS_UPDATE_LOW_RISK,
    })
    grant = last_payload(client, standterm.SETTINGS_EVENT_ADMIN_GRANT_RESULT)
    assert grant['status'] == 'ok'
    assert standterm.CAPABILITY_SETTINGS_UPDATE_LOW_RISK in grant['capabilities']

    standterm.socket_client_ips[sid] = '203.0.113.10'
    standterm.socket_browser_authorized[sid] = True
    client.emit(standterm.SETTINGS_EVENT_UPDATE_REQUEST, {
        'request_id': 'settings-grant-client-mismatch',
        'setting_key': standterm.SETTING_DEFAULT_CONNECTION_TYPE,
        'value': standterm.CONNECTION_TYPE_SSH,
        'expected_version': standterm.get_runtime_settings_version(),
        'grant_id': grant['grant_id'],
    })
    denied = last_payload(client, standterm.SETTINGS_EVENT_UPDATE_RESULT)
    assert denied['status'] == 'failed'
    assert denied['error_code'] == 'settings_admin_grant_client_mismatch'

    standterm.socket_client_ips[sid] = '127.0.0.1'
    client.emit(standterm.SETTINGS_EVENT_ADMIN_GRANT_REVOKE, {
        'grant_id': grant['grant_id'],
    })
    revoked = last_payload(client, standterm.SETTINGS_EVENT_ADMIN_GRANT_RESULT)
    assert revoked['status'] == 'ok'
    assert revoked['revoked'] is True

    events = standterm.settings_audit_store.get_recent()
    assert any(event['event_type'] == standterm.SETTINGS_AUDIT_GRANT_CREATED for event in events)
    assert any(event['event_type'] == standterm.SETTINGS_AUDIT_GRANT_REVOKED for event in events)

    client.disconnect()


def test_ssh_backend_action_contract_uses_public_bridge_method():
    action_store = standterm.BackendActionStore(time_func=lambda: 1000)
    calls = []

    class ActionBridge:
        def prepare_backend_action(self, action_type, payload, expires_at, message=None, question=None):
            calls.append((action_type, payload, expires_at, message, question))
            return standterm.BackendAction(
                action_type=action_type,
                terminal_id=payload['terminal_id'],
                metadata={'username': payload['username'], 'key_entry': {'line': 'ssh-ed25519 AAAA test'}},
                expires_at=expires_at,
                message=message,
                question=question,
            )

        def __getattr__(self, name):
            if name.startswith('_'):
                raise AssertionError(f'Backend plugin used private bridge method: {name}')
            raise AttributeError(name)

    plugin = standterm.SSHBackendPlugin(
        bridge_cls=ActionBridge,
        default_host=standterm.SSH_HOST,
        default_port=standterm.SSH_PORT,
        default_user=standterm.SSH_USER,
        max_host_length=standterm.MAX_HOST_LENGTH,
        max_username_length=standterm.MAX_USERNAME_LENGTH,
        max_password_bytes=standterm.MAX_PASSWORD_BYTES,
        has_control_chars=standterm.has_control_chars,
        allowed_action_types={'offer_localhost_key_setup'},
        backend_action_store=action_store,
        bridge_kwargs={},
        low_risk_settings_capability=standterm.CAPABILITY_SETTINGS_UPDATE_LOW_RISK,
        high_risk_settings_capability=standterm.CAPABILITY_SETTINGS_UPDATE_HIGH_RISK,
        key_setup_ttl_seconds=120,
        token_urlsafe=lambda _length: 'action-token',
        time_func=lambda: 1000,
    )
    payload = {
        'connection_type': standterm.CONNECTION_TYPE_SSH,
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'host': '127.0.0.1',
        'port': 22,
        'username': standterm.SSH_USER,
    }

    failure = plugin.prepare_connection_failure(
        'sid-1',
        ActionBridge(),
        payload,
        {
            'message': 'key failed',
            'error_code': 'localhost_key_not_authorized',
            'action_type': 'offer_localhost_key_setup',
            'action_message': 'message',
            'action_question': 'question',
        },
    )

    assert failure['action_id'] == 'action-token'
    action, error = action_store.get('sid-1', 'action-token', standterm.secrets.compare_digest)
    assert error is None
    assert action.action_type == 'offer_localhost_key_setup'
    assert action.metadata['username'] == standterm.SSH_USER
    assert calls == [
        ('offer_localhost_key_setup', payload, 1120, 'message', 'question'),
    ]


def test_ssh_bridge_is_provided_by_backend_module():
    assert standterm.SSHBridge.__module__ == 'terminal_backends.ssh'
    plugin = standterm.TERMINAL_BACKEND_REGISTRY.get(standterm.CONNECTION_TYPE_SSH)
    bridge = plugin.create_bridge(standterm.ACCESS_TOKEN, standterm.TERMINAL_ID_MAIN, {})
    assert isinstance(bridge, standterm.TerminalBridge)
    assert bridge.connection_type == standterm.CONNECTION_TYPE_SSH
    bridge.close()


def test_local_shell_bridge_is_provided_by_backend_module():
    assert standterm.LocalShellBridge.__module__ == 'terminal_backends.local_shell'
    plugin = standterm.TERMINAL_BACKEND_REGISTRY.get(standterm.CONNECTION_TYPE_LOCAL_SHELL)
    shell_config, error = standterm.get_default_local_shell_config()
    assert error is None
    bridge = plugin.create_bridge(
        standterm.ACCESS_TOKEN,
        standterm.TERMINAL_ID_MAIN,
        {'local_shell_config': shell_config},
    )
    assert isinstance(bridge, standterm.TerminalBridge)
    assert bridge.connection_type == standterm.CONNECTION_TYPE_LOCAL_SHELL
    assert bridge.terminal_label == shell_config['terminal_label']
    bridge.close()


def test_uart_bridge_is_provided_by_backend_module():
    assert standterm.UARTBridge.__module__ == 'terminal_backends.uart'
    plugin = standterm.TERMINAL_BACKEND_REGISTRY.get(standterm.CONNECTION_TYPE_UART)
    bridge = plugin.create_bridge(
        standterm.ACCESS_TOKEN,
        standterm.TERMINAL_ID_MAIN,
        {
            'serial_port_info': {
                'device': '/dev/ttyS0',
                'label': 'COM1 (/dev/ttyS0)',
            },
            'baud_rate': standterm.DEFAULT_UART_BAUD_RATE,
        },
    )
    assert isinstance(bridge, standterm.TerminalBridge)
    assert bridge.connection_type == standterm.CONNECTION_TYPE_UART
    assert bridge.terminal_label == 'UART COM1 (/dev/ttyS0)'
    bridge.close()


def test_agent_audit_records_typed_events_without_raw_action_data():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'

    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)
    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'],
    })
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['status'] == standterm.AGENT_STATUS_COMPLETED

    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    event_types = [event['event_type'] for event in audit_events]
    assert standterm.AGENT_AUDIT_VIEWER_ATTACH in event_types
    assert standterm.AGENT_AUDIT_MODE_SET in event_types
    assert standterm.AGENT_AUDIT_PROVIDER_RUN_REQUEST in event_types
    assert standterm.AGENT_AUDIT_CONTEXT_BUILT in event_types
    assert standterm.AGENT_AUDIT_PROPOSAL_CREATED in event_types
    assert standterm.AGENT_AUDIT_ACTION_APPROVE in event_types
    assert standterm.AGENT_AUDIT_ACTION_RESULT in event_types

    context_events = [
        event for event in audit_events
        if event['event_type'] == standterm.AGENT_AUDIT_CONTEXT_BUILT
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

    client_a.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client_a.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client_a.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'secret\n',
    })
    action = last_payload(client_a, standterm.AGENT_EVENT_ACTION_REQUEST)

    client_b.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    assert last_payload(client_b, standterm.AGENT_EVENT_ACTION_RESULT)['error_code'] == standterm.AGENT_ERROR_NOT_ATTACHED

    client_a.disconnect()
    client_b.disconnect()


def test_stale_mode_version_cannot_approve_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'versioned\n',
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'] + 1,
    })
    assert bridge.writes == []
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['error_code'] == standterm.AGENT_ERROR_STALE_MODE_VERSION

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'],
    })
    assert bridge.writes == ['versioned\n']

    client.disconnect()


def test_stale_privacy_version_cannot_approve_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'privacy-versioned\n',
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)

    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    assert last_payload(client, standterm.AGENT_EVENT_STATE)['privacy_version'] == action['privacy_version'] + 1

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'proposal_id': action['proposal_id'],
        'mode_version': action['mode_version'],
        'privacy_version': action['privacy_version'],
    })
    assert bridge.writes == []
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['error_code'] == standterm.AGENT_ERROR_STALE_PROPOSAL

    client.disconnect()


def test_mode_change_cancels_pending_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'stale\n',
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)

    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'observe',
    })
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['error_code'] == standterm.AGENT_REASON_MODE_CHANGED

    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['error_code'] == standterm.AGENT_ERROR_ACTION_NOT_PENDING

    client.disconnect()


def test_agent_reject_uses_stale_and_pending_checks():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'reject-stale\n',
    })
    stale_action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)
    stale_reject = {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': stale_action['action_id'],
        'proposal_id': stale_action['proposal_id'],
        'session_id': stale_action['session_id'],
        'viewer_id': stale_action['viewer_id'],
        'agent_binding_id': stale_action['agent_binding_id'],
        'mode_version': stale_action['mode_version'],
        'privacy_version': stale_action['privacy_version'],
    }
    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    assert last_payload(client, standterm.AGENT_EVENT_STATE)['privacy_version'] == stale_action['privacy_version'] + 1

    client.emit(standterm.AGENT_EVENT_ACTION_REJECT, stale_reject)
    stale_result = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert stale_result['status'] == standterm.AGENT_STATUS_FAILED
    assert stale_result['error_code'] == standterm.AGENT_ERROR_STALE_PROPOSAL
    state = standterm.get_agent_state(session_token, standterm.TERMINAL_ID_MAIN, sid)
    assert state.pending_actions[stale_action['action_id']]['status'] == standterm.AGENT_STATUS_FAILED

    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_NORMAL,
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'reject-completed\n',
    })
    completed_action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)
    completed_payload = {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': completed_action['action_id'],
        'proposal_id': completed_action['proposal_id'],
        'session_id': completed_action['session_id'],
        'viewer_id': completed_action['viewer_id'],
        'agent_binding_id': completed_action['agent_binding_id'],
        'mode_version': completed_action['mode_version'],
        'privacy_version': completed_action['privacy_version'],
    }
    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, completed_payload)
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['status'] == standterm.AGENT_STATUS_COMPLETED
    assert bridge.writes == ['reject-completed\n']

    client.emit(standterm.AGENT_EVENT_ACTION_REJECT, completed_payload)
    not_pending = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert not_pending['status'] == standterm.AGENT_STATUS_FAILED
    assert not_pending['error_code'] == standterm.AGENT_ERROR_ACTION_NOT_PENDING
    assert bridge.writes == ['reject-completed\n']
    assert state.pending_actions[completed_action['action_id']]['status'] == standterm.AGENT_STATUS_COMPLETED

    client.disconnect()


def test_terminal_close_invalidates_pending_action():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mock_input': 'after-close\n',
    })
    action = last_payload(client, standterm.AGENT_EVENT_ACTION_REQUEST)
    standterm.agent_user_input_metadata_store.append_input(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        'manual-input\n',
    )

    client.emit('close_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    audit_events = standterm.agent_audit_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert any(event['event_type'] == standterm.AGENT_AUDIT_TERMINAL_CLEANUP for event in audit_events)
    client.emit(standterm.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
    })
    assert bridge.writes == []
    assert standterm.agent_user_input_metadata_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN) == []
    assert last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)['error_code'] == standterm.AGENT_ERROR_NOT_ATTACHED

    client.disconnect()


def test_disconnect_invalidates_agent_state():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    assert standterm.agent_states
    client.disconnect()
    assert not standterm.agent_states


def test_stale_epoch_write_is_rejected():
    session_token = 'session-a'
    sid = 'sid-a'
    bridge = add_dummy_bridge(session_token)
    state = standterm.get_or_create_agent_state(session_token, standterm.TERMINAL_ID_MAIN, sid)
    state.mode = standterm.AGENT_MODE_DIRECT_ACTIVE
    action, error_code = standterm.build_agent_action(
        state,
        {
            'action_type': standterm.AGENT_ACTION_TERMINAL_INPUT,
            'terminal_id': standterm.TERMINAL_ID_MAIN,
            'data': 'stale\n',
        },
        requires_approval=False,
    )
    assert error_code is None
    stale_epoch = action['control_epoch']
    state.control_epoch += 1

    ok, result = standterm.write_agent_terminal_input(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
        action['action_id'],
        stale_epoch,
    )
    assert ok is False
    assert result['error_code'] == standterm.AGENT_ERROR_STALE_EPOCH
    assert bridge.writes == []


def test_transcript_store_sanitizes_terminal_output():
    session_token = 'session-a'
    bridge = add_dummy_bridge(session_token)
    bridge.emit_output({
        'message_type': 'terminal',
        'data': '\x1b[31mred\x1b[0m\r\nnext\x00\x1b]0;title\x07\n',
    })

    transcript = standterm.agent_transcript_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
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
    assert metadata['last_output_at'] is not None
    assert metadata['terminal_quiet_ms'] >= 0


def test_ssh_input_records_agent_metadata_after_validation():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit('ssh_input', {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'whoami\nnext',
    })

    metadata = standterm.agent_user_input_metadata_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert bridge.writes == ['whoami\nnext']
    assert len(metadata) == 1
    assert metadata[0]['terminal_id'] == standterm.TERMINAL_ID_MAIN
    assert metadata[0]['byte_length'] == len('whoami\nnext'.encode('utf-8'))
    assert metadata[0]['line_count'] == 2
    assert metadata[0]['contains_control_chars'] is False
    assert metadata[0]['escaped_preview'] == 'whoami\\nnext'
    assert 'data' not in metadata[0]

    client.disconnect()


def test_agent_input_metadata_bounds_and_sanitized_preview():
    session_token = 'session-a'
    long_input = 'x' * (standterm.AGENT_USER_INPUT_PREVIEW_CHARS + 20)

    standterm.agent_user_input_metadata_store.append_input(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        long_input,
    )
    metadata = standterm.agent_user_input_metadata_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert metadata[0]['escaped_preview'].endswith('...')
    assert len(metadata[0]['escaped_preview']) == standterm.AGENT_USER_INPUT_PREVIEW_CHARS + 3

    standterm.agent_user_input_metadata_store.append_input(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        'stop\x03',
    )
    metadata = standterm.agent_user_input_metadata_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert metadata[-1]['contains_control_chars'] is True
    assert 'escaped_preview' not in metadata[-1]

    for index in range(standterm.AGENT_USER_INPUT_METADATA_MAX_EVENTS + 1):
        standterm.agent_user_input_metadata_store.append_input(
            session_token,
            standterm.TERMINAL_ID_MAIN,
            f'cmd-{index}\n',
        )
    metadata = standterm.agent_user_input_metadata_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert len(metadata) == standterm.AGENT_USER_INPUT_METADATA_MAX_EVENTS
    assert metadata[0]['escaped_preview'] == 'cmd-1\\n'


def test_privacy_state_blocks_agent_context_and_redacts_input_metadata():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'approval',
    })
    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    state = last_payload(client, standterm.AGENT_EVENT_STATE)
    assert state['privacy_state'] == standterm.AGENT_PRIVACY_PRIVATE_INPUT
    assert state['privacy_version'] == 1

    client.emit('ssh_input', {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'secret value\n',
    })
    metadata = standterm.agent_user_input_metadata_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert bridge.writes == ['secret value\n']
    assert metadata[-1]['privacy_state'] == standterm.AGENT_PRIVACY_PRIVATE_INPUT
    assert metadata[-1]['redacted'] is True
    assert 'escaped_preview' not in metadata[-1]

    context = standterm.build_agent_context(session_token, standterm.TERMINAL_ID_MAIN, current_sid_for_session(session_token))
    assert context['privacy']['context_allowed'] is False
    assert context['human_input_metadata'] == []

    client.emit(standterm.AGENT_EVENT_PROVIDER_RUN_REQUEST, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
    })
    result = last_payload(client, standterm.AGENT_EVENT_ACTION_RESULT)
    assert result['error_code'] == standterm.AGENT_ERROR_PRIVACY_BLOCKED
    assert bridge.writes == ['secret value\n']

    client.disconnect()


def test_ssh_input_does_not_record_invalid_or_oversized_metadata():
    client = make_client()
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)

    client.emit('ssh_input', {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 123,
    })
    client.emit('ssh_input', {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'data': 'x' * (standterm.MAX_SSH_INPUT_BYTES + 1),
    })

    metadata = standterm.agent_user_input_metadata_store.get_recent(session_token, standterm.TERMINAL_ID_MAIN)
    assert metadata == []
    assert bridge.writes == []

    client.disconnect()


def test_viewport_snapshot_is_sid_scoped():
    flask_client = make_flask_client()
    client_a = make_socket_client(flask_client)
    session_token = current_session_token()
    bridge = add_dummy_bridge(session_token)
    client_b = make_socket_client(flask_client)

    client_a.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    assert bridge.attached_sids

    client_b.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    result = last_payload(client_b, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['error_code'] == standterm.AGENT_ERROR_NOT_ATTACHED
    assert standterm.agent_viewport_snapshot_store._entries == {}

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

    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    snapshot = valid_viewport_snapshot(seq=1)
    snapshot['output_seq'] = bridge.output_seq
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, snapshot)
    result = last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['status'] == 'accepted'
    assert result['snapshot_seq'] == 1
    assert result['line_count'] == 2
    assert result['byte_length'] == len('lineline'.encode('utf-8'))

    stored = standterm.agent_viewport_snapshot_store.get_latest(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert stored is not None
    assert stored['lines'] == ['line', 'line']
    assert stored['untrusted'] is True
    assert stored['output_seq'] == 1

    context = standterm.build_agent_context(session_token, standterm.TERMINAL_ID_MAIN, sid)
    assert context['session_id'].startswith('ags_')
    assert context['viewer_id'].startswith('agv_')
    assert context['terminal_mirror']['source'] == 'browser_viewport_snapshot_with_headless_fallback'
    assert context['terminal_mirror']['sources'] == [
        'browser_viewport_snapshot',
        standterm.AGENT_HEADLESS_SCREEN_SOURCE,
    ]
    assert 'session_token' not in context['terminal_session']
    assert context['terminal_session']['output_seq'] == 1
    assert context['active_screen']['source'] == 'browser_viewport_snapshot'
    assert context['active_screen']['provisional'] is True
    assert context['active_screen']['output_seq'] == 1

    client.disconnect()

def test_viewport_snapshot_policy_blocks_and_clears_private_context():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_ATTACH, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=1, fill='visible'))
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    assert standterm.agent_viewport_snapshot_store.get_latest(session_token, standterm.TERMINAL_ID_MAIN, sid)

    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_PRIVATE_INPUT,
    })
    assert standterm.agent_viewport_snapshot_store.get_latest(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    ) is None

    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=2, fill='private'))
    private_result = last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert private_result['status'] == standterm.AGENT_STATUS_FAILED
    assert private_result['error_code'] == standterm.AGENT_ERROR_PRIVACY_BLOCKED
    assert standterm.agent_viewport_snapshot_store.get_latest(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    ) is None

    client.emit(standterm.AGENT_EVENT_PRIVACY_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'privacy_state': standterm.AGENT_PRIVACY_NORMAL,
    })
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=3, fill='normal'))
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    assert standterm.agent_viewport_snapshot_store.get_latest(session_token, standterm.TERMINAL_ID_MAIN, sid)

    client.emit(standterm.AGENT_EVENT_PAUSE, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    assert standterm.agent_viewport_snapshot_store.get_latest(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    ) is None
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=4, fill='paused'))
    paused_result = last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert paused_result['status'] == standterm.AGENT_STATUS_FAILED
    assert paused_result['error_code'] == standterm.AGENT_ERROR_PAUSED

    client.emit(standterm.AGENT_EVENT_RESUME, {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_MODE_SET, {
        'terminal_id': standterm.TERMINAL_ID_MAIN,
        'mode': 'disabled',
    })
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=5, fill='disabled'))
    disabled_result = last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert disabled_result['status'] == standterm.AGENT_STATUS_FAILED
    assert disabled_result['error_code'] == standterm.AGENT_ERROR_EXTERNAL_AGENT_DISABLED

    client.disconnect()


def test_viewport_snapshot_rejects_oversized_payload():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)

    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    oversized = valid_viewport_snapshot()
    oversized['lines'][0] = 'x' * (standterm.AGENT_VIEWPORT_SNAPSHOT_MAX_LINE_BYTES + 1)
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, oversized)

    result = last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['error_code'] == standterm.AGENT_ERROR_SNAPSHOT_TOO_LARGE
    assert standterm.agent_viewport_snapshot_store._entries == {}

    client.disconnect()


def test_viewport_snapshot_stale_sequence_is_rejected():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=2, fill='new'))
    assert last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)['status'] == 'accepted'
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=1, fill='old'))

    result = last_payload(client, standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT)
    assert result['status'] == 'stale'
    assert result['error_code'] == standterm.AGENT_ERROR_SNAPSHOT_STALE
    stored = standterm.agent_viewport_snapshot_store.get_latest(
        session_token,
        standterm.TERMINAL_ID_MAIN,
        sid,
    )
    assert stored['lines'] == ['new', 'new']

    client.disconnect()


def test_viewport_snapshot_context_clears_on_terminal_close_and_disconnect():
    client = make_client()
    session_token = current_session_token()
    add_dummy_bridge(session_token)
    sid = current_sid_for_session(session_token)

    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot())
    assert standterm.agent_viewport_snapshot_store.get_latest(session_token, standterm.TERMINAL_ID_MAIN, sid)

    client.emit('close_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    assert standterm.agent_viewport_snapshot_store.get_latest(session_token, standterm.TERMINAL_ID_MAIN, sid) is None

    add_dummy_bridge(session_token)
    client.emit('replay_terminal', {'terminal_id': standterm.TERMINAL_ID_MAIN})
    client.emit(standterm.AGENT_EVENT_VIEWPORT_SNAPSHOT, valid_viewport_snapshot(seq=2))
    assert standterm.agent_viewport_snapshot_store.get_latest(session_token, standterm.TERMINAL_ID_MAIN, sid)

    client.disconnect()
    assert standterm.agent_viewport_snapshot_store.get_latest(session_token, standterm.TERMINAL_ID_MAIN, sid) is None


def main():
    tests = [
        test_pause_blocks_pending_approval,
        test_operator_observation_logs_metadata_without_input_preview,
        test_operator_observation_state_syncs_across_viewers,
        test_approval_and_direct_writes_use_gate,
        test_provider_run_uses_agent_gate,
        test_provider_adapter_receives_context_and_exposes_run_metadata,
        test_static_env_provider_is_explicit_adapter,
        test_provider_failure_is_typed_and_does_not_write,
        test_invalid_provider_proposal_is_rejected_before_action_creation,
        test_external_agent_token_requires_enabled_agent_panel,
        test_external_agent_can_attach_and_read_authorized_screen,
        test_external_agent_screen_falls_back_to_headless_grid_without_browser_snapshot,
        test_external_agent_screen_tail_lines_and_region_reduce_viewport_payload,
        test_external_agent_render_requests_browser_viewport_png,
        test_external_agent_render_mirror_screen_returns_structured_screen_without_png_request,
        test_external_agent_render_auto_uses_mirror_screen_without_png_request,
        test_external_agent_render_timeout_is_typed,
        test_external_agent_tail_reports_gap_metadata,
        test_external_agent_tail_strip_ansi_is_explicit_plain_format,
        test_external_agent_tail_limit_preserves_cursor_order,
        test_external_agent_tail_wait_returns_after_new_output,
        test_external_agent_tail_wait_times_out_without_output,
        test_external_agent_tail_wait_stops_on_pause,
        test_external_agent_wait_output_returns_structured_result_without_display_by_default,
        test_external_agent_wait_output_timeout_and_wait_quiet_are_typed,
        test_external_agent_sequence_runs_steps_and_stops_on_timeout,
        test_external_agent_observe_cannot_send,
        test_external_agent_approval_send_waits_for_human_approval,
        test_external_agent_direct_send_uses_agent_gate,
        test_external_agent_submit_after_writes_discrete_enter,
        test_external_agent_structured_text_and_keys_send_use_backend_input_kind,
        test_external_agent_direct_send_capture_returns_tail_after_write,
        test_external_agent_send_wait_strip_ansi_formats_capture_only_when_requested,
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
        test_external_agent_handoff_uses_loopback_command_url_for_non_loopback_browser_url,
        test_external_agentinfo_payload_route_and_pointer_are_tokenless,
        test_external_agent_startup_lines_point_to_launch_handoff,
        test_wsl_local_shell_choice_is_structured_and_wsl_only,
        test_terminal_policy_creates_authorized_dir_for_fresh_checkout,
        test_wsl_client_ips_require_explicit_trust_for_local_resources,
        test_settings_capabilities_are_separate_from_local_resource_access,
        test_readonly_settings_snapshot_socket_event_is_typed,
        test_backend_settings_schema_is_declared_and_typed,
        test_backend_settings_schema_rejects_unsafe_capability_mapping,
        test_settings_snapshot_exposes_plugin_schema_without_high_risk_write,
        test_settings_snapshot_requires_local_or_browser_authorized_client,
        test_low_risk_settings_update_is_versioned_and_audited,
        test_uart_default_baud_rate_runtime_update_uses_plugin_validation,
        test_remote_browser_authorization_alone_cannot_update_settings,
        test_settings_admin_grant_is_scoped_and_revocable,
        test_ssh_backend_action_contract_uses_public_bridge_method,
        test_ssh_bridge_is_provided_by_backend_module,
        test_local_shell_bridge_is_provided_by_backend_module,
        test_uart_bridge_is_provided_by_backend_module,
        test_agent_audit_records_typed_events_without_raw_action_data,
        test_wrong_sid_cannot_approve_action,
        test_stale_mode_version_cannot_approve_action,
        test_stale_privacy_version_cannot_approve_action,
        test_mode_change_cancels_pending_action,
        test_agent_reject_uses_stale_and_pending_checks,
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
        test_viewport_snapshot_policy_blocks_and_clears_private_context,
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
