class TerminalBackendPlugin:
    connection_type = None
    label = None

    def build_policy_option(self, browser_authorized=False):
        return {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': True,
        }

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        raise NotImplementedError

    def create_bridge(self, session_token, terminal_id, payload):
        raise NotImplementedError

    def connect_bridge(self, bridge, payload, cols, rows):
        raise NotImplementedError

    def build_connection_failure(self, sid, bridge, payload, result):
        message = 'Connection failed.'
        error_code = None
        if isinstance(result, dict):
            message = result.get('message', message)
            error_code = result.get('error_code')
        elif result:
            message = str(result)
        return {
            'message': message,
            'error_code': error_code,
            'action_type': None,
            'action_message': None,
            'action_question': None,
            'action_id': None,
        }


class TerminalBackendRegistry:
    def __init__(self, plugins, normalize_connection_type, default_preference=()):
        self._plugins = {}
        self._default_preference = tuple(default_preference)
        for plugin in plugins:
            connection_type = getattr(plugin, 'connection_type', None)
            label = getattr(plugin, 'label', None)
            if not isinstance(connection_type, str) or normalize_connection_type(connection_type) != connection_type:
                raise ValueError(f'Invalid terminal backend connection type: {connection_type!r}')
            if connection_type in self._plugins:
                raise ValueError(f'Duplicate terminal backend connection type: {connection_type}')
            if not isinstance(label, str) or not label:
                raise ValueError(f'Invalid terminal backend label for {connection_type}')
            self._plugins[connection_type] = plugin

        for connection_type in self._default_preference:
            if connection_type not in self._plugins:
                raise ValueError(f'Default terminal backend is not registered: {connection_type}')

    def get(self, connection_type):
        return self._plugins.get(connection_type)

    def build_policy_options(self, browser_authorized=False):
        options = []
        for plugin in self._plugins.values():
            option = plugin.build_policy_option(browser_authorized=browser_authorized)
            if not isinstance(option, dict):
                raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid policy option.')
            if option.get('connection_type') != plugin.connection_type:
                raise ValueError(f'Terminal backend {plugin.connection_type} returned mismatched policy option.')
            if not isinstance(option.get('allowed'), bool):
                raise ValueError(f'Terminal backend {plugin.connection_type} returned non-bool allowed flag.')
            options.append(option)
        return options

    def get_default_connection(self, allowed_connections):
        for connection_type in self._default_preference:
            if allowed_connections.get(connection_type):
                return connection_type
        for connection_type, allowed in allowed_connections.items():
            if allowed:
                return connection_type
        if self._default_preference:
            return self._default_preference[0]
        return next(iter(self._plugins), None)
