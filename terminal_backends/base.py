import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class BackendPolicyContext:
    client_ip: str
    browser_authorized: bool = False
    settings_snapshot: Optional[dict] = None


@dataclass(frozen=True)
class BackendAction:
    action_type: str
    terminal_id: str
    metadata: dict
    expires_at: float
    message: Optional[str] = None
    question: Optional[str] = None


@dataclass(frozen=True)
class BackendSettingSchema:
    setting_key: str
    value_type: str
    risk_level: str
    required_capability: str
    label: Optional[str] = None
    default_value: Any = None
    restart_required: bool = False
    secret: bool = False
    redact_in_audit: bool = False
    readonly_when_remote: bool = False
    mutable: bool = False
    storage_owner: str = 'core'
    storage_scope: str = 'runtime'
    apply_scope: str = 'next_connection'
    allowed_values: Optional[tuple] = None
    min_value: Any = None
    max_value: Any = None

    def to_dict(self, plugin_connection_type=None):
        payload = {
            'setting_key': self.setting_key,
            'value_type': self.value_type,
            'risk_level': self.risk_level,
            'required_capability': self.required_capability,
            'restart_required': bool(self.restart_required),
            'secret': bool(self.secret),
            'redact_in_audit': bool(self.redact_in_audit),
            'readonly_when_remote': bool(self.readonly_when_remote),
            'mutable': bool(self.mutable),
            'storage_owner': self.storage_owner,
            'storage_scope': self.storage_scope,
            'apply_scope': self.apply_scope,
        }
        if plugin_connection_type:
            payload['connection_type'] = plugin_connection_type
        if self.label is not None:
            payload['label'] = self.label
        if self.default_value is not None and not self.secret:
            payload['default_value'] = self.default_value
        if self.allowed_values is not None:
            payload['allowed_values'] = list(self.allowed_values)
        if self.min_value is not None:
            payload['min_value'] = self.min_value
        if self.max_value is not None:
            payload['max_value'] = self.max_value
        return payload


class BackendActionStore:
    def __init__(self, time_func=None):
        self._actions_by_sid = {}
        self._time_func = time_func or time.time

    def clear(self):
        self._actions_by_sid.clear()

    def discard(self, sid):
        self._actions_by_sid.pop(sid, None)

    def pop(self, sid, default=None):
        return self._actions_by_sid.pop(sid, default)

    def set(self, sid, action_id, action):
        self._actions_by_sid[sid] = {
            'action_id': action_id,
            'action': action,
        }

    def get(self, sid, action_id=None, compare_digest=None):
        if action_id is None or compare_digest is None:
            return self._actions_by_sid.get(sid)
        pending = self._actions_by_sid.get(sid)
        if not pending:
            return None, 'backend_action_no_pending_action'
        action = pending.get('action')
        if not isinstance(action, BackendAction):
            self._actions_by_sid.pop(sid, None)
            return None, 'backend_action_no_pending_action'
        if self._time_func() > action.expires_at:
            self._actions_by_sid.pop(sid, None)
            return None, 'backend_action_expired'
        if not isinstance(action_id, str) or not compare_digest(action_id, pending.get('action_id', '')):
            return None, 'backend_action_no_pending_action'
        return action, None


@dataclass(frozen=True)
class TerminalBridgeRuntime:
    emit_socket: Any
    build_metadata: Any
    append_transcript: Any
    unregister_bridge: Any
    sleep: Any
    max_replay_events: int
    max_replay_bytes: int
    close_process: Optional[Any] = None


class TerminalBridge:
    connection_type = None
    terminal_kind = None
    terminal_label = None
    _default_runtime = None

    @classmethod
    def set_default_runtime(cls, runtime):
        cls._default_runtime = runtime

    def __init__(self, owner_session, terminal_id, runtime=None):
        self.runtime = runtime or self._default_runtime
        if self.runtime is None:
            raise RuntimeError('TerminalBridge runtime is not configured.')
        self.owner_session = owner_session
        self.terminal_id = terminal_id
        self.attached_sids = set()
        self.sid = None
        self.closing = False
        self.cols = 80
        self.rows = 24
        self.output_seq = 0
        self.last_output_at = None
        self.replay_buffer = deque()
        self.replay_buffer_bytes = 0
        self.input_lock = threading.RLock()
        self.output_condition = threading.Condition(threading.RLock())

    def metadata(self, cols=None, rows=None):
        cols = self.cols if cols is None else cols
        rows = self.rows if rows is None else rows
        return self.runtime.build_metadata(
            self.connection_type,
            self.terminal_id,
            self.terminal_kind,
            self.terminal_label,
            cols,
            rows,
        )

    def session_metadata(self, now=None):
        now = time.time() if now is None else now
        terminal_quiet_ms = None
        if self.last_output_at is not None:
            terminal_quiet_ms = max(0, int((now - self.last_output_at) * 1000))
        return {
            'session_token': self.owner_session,
            'terminal_id': self.terminal_id,
            'connection_type': self.connection_type,
            'terminal_kind': self.terminal_kind,
            'terminal_label': self.terminal_label,
            'cols': self.cols,
            'rows': self.rows,
            'output_seq': self.output_seq,
            'last_output_at': self.last_output_at,
            'terminal_quiet_ms': terminal_quiet_ms,
        }

    def update_terminal_size(self, cols, rows):
        self.cols = cols
        self.rows = rows

    def attach(self, sid):
        self.sid = sid
        self.attached_sids.add(sid)

    def detach(self, sid):
        self.attached_sids.discard(sid)
        if self.sid == sid:
            self.sid = next(iter(self.attached_sids), None)

    def emit_output(self, payload):
        payload = dict(payload)
        payload.setdefault('connection_type', self.connection_type)
        payload.setdefault('terminal_id', self.terminal_id)
        if payload.get('message_type') == 'terminal':
            with self.output_condition:
                self.output_seq += 1
                self.last_output_at = time.time()
                payload.setdefault('output_seq', self.output_seq)
                self._remember_terminal_payload(payload)
                self.runtime.append_transcript(
                    self.owner_session,
                    self.terminal_id,
                    payload.get('data'),
                )
                self.output_condition.notify_all()
        for sid in list(self.attached_sids):
            self.runtime.emit_socket('ssh_output', payload, room=sid)

    def _remember_terminal_payload(self, payload):
        data = payload.get('data')
        if not isinstance(data, str) or not data:
            return
        payload_size = len(data.encode('utf-8', errors='ignore'))
        self.replay_buffer.append(dict(payload))
        self.replay_buffer_bytes += payload_size
        while (
            len(self.replay_buffer) > self.runtime.max_replay_events
            or self.replay_buffer_bytes > self.runtime.max_replay_bytes
        ):
            removed = self.replay_buffer.popleft()
            removed_data = removed.get('data', '')
            self.replay_buffer_bytes -= len(removed_data.encode('utf-8', errors='ignore'))

    def replay_to(self, sid):
        for payload in list(self.replay_buffer):
            self.runtime.emit_socket('ssh_output', payload, room=sid)

    def read_loop(self):
        raise NotImplementedError

    def write(self, data):
        raise NotImplementedError

    def resize(self, cols, rows):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class TerminalBackendPlugin:
    connection_type = None
    label = None

    def build_policy_option(self, context=None, browser_authorized=False):
        return {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': True,
        }

    def get_settings_schema(self):
        return []

    def validate_setting_update(self, setting_key, value, current_value=None):
        return None, {
            'error_code': 'settings_unknown_key',
            'message': 'Setting key is not supported by this backend.',
        }

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False, context=None):
        raise NotImplementedError

    def create_bridge(self, session_token, terminal_id, payload):
        raise NotImplementedError

    def connect_bridge(self, bridge, payload, cols, rows):
        raise NotImplementedError

    def prepare_connection_failure(self, sid, bridge, payload, result):
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

    def build_connection_failure(self, sid, bridge, payload, result):
        return self.prepare_connection_failure(sid, bridge, payload, result)

    def execute_backend_action(self, action):
        return {
            'status': 'failed',
            'message': 'Backend action is not supported.',
            'error_code': 'backend_action_unsupported',
        }


class TerminalBackendRegistry:
    def __init__(
        self,
        plugins,
        normalize_connection_type,
        default_preference=(),
        known_settings_capabilities=(),
        risk_capability_rules=None,
        mutable_setting_keys=(),
    ):
        self._plugins = {}
        self._default_preference = tuple(default_preference)
        self._known_settings_capabilities = set(known_settings_capabilities)
        self._risk_capability_rules = dict(risk_capability_rules or {})
        self._mutable_setting_keys = set(mutable_setting_keys)
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

    def build_policy_options(self, context=None, browser_authorized=False):
        if context is None:
            context = BackendPolicyContext(
                client_ip='unknown',
                browser_authorized=browser_authorized,
            )
        options = []
        for plugin in self._plugins.values():
            option = plugin.build_policy_option(context=context)
            if not isinstance(option, dict):
                raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid policy option.')
            if option.get('connection_type') != plugin.connection_type:
                raise ValueError(f'Terminal backend {plugin.connection_type} returned mismatched policy option.')
            if not isinstance(option.get('allowed'), bool):
                raise ValueError(f'Terminal backend {plugin.connection_type} returned non-bool allowed flag.')
            options.append(option)
        return options

    def build_settings_schema(self):
        schema = []
        seen_keys = set()
        for plugin in self._plugins.values():
            for item in plugin.get_settings_schema():
                payload = self._normalize_settings_schema_item(plugin, item)
                setting_key = payload['setting_key']
                if setting_key in seen_keys:
                    raise ValueError(f'Duplicate backend setting key: {setting_key}')
                seen_keys.add(setting_key)
                schema.append(payload)
        return schema

    def _normalize_settings_schema_item(self, plugin, item):
        if isinstance(item, BackendSettingSchema):
            payload = item.to_dict(plugin_connection_type=plugin.connection_type)
        elif isinstance(item, dict):
            payload = dict(item)
            payload.setdefault('connection_type', plugin.connection_type)
        else:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid settings schema.')

        setting_key = payload.get('setting_key')
        if not isinstance(setting_key, str) or not setting_key.startswith(f'{plugin.connection_type}.'):
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid setting key.')
        if payload.get('connection_type') != plugin.connection_type:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned mismatched settings schema.')
        if payload.get('value_type') not in {'boolean', 'integer', 'number', 'string', 'enum'}:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid setting value type.')
        if payload.get('risk_level') not in {'low', 'medium', 'high'}:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid setting risk level.')
        required_capability = payload.get('required_capability')
        if not isinstance(required_capability, str) or not required_capability:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid setting capability.')
        if self._known_settings_capabilities and required_capability not in self._known_settings_capabilities:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned unknown setting capability.')
        allowed_capabilities = self._risk_capability_rules.get(payload.get('risk_level'))
        if allowed_capabilities and required_capability not in allowed_capabilities:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned mismatched setting risk/capability.')

        for flag in (
            'restart_required',
            'secret',
            'redact_in_audit',
            'readonly_when_remote',
            'mutable',
        ):
            payload[flag] = bool(payload.get(flag))
        if payload['mutable'] and setting_key not in self._mutable_setting_keys:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned unauthorized mutable setting.')
        if payload['secret']:
            payload.pop('default_value', None)
        if payload.get('storage_owner') not in {'core', 'plugin_external'}:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid storage owner.')
        if payload.get('storage_scope') not in {'runtime', 'persistent', 'external'}:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid storage scope.')
        if payload.get('apply_scope') not in {'live', 'next_connection', 'restart'}:
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid apply scope.')
        allowed_values = payload.get('allowed_values')
        if allowed_values is not None and not isinstance(allowed_values, list):
            raise ValueError(f'Terminal backend {plugin.connection_type} returned invalid allowed values.')
        return payload

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
