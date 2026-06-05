import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external_agent_dispatch import ExternalAgentCommandDispatcher
from external_agent_handlers import (
    ExternalAgentBasicCommandHandlers,
    ExternalAgentLifecycleCommandHandlers,
    ExternalAgentMirrorScreenRenderHandler,
    ExternalAgentReadCommandRouter,
    ExternalAgentScreenCommandHandler,
    ExternalAgentTailCommandHandler,
    ExternalAgentWaitCommandHandler,
)


def make_dispatcher(command_auth_handlers=None, authenticated_handlers=None, validate_token=None):
    errors = []

    def build_error(error_code, terminal_id=None):
        errors.append((error_code, terminal_id))
        payload = {'status': 'failed', 'error_code': error_code}
        if terminal_id is not None:
            payload['terminal_id'] = terminal_id
        return payload

    if validate_token is None:
        def validate_token(command):
            return {'token': command.get('token')}, {'state': True}, 'term-1', None

    dispatcher = ExternalAgentCommandDispatcher(
        command_auth_handlers=command_auth_handlers or {},
        authenticated_handlers=authenticated_handlers or {},
        validate_token=validate_token,
        build_error=build_error,
        invalid_data_error_code='invalid_data',
        action_not_allowed_error_code='not_allowed',
    )
    return dispatcher, errors


def test_dispatcher_rejects_invalid_op_before_validation():
    calls = []

    def validate_token(_command):
        calls.append('validate')
        return None, None, None, 'unauthorized'

    dispatcher, errors = make_dispatcher(validate_token=validate_token)

    for command in (None, [], {}, {'op': None}, {'op': 123}, {'op': '   '}):
        assert dispatcher.dispatch(command) == {
            'status': 'failed',
            'error_code': 'invalid_data',
        }

    assert calls == []
    assert errors == [('invalid_data', None)] * 6


def test_dispatcher_command_auth_handler_bypasses_shared_validation():
    calls = []

    def command_auth_handler(op, command):
        calls.append((op, command))
        return {'status': 'ok', 'handled': op}

    def validate_token(_command):
        calls.append('validate')
        return None, None, None, 'unexpected'

    dispatcher, _errors = make_dispatcher(
        command_auth_handlers={'hello': command_auth_handler},
        validate_token=validate_token,
    )

    assert dispatcher.dispatch({'op': ' Hello ', 'token': 'abc'}) == {
        'status': 'ok',
        'handled': 'hello',
    }
    assert calls == [('hello', {'op': ' Hello ', 'token': 'abc'})]


def test_dispatcher_authenticated_handler_receives_validated_state():
    calls = []

    def validate_token(command):
        calls.append(('validate', command))
        return {'record': True}, {'state': True}, 'term-1', None

    def authenticated_handler(op, command, record, state, terminal_id):
        calls.append((op, command, record, state, terminal_id))
        return {'status': 'ok'}

    dispatcher, _errors = make_dispatcher(
        authenticated_handlers={'state': authenticated_handler},
        validate_token=validate_token,
    )

    command = {'op': 'STATE', 'token': 'abc'}
    assert dispatcher.dispatch(command) == {'status': 'ok'}
    assert calls == [
        ('validate', command),
        ('state', command, {'record': True}, {'state': True}, 'term-1'),
    ]


def test_dispatcher_unknown_op_reports_action_not_allowed_after_validation():
    dispatcher, errors = make_dispatcher()

    assert dispatcher.dispatch({'op': 'unknown', 'token': 'abc'}) == {
        'status': 'failed',
        'error_code': 'not_allowed',
        'terminal_id': 'term-1',
    }
    assert errors == [('not_allowed', 'term-1')]


def test_basic_handlers_hello_uses_command_specific_validation():
    calls = []

    def validate_token(command, require_terminal=True):
        calls.append(('validate', command, require_terminal))
        return (
            {'external_agent_id': 'agent-1', 'terminal_id': 'term-1'},
            {'mode': 'direct'},
            'term-1',
            None,
        )

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code}

    def build_state_payload(record, state):
        calls.append(('state', record, state))
        return {'status': 'attached', 'mode': state['mode']}

    handlers = ExternalAgentBasicCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        build_state_payload=build_state_payload,
    )

    payload = handlers.process_hello_command('hello', {'op': 'hello', 'token': 'abc'})
    assert payload['status'] == 'ok'
    assert payload['version'] == 1
    assert payload['external_agent_id'] == 'agent-1'
    assert payload['terminal_id'] == 'term-1'
    assert payload['state'] == {'mode': 'direct'}
    assert calls == [
        ('validate', {'op': 'hello', 'token': 'abc'}, False),
        (
            'state',
            {'external_agent_id': 'agent-1', 'terminal_id': 'term-1'},
            {'mode': 'direct'},
        ),
    ]


def test_basic_handlers_hello_maps_validation_error():
    calls = []

    def validate_token(_command, require_terminal=True):
        calls.append(('validate', require_terminal))
        return None, None, 'term-1', 'unauthorized'

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    handlers = ExternalAgentBasicCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        build_state_payload=lambda _record, _state: None,
    )

    assert handlers.process_hello_command('hello', {'op': 'hello'}) == {
        'status': 'failed',
        'error_code': 'unauthorized',
        'terminal_id': 'term-1',
    }
    assert calls == [
        ('validate', False),
        ('error', 'unauthorized', 'term-1'),
    ]


def test_basic_handlers_state_returns_state_payload():
    handlers = ExternalAgentBasicCommandHandlers(
        validate_token=lambda _command, **_kwargs: None,
        build_error=lambda _error_code, terminal_id=None: None,
        build_state_payload=lambda record, state: {'record': record, 'state': state},
    )

    assert handlers.process_state_command(
        'state',
        {'op': 'state'},
        {'external_agent_id': 'agent-1'},
        {'mode': 'direct'},
        'term-1',
    ) == {
        'record': {'external_agent_id': 'agent-1'},
        'state': {'mode': 'direct'},
    }


def test_lifecycle_handlers_heartbeat_renews_without_token_validation_side_effect():
    calls = []

    def validate_token(command, renew_token=True):
        calls.append(('validate', command, renew_token))
        return {'token_hash': 'hash-1'}, {'state': True}, 'term-1', None

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code}

    def renew_record(record):
        calls.append(('renew', record))
        return {'token_hash': 'hash-1', 'renewed': True}

    def build_heartbeat_payload(record, terminal_id):
        calls.append(('heartbeat', record, terminal_id))
        return {'status': 'ok', 'record': record, 'terminal_id': terminal_id}

    handlers = ExternalAgentLifecycleCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        renew_record=renew_record,
        build_heartbeat_payload=build_heartbeat_payload,
        attach_record=lambda _token: None,
        record_attached=lambda _state, _record: None,
        build_state_payload=lambda _record, _state: None,
        revoke_record=lambda _token: None,
        record_revoked=lambda _state, _record: None,
    )

    assert handlers.process_heartbeat_command('heartbeat', {'op': 'heartbeat', 'token': 'abc'}) == {
        'status': 'ok',
        'record': {'token_hash': 'hash-1', 'renewed': True},
        'terminal_id': 'term-1',
    }
    assert calls == [
        ('validate', {'op': 'heartbeat', 'token': 'abc'}, False),
        ('renew', {'token_hash': 'hash-1'}),
        ('heartbeat', {'token_hash': 'hash-1', 'renewed': True}, 'term-1'),
    ]


def test_lifecycle_handlers_heartbeat_maps_validation_error_without_renew():
    calls = []

    def validate_token(_command, renew_token=True):
        calls.append(('validate', renew_token))
        return None, None, 'term-1', 'expired'

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    handlers = ExternalAgentLifecycleCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        renew_record=lambda _record: calls.append('renew'),
        build_heartbeat_payload=lambda _record, _terminal_id: None,
        attach_record=lambda _token: calls.append('attach'),
        record_attached=lambda _state, _record: calls.append('audit'),
        build_state_payload=lambda _record, _state: None,
        revoke_record=lambda _token: calls.append('revoke'),
        record_revoked=lambda _state, _record: calls.append('audit-revoke'),
    )

    assert handlers.process_heartbeat_command('heartbeat', {'op': 'heartbeat'}) == {
        'status': 'failed',
        'error_code': 'expired',
        'terminal_id': 'term-1',
    }
    assert calls == [
        ('validate', False),
        ('error', 'expired', 'term-1'),
    ]


def test_lifecycle_handlers_attach_marks_record_and_audits():
    calls = []

    def validate_token(command):
        calls.append(('validate', command))
        return {'pre_attach': True}, {'mode': 'direct'}, 'term-1', None

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code}

    def attach_record(token):
        calls.append(('attach', token))
        return {'external_agent_id': 'agent-1'}, None

    def record_attached(state, record):
        calls.append(('audit', state, record))

    def build_state_payload(record, state):
        calls.append(('state', record, state))
        return {'status': 'attached', 'external_agent_id': record['external_agent_id']}

    handlers = ExternalAgentLifecycleCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        renew_record=lambda _record: None,
        build_heartbeat_payload=lambda _record, _terminal_id: None,
        attach_record=attach_record,
        record_attached=record_attached,
        build_state_payload=build_state_payload,
        revoke_record=lambda _token: None,
        record_revoked=lambda _state, _record: None,
    )

    assert handlers.process_attach_command('attach', {'op': 'attach', 'token': 'abc'}) == {
        'status': 'attached',
        'external_agent_id': 'agent-1',
    }
    assert calls == [
        ('validate', {'op': 'attach', 'token': 'abc'}),
        ('attach', 'abc'),
        ('audit', {'mode': 'direct'}, {'external_agent_id': 'agent-1'}),
        ('state', {'external_agent_id': 'agent-1'}, {'mode': 'direct'}),
    ]


def test_lifecycle_handlers_attach_maps_store_error_without_audit():
    calls = []

    def validate_token(_command):
        calls.append('validate')
        return {'pre_attach': True}, {'mode': 'direct'}, 'term-1', None

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    def attach_record(token):
        calls.append(('attach', token))
        return None, 'revoked'

    handlers = ExternalAgentLifecycleCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        renew_record=lambda _record: None,
        build_heartbeat_payload=lambda _record, _terminal_id: None,
        attach_record=attach_record,
        record_attached=lambda _state, _record: calls.append('audit'),
        build_state_payload=lambda _record, _state: calls.append('state'),
        revoke_record=lambda _token: calls.append('revoke'),
        record_revoked=lambda _state, _record: calls.append('audit-revoke'),
    )

    assert handlers.process_attach_command('attach', {'op': 'attach', 'token': 'abc'}) == {
        'status': 'failed',
        'error_code': 'revoked',
        'terminal_id': 'term-1',
    }
    assert calls == [
        'validate',
        ('attach', 'abc'),
        ('error', 'revoked', 'term-1'),
    ]


def test_lifecycle_handlers_revoke_marks_record_and_audits():
    calls = []

    def validate_token(command):
        calls.append(('validate', command))
        return {'pre_revoke': True}, {'mode': 'direct'}, 'term-1', None

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code}

    def revoke_record(token):
        calls.append(('revoke', token))
        return {'external_agent_id': 'agent-1'}, None

    def record_revoked(state, record):
        calls.append(('audit', state, record))

    handlers = ExternalAgentLifecycleCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        renew_record=lambda _record: None,
        build_heartbeat_payload=lambda _record, _terminal_id: None,
        attach_record=lambda _token: None,
        record_attached=lambda _state, _record: None,
        build_state_payload=lambda _record, _state: None,
        revoke_record=revoke_record,
        record_revoked=record_revoked,
    )

    assert handlers.process_revoke_command('revoke', {'op': 'revoke', 'token': 'abc'}) == {
        'status': 'ok',
        'terminal_id': 'term-1',
        'external_agent_id': 'agent-1',
        'revoked': True,
    }
    assert calls == [
        ('validate', {'op': 'revoke', 'token': 'abc'}),
        ('revoke', 'abc'),
        ('audit', {'mode': 'direct'}, {'external_agent_id': 'agent-1'}),
    ]


def test_lifecycle_handlers_revoke_maps_store_error_without_audit():
    calls = []

    def validate_token(_command):
        calls.append('validate')
        return {'pre_revoke': True}, {'mode': 'direct'}, 'term-1', None

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    def revoke_record(token):
        calls.append(('revoke', token))
        return None, 'revoked'

    handlers = ExternalAgentLifecycleCommandHandlers(
        validate_token=validate_token,
        build_error=build_error,
        renew_record=lambda _record: None,
        build_heartbeat_payload=lambda _record, _terminal_id: None,
        attach_record=lambda _token: None,
        record_attached=lambda _state, _record: None,
        build_state_payload=lambda _record, _state: None,
        revoke_record=revoke_record,
        record_revoked=lambda _state, _record: calls.append('audit'),
    )

    assert handlers.process_revoke_command('revoke', {'op': 'revoke', 'token': 'abc'}) == {
        'status': 'failed',
        'error_code': 'revoked',
        'terminal_id': 'term-1',
    }
    assert calls == [
        'validate',
        ('revoke', 'abc'),
        ('error', 'revoked', 'term-1'),
    ]


def make_read_router(
    *,
    read_handlers=None,
    is_context_allowed=None,
    get_bridge=None,
):
    calls = []

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    if is_context_allowed is None:
        def is_context_allowed(_state):
            calls.append('context')
            return True

    if get_bridge is None:
        def get_bridge(session_token, terminal_id):
            calls.append(('bridge', session_token, terminal_id))
            return {'bridge': True}

    router = ExternalAgentReadCommandRouter(
        read_handlers=read_handlers or {},
        build_error=build_error,
        is_context_allowed=is_context_allowed,
        get_bridge=get_bridge,
        privacy_blocked_error_code='privacy_blocked',
        terminal_not_found_error_code='terminal_not_found',
        action_not_allowed_error_code='not_allowed',
    )
    return router, calls


def test_read_router_rejects_privacy_block_before_bridge_lookup():
    calls = []

    def is_context_allowed(state):
        calls.append(('context', state))
        return False

    def get_bridge(_session_token, _terminal_id):
        calls.append('bridge')
        return {'bridge': True}

    router, router_calls = make_read_router(
        is_context_allowed=is_context_allowed,
        get_bridge=get_bridge,
    )

    assert router.process_read_command(
        'screen',
        {'op': 'screen'},
        {'session_token': 'session-1'},
        {'privacy': 'blocked'},
        'term-1',
    ) == {
        'status': 'failed',
        'error_code': 'privacy_blocked',
        'terminal_id': 'term-1',
    }
    assert calls == [('context', {'privacy': 'blocked'})]
    assert router_calls == [('error', 'privacy_blocked', 'term-1')]


def test_read_router_maps_missing_bridge():
    calls = []

    def get_bridge(session_token, terminal_id):
        calls.append(('bridge', session_token, terminal_id))
        return None

    router, router_calls = make_read_router(get_bridge=get_bridge)

    assert router.process_read_command(
        'screen',
        {'op': 'screen'},
        {'session_token': 'session-1'},
        {'privacy': 'allowed'},
        'term-1',
    ) == {
        'status': 'failed',
        'error_code': 'terminal_not_found',
        'terminal_id': 'term-1',
    }
    assert calls == [('bridge', 'session-1', 'term-1')]
    assert router_calls == [
        'context',
        ('error', 'terminal_not_found', 'term-1'),
    ]


def test_read_router_maps_unknown_read_op_after_bridge_lookup():
    router, calls = make_read_router()

    assert router.process_read_command(
        'unknown',
        {'op': 'unknown'},
        {'session_token': 'session-1'},
        {'privacy': 'allowed'},
        'term-1',
    ) == {
        'status': 'failed',
        'error_code': 'not_allowed',
        'terminal_id': 'term-1',
    }
    assert calls == [
        'context',
        ('bridge', 'session-1', 'term-1'),
        ('error', 'not_allowed', 'term-1'),
    ]


def test_read_router_delegates_to_concrete_handler_with_bridge():
    calls = []

    def handle_screen(op, command, record, state, terminal_id, bridge):
        calls.append((op, command, record, state, terminal_id, bridge))
        return {'status': 'ok', 'op': op}

    router, router_calls = make_read_router(read_handlers={'screen': handle_screen})
    command = {'op': 'screen'}
    record = {'session_token': 'session-1'}
    state = {'privacy': 'allowed'}

    assert router.process_read_command('screen', command, record, state, 'term-1') == {
        'status': 'ok',
        'op': 'screen',
    }
    assert router_calls == [
        'context',
        ('bridge', 'session-1', 'term-1'),
    ]
    assert calls == [
        ('screen', command, record, state, 'term-1', {'bridge': True}),
    ]


class OutputSeqBridge:
    def __init__(self, output_seq):
        self.output_seq = output_seq


class PublicState:
    def __init__(self, payload):
        self.payload = payload

    def public_state(self):
        return self.payload


def make_screen_handler(
    *,
    parse_screen_options=None,
    build_screen_wait=None,
):
    calls = []

    if parse_screen_options is None:
        def parse_screen_options(command):
            calls.append(('parse', command))
            return {'tail_lines': 5}, None

    if build_screen_wait is None:
        def build_screen_wait(bridge, state, wait_ms=None, quiet_ms=None):
            calls.append(('wait', bridge, state, wait_ms, quiet_ms))
            return {'wait_ms': wait_ms, 'quiet_ms': quiet_ms}, None

    def build_context(session_token, terminal_id, sid):
        calls.append(('context', session_token, terminal_id, sid))
        return {'active_screen': {'lines': ['one', 'two']}, 'sid': sid}

    def apply_screen_options(active_screen, screen_options):
        calls.append(('apply', active_screen, screen_options))
        return {'active_screen': active_screen, 'options': screen_options}

    def summarize_context(context):
        calls.append(('summary', context))
        return {'sid': context.get('sid'), 'line_count': len(context['active_screen']['lines'])}

    def record_audit(state, event_type, **metadata):
        calls.append(('audit', state, event_type, metadata))

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    handler = ExternalAgentScreenCommandHandler(
        parse_screen_options=parse_screen_options,
        build_screen_wait=build_screen_wait,
        build_context=build_context,
        apply_screen_options=apply_screen_options,
        summarize_context=summarize_context,
        record_audit=record_audit,
        build_error=build_error,
        audit_event_type='screen_audit',
    )
    return handler, calls


def test_screen_handler_maps_parse_error_without_wait_or_audit():
    calls = []

    def parse_screen_options(command):
        calls.append(('parse', command))
        return None, 'bad_screen'

    handler, handler_calls = make_screen_handler(parse_screen_options=parse_screen_options)
    state = PublicState({'mode': 'observe'})
    command = {'op': 'screen', 'tail_lines': 'bad'}

    assert handler.process_screen_command(
        'screen',
        command,
        {'external_agent_id': 'agent-1', 'session_token': 'session-1', 'sid': 'sid-1'},
        state,
        'term-1',
        OutputSeqBridge(42),
    ) == {
        'status': 'failed',
        'error_code': 'bad_screen',
        'terminal_id': 'term-1',
    }
    assert calls == [('parse', command)]
    assert handler_calls == [('error', 'bad_screen', 'term-1')]


def test_screen_handler_maps_wait_error_without_context_or_audit():
    calls = []

    def build_screen_wait(bridge, state, wait_ms=None, quiet_ms=None):
        calls.append(('wait', bridge, state, wait_ms, quiet_ms))
        return None, 'bad_wait'

    handler, handler_calls = make_screen_handler(build_screen_wait=build_screen_wait)
    state = PublicState({'mode': 'observe'})
    command = {'op': 'screen', 'wait_ms': 100}
    bridge = OutputSeqBridge(42)

    assert handler.process_screen_command(
        'screen',
        command,
        {'external_agent_id': 'agent-1', 'session_token': 'session-1', 'sid': 'sid-1'},
        state,
        'term-1',
        bridge,
    ) == {
        'status': 'failed',
        'error_code': 'bad_wait',
        'terminal_id': 'term-1',
    }
    assert calls == [('wait', bridge, state, 100, None)]
    assert handler_calls == [
        ('parse', command),
        ('error', 'bad_wait', 'term-1'),
    ]


def test_screen_handler_builds_payload_and_records_audit():
    handler, calls = make_screen_handler()
    state = PublicState({'mode': 'observe'})
    command = {'op': 'screen', 'wait_ms': 100}
    record = {
        'external_agent_id': 'agent-1',
        'session_token': 'session-1',
        'sid': 'sid-1',
    }
    bridge = OutputSeqBridge(42)

    assert handler.process_screen_command(
        'screen',
        command,
        record,
        state,
        'term-1',
        bridge,
    ) == {
        'status': 'ok',
        'terminal_id': 'term-1',
        'external_agent_id': 'agent-1',
        'output_seq': 42,
        'state': {'mode': 'observe'},
        'screen': {
            'active_screen': {'lines': ['one', 'two']},
            'options': {'tail_lines': 5},
        },
        'screen_wait': {'wait_ms': 100, 'quiet_ms': None},
    }
    assert calls == [
        ('parse', command),
        ('wait', bridge, state, 100, None),
        ('context', 'session-1', 'term-1', 'sid-1'),
        ('apply', {'lines': ['one', 'two']}, {'tail_lines': 5}),
        ('summary', {'active_screen': {'lines': ['one', 'two']}, 'sid': 'sid-1'}),
        (
            'audit',
            state,
            'screen_audit',
            {
                'external_agent_id': 'agent-1',
                'context': {'sid': 'sid-1', 'line_count': 2},
                'screen_options': {'tail_lines': 5},
                'screen_wait': {'wait_ms': 100, 'quiet_ms': None},
            },
        ),
    ]


def test_screen_handler_omits_empty_screen_wait_payload():
    def build_screen_wait(_bridge, _state, wait_ms=None, quiet_ms=None):
        return {'wait_ms': wait_ms, 'quiet_ms': quiet_ms}, None

    handler, _calls = make_screen_handler(build_screen_wait=build_screen_wait)
    payload = handler.process_screen_command(
        'screen',
        {'op': 'screen'},
        {'external_agent_id': 'agent-1', 'session_token': 'session-1', 'sid': 'sid-1'},
        PublicState({'mode': 'observe'}),
        'term-1',
        OutputSeqBridge(42),
    )

    assert 'screen_wait' not in payload


def make_mirror_screen_render_handler():
    calls = []

    def build_render_payload(record, terminal_id):
        calls.append(('render', record, terminal_id))
        return {
            'render_mode': 'mirror_screen',
            'render_type': 'terminal_screen',
            'mime_type': 'application/vnd.standterm.screen+json',
            'source': 'headless_screen',
            'line_count': 2,
            'byte_length': 24,
            'cols': 80,
            'rows': 24,
            'output_seq': 17,
            'lines': ['one', 'two'],
        }, {'active_screen': {'lines': ['one', 'two']}, 'sid': record.get('sid')}

    def summarize_context(context):
        calls.append(('summary', context))
        return {'line_count': len(context['active_screen']['lines'])}

    def record_audit(state, event_type, **metadata):
        calls.append(('audit', state, event_type, metadata))

    handler = ExternalAgentMirrorScreenRenderHandler(
        build_render_payload=build_render_payload,
        summarize_context=summarize_context,
        record_audit=record_audit,
        audit_event_type='render_audit',
    )
    return handler, calls


def test_mirror_screen_render_handler_builds_payload_and_records_audit():
    handler, calls = make_mirror_screen_render_handler()
    state = PublicState({'mode': 'observe'})
    record = {
        'external_agent_id': 'agent-1',
        'session_token': 'session-1',
        'sid': 'sid-1',
    }
    command = {'op': 'render', 'render_mode': 'mirror_screen'}

    assert handler.process_mirror_screen_render_command(
        'render',
        command,
        record,
        state,
        'term-1',
        OutputSeqBridge(42),
        requested_render_mode='mirror_screen',
        render_mode='mirror_screen',
    ) == {
        'status': 'ok',
        'terminal_id': 'term-1',
        'external_agent_id': 'agent-1',
        'output_seq': 17,
        'state': {'mode': 'observe'},
        'render': {
            'render_mode': 'mirror_screen',
            'render_type': 'terminal_screen',
            'mime_type': 'application/vnd.standterm.screen+json',
            'source': 'headless_screen',
            'line_count': 2,
            'byte_length': 24,
            'cols': 80,
            'rows': 24,
            'output_seq': 17,
            'lines': ['one', 'two'],
        },
    }
    assert calls == [
        ('render', record, 'term-1'),
        ('summary', {'active_screen': {'lines': ['one', 'two']}, 'sid': 'sid-1'}),
        (
            'audit',
            state,
            'render_audit',
            {
                'external_agent_id': 'agent-1',
                'status': 'ok',
                'requested_render_mode': 'mirror_screen',
                'render_mode': 'mirror_screen',
                'render_type': 'terminal_screen',
                'mime_type': 'application/vnd.standterm.screen+json',
                'source': 'headless_screen',
                'line_count': 2,
                'byte_length': 24,
                'cols': 80,
                'rows': 24,
                'output_seq': 17,
                'context': {'line_count': 2},
            },
        ),
    ]


def make_tail_handler(
    *,
    build_tail_waiting=None,
    should_strip_ansi=None,
    format_tail=None,
):
    calls = []

    if build_tail_waiting is None:
        def build_tail_waiting(bridge, state, since_output_seq=None, limit=None, wait_ms=None):
            calls.append(('tail', bridge, state, since_output_seq, limit, wait_ms))
            return {
                'output_seq': 10,
                'since_output_seq': 7,
                'limit': limit,
                'first_available_output_seq': 3,
                'dropped_before_output_seq': None,
                'gap': False,
                'events': [{'seq': 8, 'text': 'hello'}],
            }, None

    if should_strip_ansi is None:
        def should_strip_ansi(command):
            calls.append(('strip', command))
            return False

    if format_tail is None:
        def format_tail(tail, strip_ansi=False):
            calls.append(('format', tail, strip_ansi))
            return dict(tail)

    def parse_tail_wait_ms(value):
        calls.append(('wait_ms', value))
        return 250 if value == 'fast' else value

    def record_audit(state, event_type, **metadata):
        calls.append(('audit', state, event_type, metadata))

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    handler = ExternalAgentTailCommandHandler(
        build_tail_waiting=build_tail_waiting,
        format_tail=format_tail,
        should_strip_ansi=should_strip_ansi,
        parse_tail_wait_ms=parse_tail_wait_ms,
        record_audit=record_audit,
        build_error=build_error,
        audit_event_type='tail_audit',
        default_limit=99,
    )
    return handler, calls


def test_tail_handler_maps_tail_builder_error_without_format_or_audit():
    calls = []

    def build_tail_waiting(bridge, state, since_output_seq=None, limit=None, wait_ms=None):
        calls.append(('tail', bridge, state, since_output_seq, limit, wait_ms))
        return None, 'bad_tail'

    handler, handler_calls = make_tail_handler(build_tail_waiting=build_tail_waiting)

    assert handler.process_tail_command(
        'tail',
        {'op': 'tail', 'since_output_seq': 4, 'wait_ms': 'fast'},
        {'external_agent_id': 'agent-1'},
        {'state': True},
        'term-1',
        {'bridge': True},
    ) == {
        'status': 'failed',
        'error_code': 'bad_tail',
        'terminal_id': 'term-1',
    }
    assert calls == [
        ('tail', {'bridge': True}, {'state': True}, 4, 99, 'fast'),
    ]
    assert handler_calls == [
        ('error', 'bad_tail', 'term-1'),
    ]


def test_tail_handler_builds_payload_and_records_audit():
    handler, calls = make_tail_handler()
    command = {'op': 'tail', 'since_output_seq': 7, 'limit': 5, 'wait_ms': 'fast'}

    assert handler.process_tail_command(
        'tail',
        command,
        {'external_agent_id': 'agent-1'},
        {'state': True},
        'term-1',
        {'bridge': True},
    ) == {
        'status': 'ok',
        'terminal_id': 'term-1',
        'external_agent_id': 'agent-1',
        'output_seq': 10,
        'since_output_seq': 7,
        'limit': 5,
        'wait_ms': 250,
        'first_available_output_seq': 3,
        'dropped_before_output_seq': None,
        'gap': False,
        'events': [{'seq': 8, 'text': 'hello'}],
    }
    assert calls == [
        ('tail', {'bridge': True}, {'state': True}, 7, 5, 'fast'),
        ('strip', command),
        (
            'format',
            {
                'output_seq': 10,
                'since_output_seq': 7,
                'limit': 5,
                'first_available_output_seq': 3,
                'dropped_before_output_seq': None,
                'gap': False,
                'events': [{'seq': 8, 'text': 'hello'}],
            },
            False,
        ),
        ('wait_ms', 'fast'),
        (
            'audit',
            {'state': True},
            'tail_audit',
            {
                'external_agent_id': 'agent-1',
                'event_count': 1,
                'output_seq': 10,
                'wait_ms': 250,
                'gap': False,
                'strip_ansi': False,
            },
        ),
    ]


def test_tail_handler_reports_plain_tail_format_when_requested():
    def should_strip_ansi(_command):
        return True

    def format_tail(tail, strip_ansi=False):
        tail = dict(tail)
        tail['events'] = [{'seq': 8, 'text': 'plain'}]
        if strip_ansi:
            tail['data_format'] = 'plain'
        return tail

    handler, _calls = make_tail_handler(
        should_strip_ansi=should_strip_ansi,
        format_tail=format_tail,
    )

    payload = handler.process_tail_command(
        'tail',
        {'op': 'tail'},
        {'external_agent_id': 'agent-1'},
        {'state': True},
        'term-1',
        {'bridge': True},
    )

    assert payload['strip_ansi'] is True
    assert payload['data_format'] == 'plain'
    assert payload['events'] == [{'seq': 8, 'text': 'plain'}]


def make_wait_handler(*, build_wait_payload=None):
    calls = []

    if build_wait_payload is None:
        def build_wait_payload(bridge, state, command):
            calls.append(('wait', bridge, state, command))
            return {
                'condition': 'output',
                'status': 'settled',
                'timed_out': False,
                'output_seq': 12,
                'wait_ms': 250,
                'quiet_ms': None,
                'event_count': 2,
                'gap': False,
            }, None

    def record_audit(state, event_type, **metadata):
        calls.append(('audit', state, event_type, metadata))

    def build_error(error_code, terminal_id=None):
        calls.append(('error', error_code, terminal_id))
        return {'status': 'failed', 'error_code': error_code, 'terminal_id': terminal_id}

    handler = ExternalAgentWaitCommandHandler(
        build_wait_payload=build_wait_payload,
        record_audit=record_audit,
        build_error=build_error,
        audit_event_type='wait_audit',
    )
    return handler, calls


def test_wait_handler_maps_wait_builder_error_without_audit():
    calls = []

    def build_wait_payload(bridge, state, command):
        calls.append(('wait', bridge, state, command))
        return None, 'bad_wait'

    handler, handler_calls = make_wait_handler(build_wait_payload=build_wait_payload)
    state = PublicState({'mode': 'observe'})
    command = {'op': 'wait', 'condition': 'output'}

    assert handler.process_wait_command(
        'wait',
        command,
        {'external_agent_id': 'agent-1'},
        state,
        'term-1',
        {'bridge': True},
    ) == {
        'status': 'failed',
        'error_code': 'bad_wait',
        'terminal_id': 'term-1',
    }
    assert calls == [
        ('wait', {'bridge': True}, state, command),
    ]
    assert handler_calls == [
        ('error', 'bad_wait', 'term-1'),
    ]


def test_wait_handler_builds_payload_and_records_audit():
    handler, calls = make_wait_handler()
    state = PublicState({'mode': 'observe'})
    command = {'op': 'wait', 'condition': 'output'}

    assert handler.process_wait_command(
        'wait',
        command,
        {'external_agent_id': 'agent-1'},
        state,
        'term-1',
        {'bridge': True},
    ) == {
        'status': 'ok',
        'terminal_id': 'term-1',
        'external_agent_id': 'agent-1',
        'output_seq': 12,
        'state': {'mode': 'observe'},
        'wait': {
            'condition': 'output',
            'status': 'settled',
            'timed_out': False,
            'output_seq': 12,
            'wait_ms': 250,
            'quiet_ms': None,
            'event_count': 2,
            'gap': False,
        },
    }
    assert calls == [
        ('wait', {'bridge': True}, state, command),
        (
            'audit',
            state,
            'wait_audit',
            {
                'external_agent_id': 'agent-1',
                'condition': 'output',
                'status': 'settled',
                'timed_out': False,
                'output_seq': 12,
                'wait_ms': 250,
                'quiet_ms': None,
                'event_count': 2,
                'gap': False,
            },
        ),
    ]


def test_app_command_registry_keeps_command_specific_auth_handlers():
    import app as standterm

    assert standterm.EXTERNAL_AGENT_COMMAND_AUTH_HANDLERS == {
        'hello': standterm.external_agent_basic_command_handlers.process_hello_command,
        'attach': standterm.external_agent_lifecycle_command_handlers.process_attach_command,
        'revoke': standterm.external_agent_lifecycle_command_handlers.process_revoke_command,
        'heartbeat': standterm.external_agent_lifecycle_command_handlers.process_heartbeat_command,
    }
    assert (
        standterm.EXTERNAL_AGENT_AUTHENTICATED_COMMAND_HANDLERS['state']
        == standterm.external_agent_basic_command_handlers.process_state_command
    )
    for op in ('screen', 'render', 'tail', 'wait'):
        assert (
            standterm.EXTERNAL_AGENT_AUTHENTICATED_COMMAND_HANDLERS[op]
            == standterm.external_agent_read_command_router.process_read_command
        )
    assert (
        standterm.EXTERNAL_AGENT_READ_COMMAND_HANDLERS['screen']
        == standterm.external_agent_screen_command_handler.process_screen_command
    )
    assert isinstance(
        standterm.external_agent_mirror_screen_render_handler,
        ExternalAgentMirrorScreenRenderHandler,
    )
    assert (
        standterm.EXTERNAL_AGENT_READ_COMMAND_HANDLERS['render']
        == standterm.process_external_agent_render_command
    )
    assert (
        standterm.EXTERNAL_AGENT_READ_COMMAND_HANDLERS['tail']
        == standterm.external_agent_tail_command_handler.process_tail_command
    )
    assert (
        standterm.EXTERNAL_AGENT_READ_COMMAND_HANDLERS['wait']
        == standterm.external_agent_wait_command_handler.process_wait_command
    )


def main():
    tests = [
        test_dispatcher_rejects_invalid_op_before_validation,
        test_dispatcher_command_auth_handler_bypasses_shared_validation,
        test_dispatcher_authenticated_handler_receives_validated_state,
        test_dispatcher_unknown_op_reports_action_not_allowed_after_validation,
        test_basic_handlers_hello_uses_command_specific_validation,
        test_basic_handlers_hello_maps_validation_error,
        test_basic_handlers_state_returns_state_payload,
        test_lifecycle_handlers_heartbeat_renews_without_token_validation_side_effect,
        test_lifecycle_handlers_heartbeat_maps_validation_error_without_renew,
        test_lifecycle_handlers_attach_marks_record_and_audits,
        test_lifecycle_handlers_attach_maps_store_error_without_audit,
        test_lifecycle_handlers_revoke_marks_record_and_audits,
        test_lifecycle_handlers_revoke_maps_store_error_without_audit,
        test_read_router_rejects_privacy_block_before_bridge_lookup,
        test_read_router_maps_missing_bridge,
        test_read_router_maps_unknown_read_op_after_bridge_lookup,
        test_read_router_delegates_to_concrete_handler_with_bridge,
        test_screen_handler_maps_parse_error_without_wait_or_audit,
        test_screen_handler_maps_wait_error_without_context_or_audit,
        test_screen_handler_builds_payload_and_records_audit,
        test_screen_handler_omits_empty_screen_wait_payload,
        test_mirror_screen_render_handler_builds_payload_and_records_audit,
        test_tail_handler_maps_tail_builder_error_without_format_or_audit,
        test_tail_handler_builds_payload_and_records_audit,
        test_tail_handler_reports_plain_tail_format_when_requested,
        test_wait_handler_maps_wait_builder_error_without_audit,
        test_wait_handler_builds_payload_and_records_audit,
        test_app_command_registry_keeps_command_specific_auth_handlers,
    ]
    for test in tests:
        test()
        print(f'{test.__name__}: ok')


if __name__ == '__main__':
    main()
