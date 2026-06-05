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


class ExternalAgentScreenCommandHandler:
    def __init__(
        self,
        *,
        parse_screen_options,
        build_screen_wait,
        build_context,
        apply_screen_options,
        summarize_context,
        record_audit,
        build_error,
        audit_event_type,
    ):
        self.parse_screen_options = parse_screen_options
        self.build_screen_wait = build_screen_wait
        self.build_context = build_context
        self.apply_screen_options = apply_screen_options
        self.summarize_context = summarize_context
        self.record_audit = record_audit
        self.build_error = build_error
        self.audit_event_type = audit_event_type

    def process_screen_command(self, _op, command, record, state, terminal_id, bridge):
        screen_options, screen_options_error = self.parse_screen_options(command)
        if screen_options_error:
            return self.build_error(screen_options_error, terminal_id=terminal_id)
        screen_wait, screen_wait_error = self.build_screen_wait(
            bridge,
            state,
            wait_ms=command.get('wait_ms'),
            quiet_ms=command.get('quiet_ms'),
        )
        if screen_wait_error:
            return self.build_error(screen_wait_error, terminal_id=terminal_id)
        context = self.build_context(
            record.get('session_token'),
            terminal_id,
            record.get('sid'),
        )
        active_screen = self.apply_screen_options(
            context.get('active_screen'),
            screen_options,
        )
        self.record_audit(
            state,
            self.audit_event_type,
            external_agent_id=record.get('external_agent_id'),
            context=self.summarize_context(context),
            screen_options=screen_options,
            screen_wait=screen_wait,
        )
        payload = {
            'status': 'ok',
            'terminal_id': terminal_id,
            'external_agent_id': record.get('external_agent_id'),
            'output_seq': bridge.output_seq,
            'state': state.public_state(),
            'screen': active_screen,
        }
        if screen_wait and (screen_wait.get('wait_ms') or screen_wait.get('quiet_ms')):
            payload['screen_wait'] = screen_wait
        return payload


class ExternalAgentMirrorScreenRenderHandler:
    def __init__(
        self,
        *,
        build_render_payload,
        summarize_context,
        record_audit,
        audit_event_type,
    ):
        self.build_render_payload = build_render_payload
        self.summarize_context = summarize_context
        self.record_audit = record_audit
        self.audit_event_type = audit_event_type

    def process_mirror_screen_render_command(
        self,
        _op,
        _command,
        record,
        state,
        terminal_id,
        bridge,
        *,
        requested_render_mode,
        render_mode,
    ):
        render, context = self.build_render_payload(record, terminal_id)
        self.record_audit(
            state,
            self.audit_event_type,
            external_agent_id=record.get('external_agent_id'),
            status='ok',
            requested_render_mode=requested_render_mode,
            render_mode=render_mode,
            render_type=render.get('render_type'),
            mime_type=render.get('mime_type'),
            source=render.get('source'),
            line_count=render.get('line_count'),
            byte_length=render.get('byte_length'),
            cols=render.get('cols'),
            rows=render.get('rows'),
            output_seq=render.get('output_seq', bridge.output_seq),
            context=self.summarize_context(context),
        )
        return {
            'status': 'ok',
            'terminal_id': terminal_id,
            'external_agent_id': record.get('external_agent_id'),
            'output_seq': render.get('output_seq', bridge.output_seq),
            'state': state.public_state(),
            'render': render,
        }


class ExternalAgentRenderCommandHandler:
    def __init__(
        self,
        *,
        parse_render_mode,
        resolve_render_mode,
        mirror_screen_render_handler,
        process_viewport_render,
        build_error,
        invalid_data_error_code,
        mirror_screen_render_mode,
    ):
        self.parse_render_mode = parse_render_mode
        self.resolve_render_mode = resolve_render_mode
        self.mirror_screen_render_handler = mirror_screen_render_handler
        self.process_viewport_render = process_viewport_render
        self.build_error = build_error
        self.invalid_data_error_code = invalid_data_error_code
        self.mirror_screen_render_mode = mirror_screen_render_mode

    def process_render_command(self, op, command, record, state, terminal_id, bridge):
        requested_render_mode = self.parse_render_mode(command.get('render_mode'))
        if requested_render_mode is None:
            return self.build_error(self.invalid_data_error_code, terminal_id=terminal_id)
        render_mode = self.resolve_render_mode(requested_render_mode)
        if render_mode == self.mirror_screen_render_mode:
            return self.mirror_screen_render_handler.process_mirror_screen_render_command(
                op,
                command,
                record,
                state,
                terminal_id,
                bridge,
                requested_render_mode=requested_render_mode,
                render_mode=render_mode,
            )
        return self.process_viewport_render(
            op,
            command,
            record,
            state,
            terminal_id,
            bridge,
            requested_render_mode=requested_render_mode,
            render_mode=render_mode,
        )


class ExternalAgentSendActionExecutor:
    def __init__(
        self,
        *,
        agent_lock,
        is_context_allowed,
        is_human_input_lease_active,
        build_terminal_input_action,
        should_submit_after,
        build_action,
        record_audit,
        emit_action_request,
        emit_state,
        write_terminal_input,
        emit_action_result,
        get_agent_state,
        audit_event_type,
        status_pending_approval,
        status_completed,
        status_failed,
        mode_paused,
        mode_approval_pending,
        mode_direct_active,
        error_paused,
        error_privacy_blocked,
        error_human_input_active,
        error_mode_not_writable,
    ):
        self.agent_lock = agent_lock
        self.is_context_allowed = is_context_allowed
        self.is_human_input_lease_active = is_human_input_lease_active
        self.build_terminal_input_action = build_terminal_input_action
        self.should_submit_after = should_submit_after
        self.build_action = build_action
        self.record_audit = record_audit
        self.emit_action_request = emit_action_request
        self.emit_state = emit_state
        self.write_terminal_input = write_terminal_input
        self.emit_action_result = emit_action_result
        self.get_agent_state = get_agent_state
        self.audit_event_type = audit_event_type
        self.status_pending_approval = status_pending_approval
        self.status_completed = status_completed
        self.status_failed = status_failed
        self.mode_paused = mode_paused
        self.mode_approval_pending = mode_approval_pending
        self.mode_direct_active = mode_direct_active
        self.error_paused = error_paused
        self.error_privacy_blocked = error_privacy_blocked
        self.error_human_input_active = error_human_input_active
        self.error_mode_not_writable = error_mode_not_writable

    def execute_send_action(
        self,
        command,
        record,
        state,
        terminal_id,
        bridge,
        data,
        input_metadata,
        *,
        capture_requested,
        strip_ansi,
    ):
        before_output_seq = None
        with bridge.input_lock:
            with self.agent_lock:
                if state.paused or state.mode == self.mode_paused:
                    return None, self.error_paused
                if not self.is_context_allowed(state):
                    return None, self.error_privacy_blocked
                if self.is_human_input_lease_active(state):
                    return None, self.error_human_input_active
                if state.mode not in {self.mode_approval_pending, self.mode_direct_active}:
                    return None, self.error_mode_not_writable
                proposal = self.build_terminal_input_action(
                    state,
                    data,
                    submit_after=self.should_submit_after(command),
                    input_metadata=input_metadata,
                )
                requires_approval = state.mode != self.mode_direct_active
                action, error_code = self.build_action(state, proposal, requires_approval)
                if error_code:
                    return None, error_code
                self.record_audit(
                    state,
                    self.audit_event_type,
                    action=action,
                    external_agent_id=record.get('external_agent_id'),
                    input_kind=input_metadata.get('input_kind') if input_metadata else None,
                    key_count=input_metadata.get('key_count') if input_metadata else None,
                    capture_requested=capture_requested,
                    strip_ansi=strip_ansi if capture_requested else False,
                )
                self.emit_action_request(state.sid, action)
                self.emit_state(state.sid, state)
            if requires_approval:
                return {
                    'status': self.status_pending_approval,
                    'requires_approval': True,
                    'action': action,
                    'before_output_seq': None,
                    'write_result': None,
                }, None
            with bridge.output_condition:
                before_output_seq = bridge.output_seq
            ok, result = self.write_terminal_input(
                record.get('session_token'),
                terminal_id,
                state.sid,
                action['action_id'],
                action['control_epoch'],
                mode_version=action['mode_version'],
                proposal_id=action['proposal_id'],
            )
        status = self.status_completed if ok else self.status_failed
        self.emit_action_result(
            state.sid,
            action,
            status,
            error_code=result.get('error_code'),
        )
        with self.agent_lock:
            current_state = self.get_agent_state(
                record.get('session_token'),
                terminal_id,
                state.sid,
            )
            if current_state:
                self.emit_state(state.sid, current_state)
        return {
            'status': status,
            'requires_approval': False,
            'action': action,
            'before_output_seq': before_output_seq,
            'write_result': result,
        }, None


class ExternalAgentSendResponseBuilder:
    def __init__(
        self,
        *,
        public_action,
        status_pending_approval,
        status_completed,
        status_failed,
    ):
        self.public_action = public_action
        self.status_pending_approval = status_pending_approval
        self.status_completed = status_completed
        self.status_failed = status_failed

    def build_pending_payload(self, action, *, capture_requested):
        payload = self.public_action(action)
        payload['status'] = self.status_pending_approval
        if capture_requested:
            payload['capture'] = {
                'status': 'skipped',
                'reason': 'pending_approval',
                'requested': True,
            }
        return payload

    def build_write_payload(self, send_result):
        action = send_result['action']
        write_result = send_result.get('write_result') or {}
        payload = self.public_action(action)
        payload['status'] = send_result['status']
        if write_result.get('error_code'):
            payload['error_code'] = write_result.get('error_code')
        payload['bytes_written'] = write_result.get('bytes_written', 0)
        return payload

    def should_capture_after_write(self, send_result, *, capture_requested):
        return capture_requested and send_result.get('status') == self.status_completed

    def build_failed_capture_payload(self, error_code):
        return {
            'status': self.status_failed,
            'error_code': error_code,
            'requested': True,
        }

    def attach_capture_payload(self, payload, capture):
        capture['requested'] = True
        payload['capture'] = capture
        payload['before_output_seq'] = capture['before_output_seq']
        payload['after_output_seq'] = capture['output_seq']
        return payload


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
            'available_event_count': tail.get('available_event_count'),
            'returned_event_count': tail.get('returned_event_count', len(tail['events'])),
            'last_returned_output_seq': tail.get('last_returned_output_seq'),
            'next_since_output_seq': tail.get('next_since_output_seq'),
            'more_available': bool(tail.get('more_available')),
            'first_available_output_seq': tail['first_available_output_seq'],
            'dropped_before_output_seq': tail['dropped_before_output_seq'],
            'gap': tail['gap'],
            'events': tail['events'],
        }
        if strip_ansi:
            payload['strip_ansi'] = True
            payload['data_format'] = tail['data_format']
        return payload


class ExternalAgentWaitCommandHandler:
    def __init__(
        self,
        *,
        build_wait_payload,
        record_audit,
        build_error,
        audit_event_type,
    ):
        self.build_wait_payload = build_wait_payload
        self.record_audit = record_audit
        self.build_error = build_error
        self.audit_event_type = audit_event_type

    def process_wait_command(self, _op, command, record, state, terminal_id, bridge):
        wait_payload, error_code = self.build_wait_payload(bridge, state, command)
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)
        self.record_audit(
            state,
            self.audit_event_type,
            external_agent_id=record.get('external_agent_id'),
            condition=wait_payload['condition'],
            status=wait_payload['status'],
            timed_out=wait_payload['timed_out'],
            output_seq=wait_payload.get('output_seq'),
            wait_ms=wait_payload.get('wait_ms'),
            quiet_ms=wait_payload.get('quiet_ms'),
            event_count=wait_payload.get('event_count'),
            gap=wait_payload.get('gap'),
        )
        return {
            'status': 'ok',
            'terminal_id': terminal_id,
            'external_agent_id': record.get('external_agent_id'),
            'output_seq': wait_payload.get('output_seq'),
            'state': state.public_state(),
            'wait': wait_payload,
        }
