import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external_agent_dispatch import ExternalAgentCommandDispatcher
from external_agent_handlers import (
    ExternalAgentBasicCommandHandlers,
    ExternalAgentLifecycleCommandHandlers,
    ExternalAgentReadCommandRouter,
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
        test_app_command_registry_keeps_command_specific_auth_handlers,
    ]
    for test in tests:
        test()
        print(f'{test.__name__}: ok')


if __name__ == '__main__':
    main()
