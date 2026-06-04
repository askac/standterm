class ExternalAgentCommandDispatcher:
    def __init__(
        self,
        *,
        command_auth_handlers,
        authenticated_handlers,
        validate_token,
        build_error,
        invalid_data_error_code,
        action_not_allowed_error_code,
    ):
        self.command_auth_handlers = dict(command_auth_handlers)
        self.authenticated_handlers = dict(authenticated_handlers)
        self.validate_token = validate_token
        self.build_error = build_error
        self.invalid_data_error_code = invalid_data_error_code
        self.action_not_allowed_error_code = action_not_allowed_error_code

    def normalize_op(self, command):
        if not isinstance(command, dict):
            return None, self.invalid_data_error_code
        op = command.get('op')
        if not isinstance(op, str):
            return None, self.invalid_data_error_code
        op = op.strip().lower()
        if not op:
            return None, self.invalid_data_error_code
        return op, None

    def dispatch(self, command):
        op, error_code = self.normalize_op(command)
        if error_code:
            return self.build_error(error_code)

        command_auth_handler = self.command_auth_handlers.get(op)
        if command_auth_handler:
            return command_auth_handler(op, command)

        record, state, terminal_id, error_code = self.validate_token(command)
        if error_code:
            return self.build_error(error_code, terminal_id=terminal_id)

        handler = self.authenticated_handlers.get(op)
        if handler:
            return handler(op, command, record, state, terminal_id)

        return self.build_error(
            self.action_not_allowed_error_code,
            terminal_id=terminal_id,
        )
