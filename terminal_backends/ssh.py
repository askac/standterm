from .base import TerminalBackendPlugin


class SSHBackendPlugin(TerminalBackendPlugin):
    connection_type = 'ssh'
    label = 'SSH'

    def __init__(
        self,
        *,
        bridge_cls,
        default_host,
        default_port,
        default_user,
        max_host_length,
        max_username_length,
        max_password_bytes,
        has_control_chars,
        allowed_action_types,
        backend_action_store,
        key_setup_ttl_seconds,
        token_urlsafe,
        time_func,
    ):
        self._bridge_cls = bridge_cls
        self._default_host = default_host
        self._default_port = default_port
        self._default_user = default_user
        self._max_host_length = max_host_length
        self._max_username_length = max_username_length
        self._max_password_bytes = max_password_bytes
        self._has_control_chars = has_control_chars
        self._allowed_action_types = allowed_action_types
        self._backend_action_store = backend_action_store
        self._key_setup_ttl_seconds = key_setup_ttl_seconds
        self._token_urlsafe = token_urlsafe
        self._time_func = time_func

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        host = data.get('host', self._default_host)
        if not isinstance(host, str):
            return None, 'Host must be a string.'
        host = host.strip()
        if not host or len(host) > self._max_host_length:
            return None, 'Host is empty or too long.'
        if self._has_control_chars(host):
            return None, 'Host contains invalid control characters.'

        try:
            port = int(data.get('port', self._default_port))
        except (TypeError, ValueError):
            return None, 'Port must be a number.'
        if port < 1 or port > 65535:
            return None, 'Port must be between 1 and 65535.'

        user = data.get('username', self._default_user)
        if not isinstance(user, str):
            return None, 'Username must be a string.'
        user = user.strip()
        if not user or len(user) > self._max_username_length:
            return None, 'Username is empty or too long.'
        if self._has_control_chars(user):
            return None, 'Username contains invalid control characters.'

        password = data.get('password') or ''
        if not isinstance(password, str):
            return None, 'Password must be a string.'
        if len(password.encode('utf-8', errors='ignore')) > self._max_password_bytes:
            return None, 'Password is too long.'

        return {
            'host': host,
            'port': port,
            'username': user,
            'password': password,
        }, None

    def create_bridge(self, session_token, terminal_id, payload):
        return self._bridge_cls(session_token, terminal_id)

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(
            payload['host'],
            payload['port'],
            payload['username'],
            payload['password'],
            cols=cols,
            rows=rows,
        )

    def prepare_connection_failure(self, sid, bridge, payload, result):
        failure = super().prepare_connection_failure(sid, bridge, payload, result)
        action_type = None
        action_message = None
        action_question = None

        if isinstance(result, dict):
            action_type = result.get('action_type')
            action_message = result.get('action_message')
            action_question = result.get('action_question')
        if action_type not in self._allowed_action_types:
            action_type = None
            action_message = None
            action_question = None

        action_id = None
        if action_type == 'offer_localhost_key_setup':
            action = bridge.prepare_backend_action(
                action_type,
                payload,
                expires_at=self._time_func() + self._key_setup_ttl_seconds,
                message=action_message,
                question=action_question,
            )
            if action:
                action_id = self._token_urlsafe(16)
                self._backend_action_store.set(sid, action_id, action)
            else:
                action_type = None
                action_message = None
                action_question = None

        failure.update({
            'action_type': action_type,
            'action_message': action_message,
            'action_question': action_question,
            'action_id': action_id,
        })
        return failure

    def execute_backend_action(self, action):
        if action.action_type != 'offer_localhost_key_setup':
            return super().execute_backend_action(action)
        return self._bridge_cls.execute_backend_action(action)
