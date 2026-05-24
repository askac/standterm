from .base import TerminalBackendPlugin


class LocalShellBackendPlugin(TerminalBackendPlugin):
    connection_type = 'local_shell'
    label = 'Local Shell'

    def __init__(
        self,
        *,
        bridge_cls,
        get_request_client_ip,
        is_allowed_for_client,
        get_local_shell_config,
        is_wsl,
        get_wsl_local_shell_options,
        default_shell_kind,
    ):
        self._bridge_cls = bridge_cls
        self._get_request_client_ip = get_request_client_ip
        self._is_allowed_for_client = is_allowed_for_client
        self._get_local_shell_config = get_local_shell_config
        self._is_wsl = is_wsl
        self._get_wsl_local_shell_options = get_wsl_local_shell_options
        self._default_shell_kind = default_shell_kind

    def build_policy_option(self, browser_authorized=False):
        client_ip = self._get_request_client_ip()
        allowed = self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        option = {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
        }
        if self._is_wsl():
            option['shell_options'] = self._get_wsl_local_shell_options()
            option['default_shell_kind'] = self._default_shell_kind
        return option

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        if not self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'Local Shell is not available for this client.',
                'error_code': 'local_shell_unavailable_for_client',
            }
        shell_config, shell_error = self._get_local_shell_config(data.get('local_shell_kind'))
        if shell_error:
            return None, shell_error
        return {'local_shell_config': shell_config}, None

    def create_bridge(self, session_token, terminal_id, payload):
        return self._bridge_cls(session_token, terminal_id, shell_config=payload.get('local_shell_config'))

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols=cols, rows=rows)
