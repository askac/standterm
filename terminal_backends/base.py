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

    def session_metadata(self):
        return {
            'session_token': self.owner_session,
            'terminal_id': self.terminal_id,
            'connection_type': self.connection_type,
            'terminal_kind': self.terminal_kind,
            'terminal_label': self.terminal_label,
            'cols': self.cols,
            'rows': self.rows,
            'output_seq': self.output_seq,
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

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
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
