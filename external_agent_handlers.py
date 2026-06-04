from external_agent_protocol import (
    EXTERNAL_AGENT_CAPABILITIES,
    EXTERNAL_AGENT_PROTOCOL_VERSION,
)


class ExternalAgentBasicCommandHandlers:
    def __init__(
        self,
        *,
        validate_token,
        build_error,
        build_state_payload,
    ):
        self.validate_token = validate_token
        self.build_error = build_error
        self.build_state_payload = build_state_payload

    def process_hello_command(self, _op, command):
        record, state, terminal_id, error_code = self.validate_token(
            command,
            require_terminal=False,
        )
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        state_payload = self.build_state_payload(record, state)
        state_payload.pop('status', None)
        return {
            'status': 'ok',
            'version': EXTERNAL_AGENT_PROTOCOL_VERSION,
            'external_agent_id': record.get('external_agent_id'),
            'terminal_id': record.get('terminal_id'),
            'capabilities': list(EXTERNAL_AGENT_CAPABILITIES),
            'state': state_payload,
        }

    def process_state_command(self, _op, _command, record, state, _terminal_id):
        return self.build_state_payload(record, state)


class ExternalAgentLifecycleCommandHandlers:
    def __init__(
        self,
        *,
        validate_token,
        build_error,
        renew_record,
        build_heartbeat_payload,
        attach_record,
        record_attached,
        build_state_payload,
        revoke_record,
        record_revoked,
    ):
        self.validate_token = validate_token
        self.build_error = build_error
        self.renew_record = renew_record
        self.build_heartbeat_payload = build_heartbeat_payload
        self.attach_record = attach_record
        self.record_attached = record_attached
        self.build_state_payload = build_state_payload
        self.revoke_record = revoke_record
        self.record_revoked = record_revoked

    def process_attach_command(self, _op, command):
        _record, state, terminal_id, error_code = self.validate_token(command)
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        record, error_code = self.attach_record(command.get('token'))
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        self.record_attached(state, record)
        return self.build_state_payload(record, state)

    def process_revoke_command(self, _op, command):
        _record, state, terminal_id, error_code = self.validate_token(command)
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        record, error_code = self.revoke_record(command.get('token'))
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        self.record_revoked(state, record)
        return {
            'status': 'ok',
            'terminal_id': terminal_id,
            'external_agent_id': record.get('external_agent_id'),
            'revoked': True,
        }

    def process_heartbeat_command(self, _op, command):
        record, _state, terminal_id, error_code = self.validate_token(
            command,
            renew_token=False,
        )
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        record = self.renew_record(record)
        return self.build_heartbeat_payload(record, terminal_id)


class ExternalAgentReadCommandRouter:
    def __init__(
        self,
        *,
        read_handlers,
        build_error,
        is_context_allowed,
        get_bridge,
        privacy_blocked_error_code,
        terminal_not_found_error_code,
        action_not_allowed_error_code,
    ):
        self.read_handlers = dict(read_handlers)
        self.build_error = build_error
        self.is_context_allowed = is_context_allowed
        self.get_bridge = get_bridge
        self.privacy_blocked_error_code = privacy_blocked_error_code
        self.terminal_not_found_error_code = terminal_not_found_error_code
        self.action_not_allowed_error_code = action_not_allowed_error_code

    def process_read_command(self, op, command, record, state, terminal_id):
        if not self.is_context_allowed(state):
            return self.build_error(
                self.privacy_blocked_error_code,
                terminal_id=terminal_id,
            )
        bridge = self.get_bridge(record.get('session_token'), terminal_id)
        if not bridge:
            return self.build_error(
                self.terminal_not_found_error_code,
                terminal_id=terminal_id,
            )
        handler = self.read_handlers.get(op)
        if not handler:
            return self.build_error(
                self.action_not_allowed_error_code,
                terminal_id=terminal_id,
            )
        return handler(op, command, record, state, terminal_id, bridge)


class ExternalAgentTailCommandHandler:
    def __init__(
        self,
        *,
        build_tail_waiting,
        format_tail,
        should_strip_ansi,
        parse_tail_wait_ms,
        record_audit,
        build_error,
        audit_event_type,
        default_limit,
    ):
        self.build_tail_waiting = build_tail_waiting
        self.format_tail = format_tail
        self.should_strip_ansi = should_strip_ansi
        self.parse_tail_wait_ms = parse_tail_wait_ms
        self.record_audit = record_audit
        self.build_error = build_error
        self.audit_event_type = audit_event_type
        self.default_limit = default_limit

    def process_tail_command(self, _op, command, record, state, terminal_id, bridge):
        tail, error_code = self.build_tail_waiting(
            bridge,
            state,
            since_output_seq=command.get('since_output_seq'),
            limit=command.get('limit', self.default_limit),
            wait_ms=command.get('wait_ms'),
        )
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        strip_ansi = self.should_strip_ansi(command)
        tail = self.format_tail(tail, strip_ansi=strip_ansi)
        wait_ms = self.parse_tail_wait_ms(command.get('wait_ms'))
        self.record_audit(
            state,
            self.audit_event_type,
            external_agent_id=record.get('external_agent_id'),
            event_count=len(tail['events']),
            output_seq=tail['output_seq'],
            wait_ms=wait_ms,
            gap=tail['gap'],
            strip_ansi=strip_ansi,
        )
        payload = {
            'status': 'ok',
            'terminal_id': terminal_id,
            'external_agent_id': record.get('external_agent_id'),
            'output_seq': tail['output_seq'],
            'since_output_seq': tail['since_output_seq'],
            'limit': tail['limit'],
            'wait_ms': wait_ms,
            'first_available_output_seq': tail['first_available_output_seq'],
            'dropped_before_output_seq': tail['dropped_before_output_seq'],
            'gap': tail['gap'],
            'events': tail['events'],
        }
        if strip_ansi:
            payload['strip_ansi'] = True
            payload['data_format'] = tail['data_format']
        return payload
