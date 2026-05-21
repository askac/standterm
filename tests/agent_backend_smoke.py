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
    webssh.agent_transcript_store.clear()
    webssh.agent_user_input_metadata_store.clear()


def make_client():
    flask_client = webssh.app.test_client()
    response = flask_client.get('/?token=' + webssh.ACCESS_TOKEN)
    assert response.status_code == 200, response.status_code
    socket_client = webssh.socketio.test_client(webssh.app, flask_test_client=flask_client)
    assert socket_client.is_connected()
    return socket_client


def current_session_token():
    assert webssh.socket_session_tokens
    return next(reversed(webssh.socket_session_tokens.values()))


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
    client.emit(webssh.AGENT_EVENT_SUGGESTION_REQUEST, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'mock_input': 'approved\n',
    })
    action = last_payload(client, webssh.AGENT_EVENT_ACTION_REQUEST)
    assert action['requires_approval'] is True
    assert action['escaped_preview'] == 'approved\\n'

    client.emit(webssh.AGENT_EVENT_ACTION_APPROVE, {
        'terminal_id': webssh.TERMINAL_ID_MAIN,
        'action_id': action['action_id'],
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


def main():
    tests = [
        test_pause_blocks_pending_approval,
        test_approval_and_direct_writes_use_gate,
        test_wrong_sid_cannot_approve_action,
        test_mode_change_cancels_pending_action,
        test_terminal_close_invalidates_pending_action,
        test_disconnect_invalidates_agent_state,
        test_stale_epoch_write_is_rejected,
        test_transcript_store_sanitizes_terminal_output,
        test_ssh_input_records_agent_metadata_after_validation,
        test_agent_input_metadata_bounds_and_sanitized_preview,
        test_ssh_input_does_not_record_invalid_or_oversized_metadata,
    ]
    for test in tests:
        reset_state()
        test()
        print(f'{test.__name__}: ok')


if __name__ == '__main__':
    main()
