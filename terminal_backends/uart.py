from .base import TerminalBackendPlugin


class UARTBackendPlugin(TerminalBackendPlugin):
    connection_type = 'uart'
    label = 'UART'

    def __init__(
        self,
        *,
        bridge_cls,
        get_request_client_ip,
        is_allowed_for_client,
        detect_serial_ports,
        get_detected_serial_port,
        default_baud_rate,
        min_baud_rate,
        max_baud_rate,
        baud_rates,
    ):
        self._bridge_cls = bridge_cls
        self._get_request_client_ip = get_request_client_ip
        self._is_allowed_for_client = is_allowed_for_client
        self._detect_serial_ports = detect_serial_ports
        self._get_detected_serial_port = get_detected_serial_port
        self._default_baud_rate = default_baud_rate
        self._min_baud_rate = min_baud_rate
        self._max_baud_rate = max_baud_rate
        self._baud_rates = baud_rates

    def build_policy_option(self, browser_authorized=False):
        client_ip = self._get_request_client_ip()
        allowed = self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        return {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
            'available_ports': self._detect_serial_ports() if allowed else [],
            'default_baud_rate': self._default_baud_rate,
            'baud_rates': self._baud_rates,
        }

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        if not self._is_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'UART is not available for this client.',
                'error_code': 'uart_unavailable_for_client',
            }

        port_info = self._get_detected_serial_port(data.get('serial_port'))
        if not port_info:
            return None, {
                'message': 'Select an available UART port.',
                'error_code': 'uart_port_unavailable',
            }

        try:
            baud_rate = int(data.get('baud_rate', self._default_baud_rate))
        except (TypeError, ValueError):
            return None, {
                'message': 'UART baud rate must be a number.',
                'error_code': 'uart_invalid_baud_rate',
            }
        if baud_rate < self._min_baud_rate or baud_rate > self._max_baud_rate:
            return None, {
                'message': 'UART baud rate is outside the supported range.',
                'error_code': 'uart_invalid_baud_rate',
            }

        return {
            'serial_port': port_info['device'],
            'serial_port_info': port_info,
            'baud_rate': baud_rate,
        }, None

    def create_bridge(self, session_token, terminal_id, payload):
        return self._bridge_cls(
            session_token,
            terminal_id,
            payload['serial_port_info'],
            payload['baud_rate'],
        )

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols=cols, rows=rows)
