import sys
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


def add_dummy_bridge(session_token):
    bridge = DummyBridge(session_token, webssh.TERMINAL_ID_MAIN)
    webssh.set_bridge(session_token, webssh.TERMINAL_ID_MAIN, bridge)
    return bridge


def valid_viewport_snapshot(seq=1, rows=2, cols=4, fill='line'):
    return {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'cols': cols,
        'rows': rows,
        'viewport_y': 0,
        'base_y': 0,
        'snapshot_seq': seq,
        'output_seq': 0,
        'captured_at': '2026-05-22T00:00:00.000Z',
        'lines': [fill for _ in range(rows)],
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
    send = webssh.process_external_agent_command({
        'op': 'send',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'data': 'blocked\n',
    })
    assert screen['error_code'] == webssh.AGENT_ERROR_PRIVACY_BLOCKED
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
        ttl_seconds=-1,
    )
    assert error_code is None
    result = webssh.process_external_agent_command({
        'op': 'state',
        'token': token,
        'terminal_id': webssh.TERMINAL_ID_MAIN,
    })
    assert result['error_code'] == webssh.AGENT_ERROR_EXTERNAL_AGENT_EXPIRED

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
        test_external_agent_observe_cannot_send,
        test_external_agent_approval_send_waits_for_human_approval,
        test_external_agent_direct_send_uses_agent_gate,
        test_external_agent_privacy_and_disabled_state_block_visibility_and_send,
        test_external_agent_token_revoke_and_terminal_close_invalidate_access,
        test_external_agent_expired_and_wrong_terminal_tokens_are_rejected,
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
