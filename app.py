import os
import sys
import subprocess
import base64
import getpass
import logging
import secrets
import socket
import time
import argparse
import select
import shutil
import re
import ipaddress
import json
import hmac
import hashlib
import threading
import shlex
import urllib.parse
from collections import deque
from pathlib import Path
from flask import Flask, render_template, request, abort, make_response, redirect, send_file, jsonify
from flask_socketio import SocketIO

try:
    from ptyprocess import PtyProcessUnicode
except Exception:
    PtyProcessUnicode = None

try:
    from winpty import PtyProcess as WinPtyProcess
except Exception:
    WinPtyProcess = None

paramiko = None
serial_module = None
serial_list_ports_module = None

ASYNC_MODE = os.getenv('WEBSSH_ASYNC_MODE', '').strip().lower()
if not ASYNC_MODE:
    ASYNC_MODE = 'threading'

if ASYNC_MODE == 'eventlet':
    try:
        import eventlet
        eventlet.monkey_patch()
    except Exception as exc:
        print(f"[!] Eventlet unavailable ({exc}); falling back to threading.", file=sys.stderr)
        ASYNC_MODE = 'threading'

app = Flask(__name__, static_url_path='/static', static_folder='static')
app.config['SECRET_KEY'] = secrets.token_hex(16)
ACCESS_TOKEN = secrets.token_urlsafe(16)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Default to threading for consistent cross-platform behavior.
socketio = SocketIO(app, async_mode=ASYNC_MODE, logger=False, engineio_logger=False)

# SSH Configuration
SSH_HOST = '127.0.0.1'
SSH_PORT = 22
SSH_USER = os.getenv('USER', 'aska')
DEFAULT_BIND_HOST = os.getenv('WEBSSH_HOST', '').strip()
DEFAULT_PORT = int(os.getenv('WEBSSH_PORT', '5000'))
AGENT_EXTERNAL_DEV_TOKEN_ENABLED = os.getenv('WEBSSH_AGENT_DEV_TOKEN', '').strip().lower() in {'1', 'true', 'yes', 'on'}

def parse_optional_seconds_env(name, default=None):
    raw_value = os.getenv(name, '').strip().lower()
    if not raw_value:
        return default
    if raw_value in {'0', 'none', 'session', 'disconnect', 'off'}:
        return None
    try:
        seconds = int(raw_value)
    except ValueError:
        print(f"[!] Ignoring invalid {name}={raw_value!r}; expected seconds or 'session'.", file=sys.stderr)
        return default
    if seconds < 0:
        print(f"[!] Ignoring invalid {name}={raw_value!r}; expected a non-negative value.", file=sys.stderr)
        return default
    return seconds

SSH_TERM = 'xterm-256color'
MAX_SSH_INPUT_BYTES = 65536
MAX_PASSWORD_BYTES = 4096
MAX_HOST_LENGTH = 255
MAX_USERNAME_LENGTH = 128
SESSION_COOKIE_NAME = 'webssh_session'
SESSION_COOKIE_MAX_AGE = 12 * 60 * 60
SESSION_CLEANUP_INTERVAL_SECONDS = 60
LOCALHOST_KEY_SETUP_TTL_SECONDS = 120
MIN_TERMINAL_COLS = 2
MAX_TERMINAL_COLS = 500
MIN_TERMINAL_ROWS = 2
MAX_TERMINAL_ROWS = 500
CONNECTION_TYPE_SSH = 'ssh'
CONNECTION_TYPE_LOCAL_SHELL = 'local_shell'
CONNECTION_TYPE_UART = 'uart'
LOCAL_SHELL_KIND_BASH = 'bash'
LOCAL_SHELL_KIND_CMD = 'cmd'
LOCAL_SHELL_KIND_POWERSHELL = 'powershell'
WSL_LOCAL_SHELL_KINDS = (
    LOCAL_SHELL_KIND_BASH,
    LOCAL_SHELL_KIND_CMD,
    LOCAL_SHELL_KIND_POWERSHELL,
)
CONNECTION_TYPES = (
    CONNECTION_TYPE_SSH,
    CONNECTION_TYPE_LOCAL_SHELL,
    CONNECTION_TYPE_UART,
)
RESERVED_BACKEND_PAYLOAD_KEYS = {
    'connection_type',
    'terminal_id',
    'owner_session',
    'session_token',
}
ALLOWED_CONNECTION_ACTION_TYPES = {
    'offer_localhost_key_setup',
}
TERMINAL_ID_MAIN = 'main'
MAX_TERMINAL_ID_LENGTH = 64
MAX_TERMINALS_PER_CLIENT = 12
MAX_TERMINAL_REPLAY_EVENTS = 1000
MAX_TERMINAL_REPLAY_BYTES = 200000
AGENT_MODE_DISABLED = 'disabled'
AGENT_MODE_OBSERVE = 'observe'
AGENT_MODE_APPROVAL_PENDING = 'approval_pending'
AGENT_MODE_DIRECT_ACTIVE = 'direct_active'
AGENT_MODE_PAUSED = 'paused'
AGENT_CLIENT_MODE_MAP = {
    'disabled': AGENT_MODE_DISABLED,
    'observe': AGENT_MODE_OBSERVE,
    'approval': AGENT_MODE_APPROVAL_PENDING,
    'approval_pending': AGENT_MODE_APPROVAL_PENDING,
    'direct': AGENT_MODE_DIRECT_ACTIVE,
    'direct_active': AGENT_MODE_DIRECT_ACTIVE,
}
AGENT_ACTION_TERMINAL_INPUT = 'terminal_input'
AGENT_STATUS_PENDING_APPROVAL = 'pending_approval'
AGENT_STATUS_DIRECT_PENDING = 'direct_pending'
AGENT_STATUS_APPROVED = 'approved'
AGENT_STATUS_COMPLETED = 'completed'
AGENT_STATUS_FAILED = 'failed'
AGENT_STATUS_REJECTED = 'rejected'
AGENT_STATUS_WRITABLE = {
    AGENT_STATUS_APPROVED,
    AGENT_STATUS_DIRECT_PENDING,
}
AGENT_STATUS_OPEN = {
    AGENT_STATUS_PENDING_APPROVAL,
    AGENT_STATUS_DIRECT_PENDING,
    AGENT_STATUS_APPROVED,
}
AGENT_EVENT_ATTACH = 'agent_attach'
AGENT_EVENT_DETACH = 'agent_detach'
AGENT_EVENT_MODE_SET = 'agent_mode_set'
AGENT_EVENT_PAUSE = 'agent_pause'
AGENT_EVENT_RESUME = 'agent_resume'
AGENT_EVENT_SUGGESTION_REQUEST = 'agent_suggestion_request'
AGENT_EVENT_PROVIDER_RUN_REQUEST = 'agent_provider_run_request'
AGENT_EVENT_ACTION_APPROVE = 'agent_action_approve'
AGENT_EVENT_ACTION_REJECT = 'agent_action_reject'
AGENT_EVENT_VIEWPORT_SNAPSHOT = 'agent_viewport_snapshot'
AGENT_EVENT_VIEWPORT_RENDER_REQUEST = 'agent_viewport_render_request'
AGENT_EVENT_VIEWPORT_RENDER_RESULT = 'agent_viewport_render_result'
AGENT_EVENT_STATE = 'agent_state'
AGENT_EVENT_ACTION_REQUEST = 'agent_action_request'
AGENT_EVENT_ACTION_RESULT = 'agent_action_result'
AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT = 'agent_viewport_snapshot_result'
AGENT_EVENT_PRIVACY_SET = 'agent_privacy_set'
AGENT_ERROR_NOT_ATTACHED = 'agent_not_attached'
AGENT_ERROR_PAUSED = 'agent_paused'
AGENT_ERROR_STALE_EPOCH = 'agent_stale_epoch'
AGENT_ERROR_ACTION_NOT_FOUND = 'agent_action_not_found'
AGENT_ERROR_STALE_ACTION = 'agent_stale_action'
AGENT_ERROR_ACTION_NOT_WRITABLE = 'agent_action_not_writable'
AGENT_ERROR_TERMINAL_MISMATCH = 'agent_terminal_mismatch'
AGENT_ERROR_TERMINAL_NOT_FOUND = 'terminal_not_found'
AGENT_ERROR_ACTION_INVALID_DATA = 'agent_action_invalid_data'
AGENT_ERROR_ACTION_TOO_LARGE = 'agent_action_too_large'
AGENT_ERROR_INVALID_MODE = 'agent_invalid_mode'
AGENT_ERROR_ACTION_NOT_ALLOWED = 'agent_action_not_allowed'
AGENT_ERROR_MODE_NOT_WRITABLE = 'agent_mode_not_writable'
AGENT_ERROR_ACTION_NOT_PENDING = 'agent_action_not_pending'
AGENT_ERROR_SNAPSHOT_INVALID = 'agent_snapshot_invalid'
AGENT_ERROR_SNAPSHOT_TOO_LARGE = 'agent_snapshot_too_large'
AGENT_ERROR_SNAPSHOT_STALE = 'agent_snapshot_stale'
AGENT_ERROR_RENDER_INVALID = 'agent_render_invalid'
AGENT_ERROR_RENDER_TOO_LARGE = 'agent_render_too_large'
AGENT_ERROR_RENDER_TIMEOUT = 'agent_render_timeout'
AGENT_ERROR_RENDER_STALE = 'agent_render_stale'
AGENT_ERROR_PRIVACY_BLOCKED = 'agent_privacy_blocked'
AGENT_ERROR_STALE_MODE_VERSION = 'agent_stale_mode_version'
AGENT_ERROR_STALE_PROPOSAL = 'agent_stale_proposal'
AGENT_ERROR_PROVIDER_UNAVAILABLE = 'agent_provider_unavailable'
AGENT_ERROR_PROVIDER_FAILED = 'agent_provider_failed'
AGENT_ERROR_PROVIDER_TIMEOUT = 'agent_provider_timeout'
AGENT_ERROR_PROVIDER_INVALID_PROPOSAL = 'agent_provider_invalid_proposal'
AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED = 'agent_external_unauthorized'
AGENT_ERROR_EXTERNAL_AGENT_EXPIRED = 'agent_external_expired'
AGENT_ERROR_EXTERNAL_AGENT_REVOKED = 'agent_external_revoked'
AGENT_ERROR_EXTERNAL_AGENT_DISABLED = 'agent_external_disabled'
AGENT_ERROR_HUMAN_INPUT_ACTIVE = 'agent_human_input_active'
AGENT_REASON_DETACHED = 'agent_detached'
AGENT_REASON_DISABLED = 'agent_disabled'
AGENT_REASON_MODE_CHANGED = 'agent_mode_changed'
AGENT_REASON_DISCONNECTED = 'agent_disconnected'
AGENT_REASON_TERMINAL_CLOSED = 'terminal_closed'
AGENT_REASON_INVALIDATED = 'agent_invalidated'
AGENT_RUN_STATUS_REQUESTED = 'requested'
AGENT_RUN_STATUS_RUNNING = 'running'
AGENT_RUN_STATUS_COMPLETED = 'completed'
AGENT_RUN_STATUS_FAILED = 'failed'
AGENT_RUN_STATUS_TIMEOUT = 'timeout'
AGENT_RUN_STATUS_CANCELLED = 'cancelled'
AGENT_MAX_INPUT_BYTES = 4096
AGENT_INPUT_CHUNK_BYTES = 256
AGENT_AUDIT_EVENTS = 200
AGENT_AUDIT_TTL_SECONDS = 12 * 60 * 60
AGENT_PREVIEW_CHARS = 160
AGENT_TRANSCRIPT_TTL_SECONDS = 30 * 60
AGENT_TRANSCRIPT_MAX_EVENTS = 400
AGENT_TRANSCRIPT_MAX_BYTES = 120000
AGENT_TRANSCRIPT_MAX_EVENT_BYTES = 4096
AGENT_USER_INPUT_METADATA_TTL_SECONDS = 30 * 60
AGENT_USER_INPUT_METADATA_MAX_EVENTS = 400
AGENT_USER_INPUT_PREVIEW_CHARS = 80
AGENT_PRIVACY_NORMAL = 'normal'
AGENT_PRIVACY_PRIVATE_INPUT = 'private_input'
AGENT_PRIVACY_PASTE_REVIEW = 'paste_review'
AGENT_PRIVACY_PAUSED = 'paused'
AGENT_PRIVACY_STATES = {
    AGENT_PRIVACY_NORMAL,
    AGENT_PRIVACY_PRIVATE_INPUT,
    AGENT_PRIVACY_PASTE_REVIEW,
    AGENT_PRIVACY_PAUSED,
}
AGENT_CONTEXT_BLOCKING_PRIVACY_STATES = {
    AGENT_PRIVACY_PRIVATE_INPUT,
    AGENT_PRIVACY_PASTE_REVIEW,
    AGENT_PRIVACY_PAUSED,
}
AGENT_VIEWPORT_SNAPSHOT_TTL_SECONDS = 5 * 60
AGENT_VIEWPORT_SNAPSHOT_MAX_BYTES = 120000
AGENT_VIEWPORT_SNAPSHOT_MAX_LINE_BYTES = 4096
AGENT_VIEWPORT_RENDER_REQUEST_TTL_SECONDS = 10
AGENT_VIEWPORT_RENDER_WAIT_MS = 3000
AGENT_VIEWPORT_RENDER_MAX_WAIT_MS = 10000
AGENT_VIEWPORT_RENDER_MAX_IMAGE_BYTES = 1024 * 1024
AGENT_VIEWPORT_RENDER_MAX_PIXELS = 4096 * 4096
AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS = parse_optional_seconds_env(
    'WEBSSH_AGENT_EXTERNAL_IDLE_TIMEOUT_SECONDS',
    default=5 * 60,
)
AGENT_EXTERNAL_TAIL_MAX_EVENTS = 200
AGENT_EXTERNAL_TAIL_MAX_WAIT_MS = 30000
AGENT_HUMAN_INPUT_LEASE_SECONDS = 2.0
AGENT_AUDIT_VIEWER_ATTACH = 'viewer_attach'
AGENT_AUDIT_VIEWER_DETACH = 'viewer_detach'
AGENT_AUDIT_MODE_SET = 'mode_set'
AGENT_AUDIT_PAUSE = 'pause'
AGENT_AUDIT_RESUME = 'resume'
AGENT_AUDIT_PRIVACY_SET = 'privacy_set'
AGENT_AUDIT_PROVIDER_RUN_REQUEST = 'provider_run_request'
AGENT_AUDIT_PROVIDER_RUN_START = 'provider_run_start'
AGENT_AUDIT_PROVIDER_RUN_COMPLETE = 'provider_run_complete'
AGENT_AUDIT_PROVIDER_RUN_ERROR = 'provider_run_error'
AGENT_AUDIT_EXTERNAL_AGENT_TOKEN_CREATED = 'external_agent_token_created'
AGENT_AUDIT_EXTERNAL_AGENT_ATTACHED = 'external_agent_attached'
AGENT_AUDIT_EXTERNAL_AGENT_REVOKED = 'external_agent_revoked'
AGENT_AUDIT_EXTERNAL_AGENT_SCREEN = 'external_agent_screen'
AGENT_AUDIT_EXTERNAL_AGENT_RENDER = 'external_agent_render'
AGENT_AUDIT_EXTERNAL_AGENT_TAIL = 'external_agent_tail'
AGENT_AUDIT_EXTERNAL_AGENT_SEND = 'external_agent_send'
AGENT_AUDIT_CONTEXT_BUILT = 'context_built'
AGENT_AUDIT_PROPOSAL_CREATED = 'proposal_created'
AGENT_AUDIT_ACTION_APPROVE = 'action_approve'
AGENT_AUDIT_ACTION_REJECT = 'action_reject'
AGENT_AUDIT_ACTION_RESULT = 'action_result'
AGENT_AUDIT_DIRECT_WRITE = 'direct_write'
AGENT_AUDIT_TERMINAL_CLEANUP = 'terminal_cleanup'
AGENT_AUDIT_ERROR = 'error'
EXTERNAL_AGENT_PROTOCOL_VERSION = 1
EXTERNAL_AGENT_CAPABILITIES = ['state', 'screen', 'render', 'tail', 'send', 'send_capture', 'strip_ansi', 'revoke']
AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_WAIT_MS = 3000
AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_SETTLE_MS = 150
AGENT_EXTERNAL_SEND_CAPTURE_MAX_SETTLE_MS = 5000
ANSI_OSC_PATTERN = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
ANSI_CSI_PATTERN = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b[@-Z\\-_]')
AGENT_TRANSCRIPT_CONTROL_PATTERN = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
MAX_UART_PORT_LENGTH = 128
DEFAULT_UART_BAUD_RATE = 115200
MIN_UART_BAUD_RATE = 300
MAX_UART_BAUD_RATE = 4000000
UART_BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
SERIAL_PORT_CACHE_TTL_SECONDS = 10
WINDOWS_COM_PATTERN = re.compile(r'^COM([1-9][0-9]*)$', re.IGNORECASE)
BROWSER_PAIRING_TYPE = 'webssh_browser_authorization'
BROWSER_PAIRING_VERSION = 1
BROWSER_PAIRING_TTL_SECONDS = 10 * 60
MAX_BROWSER_PUBLIC_KEY_BYTES = 4096
MAX_BROWSER_SIGNATURE_BYTES = 256
MAX_BROWSER_ID_LENGTH = 128
TERMINAL_ID_PATTERN = re.compile(r'^[A-Za-z0-9_.:-]+$')
BROWSER_ID_PATTERN = re.compile(r'^[a-f0-9]{64}$')
LOCAL_PUBLIC_KEY_TYPES = {
    'ssh-ed25519',
    'ssh-rsa',
    'ssh-dss',
    'ecdsa-sha2-nistp256',
    'ecdsa-sha2-nistp384',
    'ecdsa-sha2-nistp521',
    'sk-ssh-ed25519@openssh.com',
    'sk-ecdsa-sha2-nistp256@openssh.com',
}

def normalize_connection_type(value):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace('-', '_')
    if normalized in CONNECTION_TYPES:
        return normalized
    return None

def connection_type_cli_value(value):
    normalized = normalize_connection_type(value)
    if not normalized:
        raise argparse.ArgumentTypeError('expected ssh, local-shell, or uart')
    return normalized

def parse_cli_args(argv):
    parser = argparse.ArgumentParser(description='WebSSH server')
    parser.add_argument(
        '--default-connection',
        type=connection_type_cli_value,
        default=CONNECTION_TYPE_LOCAL_SHELL,
        metavar='ssh|local-shell|uart',
        help='Default connection mode shown in the UI.',
    )
    parser.add_argument(
        '--force-connection',
        type=connection_type_cli_value,
        default=None,
        metavar='ssh|local-shell|uart',
        help='Force one connection mode in both the UI and backend.',
    )
    parser.add_argument(
        '--force',
        '-f',
        action='store_true',
        help='Accepted for run script compatibility; dependency checks are handled by the launcher.',
    )
    parser.add_argument(
        '--debug-input',
        '-d',
        action='store_true',
        help='Log terminal input bytes for debugging key sequences.',
    )
    parser.add_argument(
        '--https',
        action='store_true',
        help='Serve WebSSH over HTTPS using a local generated certificate.',
    )
    parser.add_argument(
        '--certfile',
        default=None,
        help='TLS certificate file for HTTPS.',
    )
    parser.add_argument(
        '--keyfile',
        default=None,
        help='TLS private key file for HTTPS.',
    )
    return parser.parse_args(argv)

CLI_ARGS = parse_cli_args(sys.argv[1:])
DEFAULT_CONNECTION_TYPE = CLI_ARGS.default_connection
FORCE_CONNECTION_TYPE = CLI_ARGS.force_connection
DEBUG_INPUT = CLI_ARGS.debug_input or os.getenv('WEBSSH_DEBUG_INPUT') == '1'
DEBUG_POLICY = os.getenv('WEBSSH_DEBUG_POLICY') == '1'
HTTPS_REQUESTED = CLI_ARGS.https or os.getenv('WEBSSH_HTTPS') == '1' or bool(CLI_ARGS.certfile or CLI_ARGS.keyfile)
HTTPS_ENABLED = HTTPS_REQUESTED
HTTPS_AUTO_DISABLED = os.getenv('WEBSSH_DISABLE_AUTO_HTTPS') == '1'
APP_DIR = Path(__file__).resolve().parent
EXTERNAL_AGENT_HANDOFF_PATH = APP_DIR / 'webssh_external_agent_handoff.json'
AUTHORIZED_DIR = APP_DIR / 'authorized'
AUTHORIZED_BROWSERS_PATH = AUTHORIZED_DIR / 'browsers.json'

def is_wsl_runtime_hint():
    if not sys.platform.startswith('linux'):
        return False
    if os.getenv('WSL_DISTRO_NAME'):
        return True
    try:
        with open('/proc/version', 'r', encoding='utf-8') as version_file:
            return 'microsoft' in version_file.read().lower()
    except OSError:
        return False

def get_default_certs_dir():
    configured_dir = os.getenv('WEBSSH_CERTS_DIR', '').strip()
    if configured_dir:
        return Path(configured_dir).expanduser()
    if is_wsl_runtime_hint() and str(APP_DIR).startswith('/mnt/'):
        app_hash = hashlib.sha256(str(APP_DIR).encode('utf-8')).hexdigest()[:12]
        return Path.home() / '.webssh' / 'certs' / app_hash
    return APP_DIR / 'certs'

CERTS_DIR = get_default_certs_dir()
LOCAL_CA_CERT_PATH = CERTS_DIR / 'webssh-local-ca.crt'
LOCAL_CA_KEY_PATH = CERTS_DIR / 'webssh-local-ca.key'
LOCAL_SERVER_CERT_PATH = CERTS_DIR / 'webssh-server.crt'
LOCAL_SERVER_KEY_PATH = CERTS_DIR / 'webssh-server.key'
BROWSER_PAIRING_SECRET = secrets.token_bytes(32)
serial_port_cache = {
    'expires_at': 0,
    'ports': [],
}
wsl_ip_cache = None
WINDOWS_SERIAL_HELPER = r'''
import json
import sys
import threading

try:
    import serial
except Exception as exc:
    sys.stderr.write(json.dumps({"event": "error", "message": f"pyserial is not available in Windows Python: {exc}"}) + "\n")
    sys.stderr.flush()
    raise SystemExit(1)

port = sys.argv[1]
baud_rate = int(sys.argv[2])

try:
    serial_port = serial.Serial(port=port, baudrate=baud_rate, timeout=0.05, write_timeout=1)
except Exception as exc:
    sys.stderr.write(json.dumps({"event": "error", "message": f"Failed to open {port}: {exc}"}) + "\n")
    sys.stderr.flush()
    raise SystemExit(2)

sys.stderr.write(json.dumps({"event": "ready"}) + "\n")
sys.stderr.flush()

def copy_stdin_to_serial():
    while True:
        data = sys.stdin.buffer.read(4096)
        if not data:
            break
        serial_port.write(data)
        serial_port.flush()

threading.Thread(target=copy_stdin_to_serial, daemon=True).start()

try:
    while True:
        data = serial_port.read(4096)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
except KeyboardInterrupt:
    pass
finally:
    serial_port.close()
'''

def build_terminal_policy(browser_authorized=False):
    connection_options = TERMINAL_BACKEND_REGISTRY.build_policy_options(browser_authorized=browser_authorized)
    allowed_connections = {
        option['connection_type']: bool(option.get('allowed'))
        for option in connection_options
    }
    browser_authorization_required_for = [
        option['connection_type']
        for option in connection_options
        if option.get('authorization_available')
    ]
    default_connection = DEFAULT_CONNECTION_TYPE
    if not allowed_connections.get(default_connection):
        default_connection = TERMINAL_BACKEND_REGISTRY.get_default_connection(allowed_connections)
    force_connection = FORCE_CONNECTION_TYPE
    if force_connection and not allowed_connections.get(force_connection):
        force_connection = None

    return {
        'default_connection': default_connection,
        'force_connection': force_connection,
        'https_enabled': HTTPS_ENABLED,
        'ca_download_url': '/download_ca' if HTTPS_ENABLED and not (CLI_ARGS.certfile and CLI_ARGS.keyfile) else None,
        'authorized_dir': str(AUTHORIZED_DIR),
        'localhost_access_url': get_localhost_access_url(DEFAULT_PORT) if is_wsl() else None,
        'browser_authorization': {
            'available': bool(browser_authorization_required_for),
            'authorized': bool(browser_authorized),
            'required_for': browser_authorization_required_for,
        },
        'connection_options': connection_options,
    }

def build_terminal_metadata(connection_type, terminal_id, terminal_kind, terminal_label, cols, rows):
    return {
        'connection_type': connection_type,
        'terminal_id': terminal_id,
        'terminal_kind': terminal_kind,
        'terminal_label': terminal_label,
        'term': SSH_TERM,
        'cols': cols,
        'rows': rows,
    }

def get_request_client_ip():
    return request.remote_addr or request.environ.get('REMOTE_ADDR') or 'unknown'

def get_paramiko():
    global paramiko
    if paramiko is None:
        import paramiko as paramiko_module
        paramiko = paramiko_module
    return paramiko

def get_serial_modules():
    global serial_module, serial_list_ports_module
    if serial_module is None:
        import serial as imported_serial
        import serial.tools.list_ports as imported_list_ports
        serial_module = imported_serial
        serial_list_ports_module = imported_list_ports
    return serial_module, serial_list_ports_module

def iter_windows_python_candidates():
    candidates = []
    repo_windows_helper_venv = APP_DIR / 'tools' / '.venv_win' / 'Scripts' / 'python.exe'
    repo_windows_venv = APP_DIR / 'tools' / '.venv' / 'Scripts' / 'python.exe'
    candidates.append(str(repo_windows_helper_venv))
    candidates.append(str(repo_windows_venv))
    for executable_name in ('python.exe', 'py.exe'):
        found = shutil.which(executable_name)
        if found:
            candidates.append(found)

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate

def find_windows_python_with_pyserial():
    last_error = None
    for candidate in iter_windows_python_candidates():
        try:
            result = subprocess.run(
                [candidate, '-c', 'import serial'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0:
            return candidate, None
        stderr = (result.stderr or result.stdout or '').strip()
        last_error = f'{candidate} cannot import pyserial: {stderr or "unknown error"}'
    return None, last_error or 'No Windows Python executable was found for WSL COM access.'

def close_process(process):
    if not process:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass

def load_or_create_private_key(key_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if key_path.is_file():
        return serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key

def chmod_private_key(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def ensure_local_https_certificates(bind_host, access_host):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CERTS_DIR, 0o700)
    except OSError:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)

    if LOCAL_CA_CERT_PATH.is_file() and LOCAL_CA_KEY_PATH.is_file():
        ca_key = serialization.load_pem_private_key(LOCAL_CA_KEY_PATH.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(LOCAL_CA_CERT_PATH.read_bytes())
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, 'WebSSH Local Development CA'),
        ])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        LOCAL_CA_KEY_PATH.write_bytes(ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        LOCAL_CA_CERT_PATH.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        chmod_private_key(LOCAL_CA_KEY_PATH)

    server_key = load_or_create_private_key(LOCAL_SERVER_KEY_PATH)
    dns_names = {'localhost'}
    ip_addresses = {'127.0.0.1', '::1'}
    for host in (bind_host, access_host, get_wsl_ip() if is_wsl() else None):
        if not host or host in {'0.0.0.0', '::'}:
            continue
        try:
            ipaddress.ip_address(host)
            ip_addresses.add(host)
        except ValueError:
            dns_names.add(host)

    san_entries = [x509.DNSName(name) for name in sorted(dns_names)]
    san_entries.extend(x509.IPAddress(ipaddress.ip_address(address)) for address in sorted(ip_addresses))
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'WebSSH Local Server'),
    ])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    LOCAL_SERVER_CERT_PATH.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    chmod_private_key(LOCAL_CA_KEY_PATH)
    chmod_private_key(LOCAL_SERVER_KEY_PATH)
    return str(LOCAL_SERVER_CERT_PATH), str(LOCAL_SERVER_KEY_PATH)

def _format_serial_label(port_info):
    device = getattr(port_info, 'device', '') or ''
    description = getattr(port_info, 'description', '') or ''
    label = device

    path_name = Path(device).name
    if sys.platform.startswith('linux') and path_name.startswith('ttyS'):
        suffix = path_name[4:]
        if suffix.isdigit():
            label = f'COM{int(suffix) + 1} ({device})'

    if description and description.lower() not in {'n/a', device.lower()}:
        label = f'{label} - {description}'
    return label

def is_windows_com_device(device):
    return isinstance(device, str) and bool(WINDOWS_COM_PATTERN.fullmatch(device.strip()))

def detect_windows_serial_ports_for_wsl():
    if not is_wsl():
        return []

    try:
        result = subprocess.run(
            [
                'powershell.exe',
                '-NoProfile',
                '-Command',
                '[System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object',
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    ports = []
    seen_devices = set()
    for line in result.stdout.splitlines():
        device = line.strip()
        if not is_windows_com_device(device):
            continue
        normalized = device.upper()
        if normalized in seen_devices or len(normalized) > MAX_UART_PORT_LENGTH:
            continue
        seen_devices.add(normalized)
        ports.append({
            'device': normalized,
            'label': f'{normalized} (Windows)',
            'description': 'Windows serial port',
            'hwid': '',
            'backend': 'windows',
        })
    return ports

def scan_serial_ports():
    if is_wsl():
        return detect_windows_serial_ports_for_wsl()

    try:
        _, list_ports_module = get_serial_modules()
    except Exception:
        return []

    ports = []
    seen_devices = set()
    for port_info in sorted(list_ports_module.comports(), key=lambda item: (item.device or '').lower()):
        device = getattr(port_info, 'device', '') or ''
        if not device or device in seen_devices:
            continue
        if len(device) > MAX_UART_PORT_LENGTH or has_control_chars(device):
            continue
        seen_devices.add(device)
        ports.append({
            'device': device,
            'label': _format_serial_label(port_info),
            'description': getattr(port_info, 'description', '') or '',
            'hwid': getattr(port_info, 'hwid', '') or '',
        })
    return ports

def detect_serial_ports():
    now = time.time()
    if serial_port_cache['expires_at'] > now:
        return [dict(port) for port in serial_port_cache['ports']]

    ports = scan_serial_ports()
    serial_port_cache['ports'] = [dict(port) for port in ports]
    serial_port_cache['expires_at'] = now + SERIAL_PORT_CACHE_TTL_SECONDS
    return ports

def get_manual_serial_port(device):
    if not isinstance(device, str):
        return None
    selected_device = device.strip()
    if not selected_device or len(selected_device) > MAX_UART_PORT_LENGTH:
        return None
    if has_control_chars(selected_device):
        return None

    if is_windows_com_device(selected_device):
        normalized = selected_device.upper()
        return {
            'device': normalized,
            'label': normalized,
            'description': 'Manual Windows serial port',
            'hwid': '',
            'backend': 'windows',
        }

    if selected_device.startswith('/dev/'):
        return {
            'device': selected_device,
            'label': selected_device,
            'description': 'Manual serial device',
            'hwid': '',
            'backend': 'manual',
        }

    return None

def decode_base64_bytes(value, max_bytes):
    if not isinstance(value, str):
        return None
    try:
        decoded = base64.b64decode(value.encode('ascii'), validate=True)
    except Exception:
        return None
    if not decoded or len(decoded) > max_bytes:
        return None
    return decoded

def build_browser_id(public_key_b64):
    public_key_bytes = decode_base64_bytes(public_key_b64, MAX_BROWSER_PUBLIC_KEY_BYTES)
    if not public_key_bytes:
        return None
    return hashlib.sha256(public_key_bytes).hexdigest()

def validate_browser_identity(browser_id, public_key_b64):
    if not isinstance(browser_id, str) or not BROWSER_ID_PATTERN.fullmatch(browser_id):
        return False
    expected_browser_id = build_browser_id(public_key_b64)
    return expected_browser_id == browser_id

def load_authorized_browsers():
    try:
        data = json.loads(AUTHORIZED_BROWSERS_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {'browsers': []}
    if not isinstance(data, dict) or not isinstance(data.get('browsers'), list):
        return {'browsers': []}
    return data

def save_authorized_browsers(data):
    AUTHORIZED_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = AUTHORIZED_BROWSERS_PATH.with_suffix('.json.tmp')
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp_path.replace(AUTHORIZED_BROWSERS_PATH)

def get_authorized_browser(browser_id):
    for entry in load_authorized_browsers().get('browsers', []):
        if isinstance(entry, dict) and entry.get('browser_id') == browser_id:
            return entry
    return None

def is_browser_authorized(browser_id, public_key_b64):
    entry = get_authorized_browser(browser_id)
    return bool(entry and entry.get('public_key') == public_key_b64)

def authorize_browser(browser_id, public_key_b64):
    data = load_authorized_browsers()
    browsers = [
        entry for entry in data.get('browsers', [])
        if isinstance(entry, dict) and entry.get('browser_id') != browser_id
    ]
    browsers.append({
        'browser_id': browser_id,
        'public_key': public_key_b64,
        'authorized_at': int(time.time()),
    })
    data['browsers'] = sorted(browsers, key=lambda entry: entry.get('browser_id', ''))
    save_authorized_browsers(data)

def canonical_pairing_payload(pairing):
    payload = {
        'type': pairing['type'],
        'version': pairing['version'],
        'pairing_id': pairing['pairing_id'],
        'browser_id': pairing['browser_id'],
        'public_key': pairing['public_key'],
        'server_nonce': pairing['server_nonce'],
        'expires_at': pairing['expires_at'],
    }
    return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')

def sign_pairing_payload(pairing):
    return hmac.new(BROWSER_PAIRING_SECRET, canonical_pairing_payload(pairing), hashlib.sha256).hexdigest()

def build_pairing_file(browser_id, public_key_b64):
    pairing = {
        'type': BROWSER_PAIRING_TYPE,
        'version': BROWSER_PAIRING_VERSION,
        'pairing_id': secrets.token_urlsafe(16),
        'browser_id': browser_id,
        'public_key': public_key_b64,
        'server_nonce': secrets.token_urlsafe(32),
        'expires_at': int(time.time() + BROWSER_PAIRING_TTL_SECONDS),
    }
    pairing['signature'] = sign_pairing_payload(pairing)
    return pairing

def validate_pairing_file(data, browser_id, public_key_b64):
    if not isinstance(data, dict):
        return False
    expected = {
        'type': BROWSER_PAIRING_TYPE,
        'version': BROWSER_PAIRING_VERSION,
        'browser_id': browser_id,
        'public_key': public_key_b64,
    }
    for key, value in expected.items():
        if data.get(key) != value:
            return False
    if not isinstance(data.get('pairing_id'), str) or not isinstance(data.get('server_nonce'), str):
        return False
    try:
        expires_at = int(data.get('expires_at'))
    except (TypeError, ValueError):
        return False
    if time.time() > expires_at:
        return False
    signature = data.get('signature')
    if not isinstance(signature, str):
        return False
    return hmac.compare_digest(signature, sign_pairing_payload(data))

def accept_browser_pairing_file(browser_id, public_key_b64):
    if not AUTHORIZED_DIR.is_dir():
        return False
    for pairing_path in sorted(AUTHORIZED_DIR.glob('webssh-authorize_*.json')):
        try:
            data = json.loads(pairing_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if validate_pairing_file(data, browser_id, public_key_b64):
            authorize_browser(browser_id, public_key_b64)
            return True
    return False

def verify_browser_signature(public_key_b64, nonce, signature_b64):
    public_key_bytes = decode_base64_bytes(public_key_b64, MAX_BROWSER_PUBLIC_KEY_BYTES)
    signature = decode_base64_bytes(signature_b64, MAX_BROWSER_SIGNATURE_BYTES)
    if not public_key_bytes or not signature or not isinstance(nonce, str):
        return False

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        public_key = serialization.load_der_public_key(public_key_bytes)
        if len(signature) == 64:
            r = int.from_bytes(signature[:32], 'big')
            s = int.from_bytes(signature[32:], 'big')
            signature = utils.encode_dss_signature(r, s)
        public_key.verify(signature, nonce.encode('utf-8'), ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False

def get_shell_kind(shell_path):
    shell_name = Path(shell_path).name.lower()
    if shell_name in {'bash', 'zsh', 'sh', 'fish', 'dash', 'ksh'}:
        return shell_name
    return 'shell'

def get_shell_label(shell_kind):
    labels = {
        'bash': 'bash',
        'zsh': 'zsh',
        'sh': 'sh',
        'fish': 'fish',
        'dash': 'dash',
        'ksh': 'ksh',
        'powershell': 'PowerShell',
        'pwsh': 'PowerShell',
        'cmd': 'cmd',
        'shell': 'Shell',
    }
    return labels.get(shell_kind, 'Shell')

def get_windows_shell_path():
    for shell_name in ('pwsh.exe', 'powershell.exe', 'cmd.exe'):
        shell_path = shutil.which(shell_name)
        if shell_path:
            return shell_path
    return 'cmd.exe'

def get_windows_shell_kind(shell_path):
    shell_name = Path(shell_path).name.lower()
    if shell_name == 'pwsh.exe':
        return 'pwsh'
    if shell_name == 'powershell.exe':
        return 'powershell'
    if shell_name == 'cmd.exe':
        return 'cmd'
    return 'shell'

def get_wsl_local_shell_options():
    return [
        {
            'kind': LOCAL_SHELL_KIND_BASH,
            'label': 'bash',
            'default': True,
        },
        {
            'kind': LOCAL_SHELL_KIND_CMD,
            'label': 'cmd.exe',
            'default': False,
        },
        {
            'kind': LOCAL_SHELL_KIND_POWERSHELL,
            'label': 'PowerShell',
            'default': False,
        },
    ]

def get_wsl_local_shell_config(shell_kind):
    requested_kind = shell_kind.strip().lower() if isinstance(shell_kind, str) else ''
    if not requested_kind:
        requested_kind = LOCAL_SHELL_KIND_BASH
    if requested_kind not in WSL_LOCAL_SHELL_KINDS:
        return None, {
            'message': 'Local Shell kind must be bash, cmd, or powershell on WSL.',
            'error_code': 'local_shell_invalid_kind',
        }

    shell_paths = {
        LOCAL_SHELL_KIND_BASH: shutil.which('bash') or '/bin/bash',
        LOCAL_SHELL_KIND_CMD: shutil.which('cmd.exe') or 'cmd.exe',
        LOCAL_SHELL_KIND_POWERSHELL: shutil.which('powershell.exe') or 'powershell.exe',
    }
    labels = {
        LOCAL_SHELL_KIND_BASH: 'bash',
        LOCAL_SHELL_KIND_CMD: 'cmd.exe',
        LOCAL_SHELL_KIND_POWERSHELL: 'PowerShell',
    }
    return {
        'shell_kind': requested_kind,
        'terminal_kind': requested_kind,
        'terminal_label': labels[requested_kind],
        'shell_command': [shell_paths[requested_kind]],
        'shell_display': shell_paths[requested_kind],
    }, None

def get_default_local_shell_config():
    if is_wsl():
        return get_wsl_local_shell_config(LOCAL_SHELL_KIND_BASH)
    if sys.platform.startswith('win'):
        shell = get_windows_shell_path()
        terminal_kind = get_windows_shell_kind(shell)
        return {
            'shell_kind': terminal_kind,
            'terminal_kind': terminal_kind,
            'terminal_label': get_shell_label(terminal_kind),
            'shell_command': shell,
            'shell_display': shell,
        }, None

    shell = os.environ.get('SHELL') or '/bin/sh'
    terminal_kind = get_shell_kind(shell)
    return {
        'shell_kind': terminal_kind,
        'terminal_kind': terminal_kind,
        'terminal_label': get_shell_label(terminal_kind),
        'shell_command': [shell],
        'shell_display': shell,
    }, None

def get_local_shell_config(shell_kind=None):
    if is_wsl():
        return get_wsl_local_shell_config(shell_kind)
    if isinstance(shell_kind, str) and shell_kind.strip():
        return None, {
            'message': 'Local Shell selection is only available on WSL.',
            'error_code': 'local_shell_kind_not_supported',
        }
    return get_default_local_shell_config()

class TerminalBridge:
    connection_type = None
    terminal_kind = None
    terminal_label = None

    def __init__(self, owner_session, terminal_id):
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
        return build_terminal_metadata(
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
                agent_transcript_store.append_terminal_output(
                    self.owner_session,
                    self.terminal_id,
                    payload.get('data'),
                )
                self.output_condition.notify_all()
        for sid in list(self.attached_sids):
            socketio.emit('ssh_output', payload, room=sid)

    def _remember_terminal_payload(self, payload):
        data = payload.get('data')
        if not isinstance(data, str) or not data:
            return
        payload_size = len(data.encode('utf-8', errors='ignore'))
        self.replay_buffer.append(dict(payload))
        self.replay_buffer_bytes += payload_size
        while (
            len(self.replay_buffer) > MAX_TERMINAL_REPLAY_EVENTS
            or self.replay_buffer_bytes > MAX_TERMINAL_REPLAY_BYTES
        ):
            removed = self.replay_buffer.popleft()
            removed_data = removed.get('data', '')
            self.replay_buffer_bytes -= len(removed_data.encode('utf-8', errors='ignore'))

    def replay_to(self, sid):
        for payload in list(self.replay_buffer):
            socketio.emit('ssh_output', payload, room=sid)

    def read_loop(self):
        raise NotImplementedError

    def write(self, data):
        raise NotImplementedError

    def resize(self, cols, rows):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

class SSHBridge(TerminalBridge):
    connection_type = CONNECTION_TYPE_SSH
    terminal_kind = 'ssh'
    terminal_label = 'SSH'

    def __init__(self, sid, terminal_id=TERMINAL_ID_MAIN):
        super().__init__(sid, terminal_id)
        self.ssh = None
        self._reset_ssh_client()
        self.channel = None

    def _reset_ssh_client(self, trust_unknown_host=False):
        paramiko_module = get_paramiko()
        if self.ssh:
            self.ssh.close()
        self.ssh = paramiko_module.SSHClient()
        self.ssh.load_system_host_keys()
        if trust_unknown_host:
            self.ssh.set_missing_host_key_policy(paramiko_module.AutoAddPolicy())
        else:
            self.ssh.set_missing_host_key_policy(paramiko_module.RejectPolicy())

    @staticmethod
    def _is_local_target(host):
        if not host:
            return False
        normalized = host.strip().lower()
        return normalized in {'127.0.0.1', 'localhost', '::1'}

    @staticmethod
    def _iter_local_private_key_files():
        ssh_dir = Path.home() / '.ssh'
        key_names = (
            'id_ed25519',
            'id_ecdsa',
            'id_rsa',
            'id_dsa',
            'id_ed25519_sk',
            'id_ecdsa_sk',
        )
        for key_name in key_names:
            key_path = ssh_dir / key_name
            if key_path.is_file():
                yield key_path

    @staticmethod
    def _iter_local_public_key_files():
        ssh_dir = Path.home() / '.ssh'
        key_names = (
            'id_ed25519.pub',
            'id_ecdsa.pub',
            'id_rsa.pub',
            'id_dsa.pub',
            'id_ed25519_sk.pub',
            'id_ecdsa_sk.pub',
        )
        for key_name in key_names:
            key_path = ssh_dir / key_name
            if key_path.is_file():
                yield key_path

    @staticmethod
    def _parse_public_key_line(line):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            return None

        parts = stripped.split()
        for index in range(len(parts) - 1):
            key_type = parts[index]
            key_body = parts[index + 1]
            if key_type not in LOCAL_PUBLIC_KEY_TYPES:
                continue
            try:
                base64.b64decode(key_body.encode('ascii'), validate=True)
            except Exception:
                continue
            return {
                'key_type': key_type,
                'key_body': key_body,
                'line': stripped,
            }
        return None

    def _get_local_public_key_entries(self):
        entries = []
        for key_path in self._iter_local_public_key_files():
            try:
                line = key_path.read_text(encoding='utf-8').strip()
            except OSError:
                continue
            parsed = self._parse_public_key_line(line)
            if parsed:
                parsed['path'] = key_path
                entries.append(parsed)
        return entries

    def _get_authorized_keys_path(self):
        return Path.home() / '.ssh' / 'authorized_keys'

    def _read_authorized_key_fingerprints(self):
        authorized_keys_path = self._get_authorized_keys_path()
        fingerprints = set()
        if not authorized_keys_path.is_file():
            return fingerprints

        try:
            lines = authorized_keys_path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return fingerprints

        for line in lines:
            parsed = self._parse_public_key_line(line)
            if parsed:
                fingerprints.add((parsed['key_type'], parsed['key_body']))
        return fingerprints

    def _get_missing_local_public_keys(self):
        authorized_fingerprints = self._read_authorized_key_fingerprints()
        missing_entries = []
        for entry in self._get_local_public_key_entries():
            fingerprint = (entry['key_type'], entry['key_body'])
            if fingerprint not in authorized_fingerprints:
                missing_entries.append(entry)
        return missing_entries

    def _can_offer_local_key_setup(self, user):
        availability = self._get_local_key_setup_availability(user)
        return availability['can_offer']

    def _get_local_key_setup_availability(self, user):
        current_user = getpass.getuser()
        if user != current_user:
            return {
                'can_offer': False,
                'reason': 'Automatic localhost key setup is only available for the current local user.',
                'error_code': 'localhost_key_setup_unsupported_user',
            }

        if os.name == 'nt':
            return {
                'can_offer': False,
                'reason': (
                    'Automatic localhost key setup is not supported on native Windows yet. '
                    'Windows OpenSSH may require a different authorized keys file, such as '
                    '%USERPROFILE%\\.ssh\\authorized_keys for a regular user or '
                    'C:\\ProgramData\\ssh\\administrators_authorized_keys for an administrator '
                    'account. Please add your public key manually, then try again.'
                ),
                'error_code': 'localhost_key_setup_unsupported_windows',
            }

        return {'can_offer': True}

    def _append_public_key_entry_to_authorized_keys(self, entry):
        fingerprint = (entry['key_type'], entry['key_body'])
        if fingerprint in self._read_authorized_key_fingerprints():
            return False, {
                'status': 'already_configured',
                'message': 'Your local public key is already present in ~/.ssh/authorized_keys.',
            }

        ssh_dir = Path.home() / '.ssh'
        authorized_keys_path = self._get_authorized_keys_path()

        try:
            ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(ssh_dir, 0o700)

            existing_text = ''
            if authorized_keys_path.exists():
                existing_text = authorized_keys_path.read_text(encoding='utf-8')

            with authorized_keys_path.open('a', encoding='utf-8') as authorized_keys_file:
                if existing_text and not existing_text.endswith('\n'):
                    authorized_keys_file.write('\n')
                authorized_keys_file.write(entry['line'] + '\n')
            os.chmod(authorized_keys_path, 0o600)
        except OSError as exc:
            return False, {
                'status': 'failed',
                'message': f'Failed to update ~/.ssh/authorized_keys: {exc}',
            }

        return True, {
            'status': 'success',
            'message': (
                f'Added {entry["path"].name} to ~/.ssh/authorized_keys. '
                'Try connecting to localhost again.'
            ),
        }

    def _append_local_public_key_to_authorized_keys(self):
        missing_entries = self._get_missing_local_public_keys()
        if not missing_entries:
            return False, {
                'status': 'already_configured',
                'message': 'Your local public key is already present in ~/.ssh/authorized_keys.',
            }

        return self._append_public_key_entry_to_authorized_keys(missing_entries[0])

    def _build_local_key_setup_hint(self):
        message = (
            'Local public key authentication for localhost failed, and your local public key was not '
            'found in ~/.ssh/authorized_keys on this machine. Add your public key to '
            '~/.ssh/authorized_keys, or enter your SSH password and try again.'
        )
        question = (
            'Do you want to add your public key to ~/.ssh/authorized_keys?'
        )
        return {
            'message': message,
            'error_code': 'localhost_key_not_authorized',
            'action_type': 'offer_localhost_key_setup',
            'action_message': message,
            'action_question': question,
        }

    @staticmethod
    def _build_manual_local_key_setup_hint(reason, error_code):
        return {
            'message': reason,
            'error_code': error_code,
        }

    @staticmethod
    def _load_private_key(key_path, passphrase=None):
        paramiko_module = get_paramiko()
        key_types = []
        for key_type_name in ('Ed25519Key', 'ECDSAKey', 'RSAKey', 'DSSKey'):
            key_type = getattr(paramiko_module, key_type_name, None)
            if key_type is not None:
                key_types.append(key_type)
        last_error = None
        for key_type in key_types:
            try:
                return key_type.from_private_key_file(str(key_path), password=passphrase)
            except paramiko_module.PasswordRequiredException:
                raise
            except paramiko_module.SSHException as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise paramiko_module.SSHException(f"Unsupported key format: {key_path}")

    def _connect_with_local_keys(self, host, port, user, password):
        paramiko_module = get_paramiko()
        auth_errors = []
        passphrase = password or None

        try:
            self._reset_ssh_client(trust_unknown_host=True)
            self.ssh.connect(
                host,
                port=int(port),
                username=user,
                password=None,
                timeout=15,
                allow_agent=True,
                look_for_keys=True,
            )
            print(f"[+] Local key auth succeeded via agent/default keys for {self.sid}")
            return True, None
        except paramiko_module.AuthenticationException as exc:
            auth_errors.append(f"agent/default keys: {exc}")
        except Exception as exc:
            auth_errors.append(f"agent/default keys: {exc}")

        for key_path in self._iter_local_private_key_files():
            try:
                pkey = self._load_private_key(key_path, passphrase=passphrase)
            except paramiko_module.PasswordRequiredException:
                auth_errors.append(f"{key_path.name}: passphrase required")
                continue
            except Exception as exc:
                auth_errors.append(f"{key_path.name}: {exc}")
                continue

            try:
                self._reset_ssh_client(trust_unknown_host=True)
                self.ssh.connect(
                    host,
                    port=int(port),
                    username=user,
                    password=None,
                    pkey=pkey,
                    timeout=15,
                    allow_agent=False,
                    look_for_keys=False,
                )
                print(f"[+] Local key auth succeeded via {key_path.name} for {self.sid}")
                return True, None
            except Exception as exc:
                auth_errors.append(f"{key_path.name}: {exc}")

        return False, '; '.join(auth_errors)

    def connect(self, host, port, user, password=None, cols=80, rows=24):
        paramiko_module = get_paramiko()
        try:
            pwd = password if password else ""
            print(f"[*] Attempting SSH connection for {user!r} at {host!r}:{port}...")

            is_localhost = self._is_local_target(host)
            if is_localhost and not pwd:
                success, key_error = self._connect_with_local_keys(host, port, user, None)
                if not success:
                    setup_availability = self._get_local_key_setup_availability(user)
                    if setup_availability['can_offer']:
                        missing_local_keys = self._get_missing_local_public_keys()
                        if missing_local_keys:
                            hint = self._build_local_key_setup_hint()
                            print(f"[*] Local key auth failed for {self.sid}; offering localhost key setup.")
                            return False, hint
                    elif setup_availability.get('reason'):
                        print(f"[*] Local key auth failed for {self.sid}; auto setup unavailable.")
                        return False, self._build_manual_local_key_setup_hint(
                            setup_availability['reason'],
                            setup_availability.get('error_code'),
                        )
                    raise paramiko_module.AuthenticationException(
                        f"Local public key auth failed: {key_error or 'no usable local key found'}"
                    )
            else:
                self._reset_ssh_client(trust_unknown_host=is_localhost)
                self.ssh.connect(
                    host,
                    port=int(port),
                    username=user,
                    password=pwd,
                    timeout=15,
                    allow_agent=False,
                    look_for_keys=False,
                )

            self.channel = self.ssh.invoke_shell(term=SSH_TERM, width=cols, height=rows)
            self.channel.setblocking(0)
            print(f"[+] SSH connection established for {self.sid}")
            return True, None
        except Exception as e:
            error_msg = str(e)
            print(f"[!] SSH Connection Error: {error_msg}")
            return False, {'message': error_msg}

    def read_loop(self):
        print(f"[*] Starting SSH read loop for {self.sid}")
        while True:
            # Short sleep to prevent CPU hogging while allowing high responsiveness
            socketio.sleep(0.01)
            if not self.channel:
                break
            
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096).decode('utf-8', errors='ignore')
                    if data:
                        self.emit_output({
                            'message_type': 'terminal',
                            'data': data,
                        })
                
                if self.channel.exit_status_ready():
                    print(f"[*] SSH session exited for {self.sid}")
                    self.emit_output({
                        'message_type': 'ssh_closed',
                        'message': 'SSH session closed.',
                    })
                    break
            except Exception as e:
                if self.closing:
                    break
                print(f"[!] Read error: {e}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'SSH connection closed due to a read error.',
                    'error_code': 'ssh_read_error',
                })
                break
        print(f"[*] SSH read loop terminated for {self.sid}")
        unregister_terminal_bridge(self.owner_session, self.terminal_id, self)

    def write(self, data):
        if self.channel:
            try:
                self.channel.send(data)
            except Exception as e:
                print(f"[!] Write error: {e}")

    def resize(self, cols, rows):
        if self.channel:
            try:
                self.channel.resize_pty(width=cols, height=rows)
            except Exception as e:
                print(f"[!] Resize error: {e}")

    def close(self):
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
            self.channel = None
        if self.ssh:
            try:
                self.ssh.close()
            except Exception:
                pass

class LocalShellBridge(TerminalBridge):
    connection_type = CONNECTION_TYPE_LOCAL_SHELL

    def __init__(self, sid, terminal_id=TERMINAL_ID_MAIN, shell_config=None):
        super().__init__(sid, terminal_id)
        self.process = None
        shell_config = shell_config or get_default_local_shell_config()[0]
        self.shell = shell_config['shell_display']
        self.shell_command = shell_config['shell_command']
        self.terminal_kind = shell_config['terminal_kind']
        self.terminal_label = shell_config['terminal_label']

    def connect(self, cols=80, rows=24):
        if sys.platform.startswith('win'):
            return self._connect_windows(cols, rows)
        if PtyProcessUnicode is None:
            return False, {
                'message': 'Local Shell requires ptyprocess. Re-run the launcher with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = dict(os.environ)
            env['TERM'] = SSH_TERM
            cwd = str(Path.home())
            self.process = PtyProcessUnicode.spawn(
                self.shell_command,
                cwd=cwd,
                env=env,
                dimensions=(rows, cols),
            )
            print(f"[+] Local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            print(f"[!] Local shell start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'local_shell_start_failed'}

    def _connect_windows(self, cols, rows):
        if WinPtyProcess is None:
            return False, {
                'message': 'Local Shell on Windows requires pywinpty. Re-run run.bat with --force to install dependencies.',
                'error_code': 'local_shell_dependency_missing',
            }

        try:
            env = dict(os.environ)
            env['TERM'] = SSH_TERM
            cwd = str(Path.home())
            self.process = self._spawn_windows_process(cols, rows, cwd, env)
            self.resize(cols, rows)
            print(f"[+] Windows local shell started for {self.sid}: {self.shell}")
            return True, None
        except Exception as exc:
            print(f"[!] Windows local shell start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'local_shell_start_failed'}

    def _spawn_windows_process(self, cols, rows, cwd, env):
        spawn_attempts = (
            lambda: WinPtyProcess.spawn(self.shell, cwd=cwd, env=env, dimensions=(rows, cols)),
            lambda: WinPtyProcess.spawn(self.shell, cwd=cwd, env=env),
            lambda: WinPtyProcess.spawn(self.shell, dimensions=(rows, cols)),
            lambda: WinPtyProcess.spawn(self.shell),
        )
        last_error = None
        for spawn in spawn_attempts:
            try:
                return spawn()
            except TypeError as exc:
                last_error = exc
        raise last_error

    def read_loop(self):
        print(f"[*] Starting local shell read loop for {self.sid}")
        while True:
            socketio.sleep(0.01)
            if not self.process:
                break

            if sys.platform.startswith('win'):
                if not self._read_windows_once():
                    break
                continue

            try:
                readable, _, _ = select.select([self.process.fd], [], [], 0)
                if not readable:
                    if self.closing:
                        break
                    if not self.process.isalive():
                        self.emit_output({
                            'message_type': 'ssh_closed',
                            'message': 'Local shell session closed.',
                        })
                        break
                    continue

                data = self.process.read(size=4096)
                if data:
                    self.emit_output({
                        'message_type': 'terminal',
                        'data': data,
                    })
            except EOFError:
                if self.closing:
                    break
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell session closed.',
                })
                break
            except Exception as exc:
                if self.closing:
                    break
                print(f"[!] Local shell read error: {exc}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell closed due to a read error.',
                    'error_code': 'local_shell_read_error',
                })
                break

        print(f"[*] Local shell read loop terminated for {self.sid}")
        unregister_terminal_bridge(self.owner_session, self.terminal_id, self)

    def _read_windows_once(self):
        try:
            data = self.process.read(4096)
            if data:
                self.emit_output({
                    'message_type': 'terminal',
                    'data': data,
                })
            if not self.process.isalive():
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'Local shell session closed.',
                })
                return False
            return True
        except EOFError:
            if self.closing:
                return False
            self.emit_output({
                'message_type': 'ssh_closed',
                'message': 'Local shell session closed.',
            })
            return False
        except Exception as exc:
            if self.closing:
                return False
            print(f"[!] Windows local shell read error: {exc}")
            self.emit_output({
                'message_type': 'ssh_closed',
                'message': 'Local shell closed due to a read error.',
                'error_code': 'local_shell_read_error',
            })
            return False

    def write(self, data):
        if self.process:
            try:
                self.process.write(data)
            except Exception as exc:
                print(f"[!] Local shell write error: {exc}")

    def resize(self, cols, rows):
        if self.process:
            try:
                if sys.platform.startswith('win'):
                    if hasattr(self.process, 'set_size'):
                        self.process.set_size(cols, rows)
                    elif hasattr(self.process, 'setwinsize'):
                        self.process.setwinsize(rows, cols)
                    elif hasattr(self.process, 'resize'):
                        self.process.resize(cols, rows)
                else:
                    self.process.setwinsize(rows, cols)
            except Exception as exc:
                print(f"[!] Local shell resize error: {exc}")

    def close(self):
        if not self.process:
            return
        try:
            if sys.platform.startswith('win') and hasattr(self.process, 'terminate'):
                self.process.terminate()
            elif sys.platform.startswith('win') and hasattr(self.process, 'kill'):
                self.process.kill()
            else:
                self.process.close(force=True)
        except TypeError:
            self.process.close()
        except Exception:
            pass
        self.process = None

class UARTBridge(TerminalBridge):
    connection_type = CONNECTION_TYPE_UART
    terminal_kind = 'uart'

    def __init__(self, sid, terminal_id, port_info, baud_rate):
        super().__init__(sid, terminal_id)
        self.serial = None
        self.device = port_info['device']
        self.baud_rate = baud_rate
        self.terminal_label = f'UART {port_info.get("label") or self.device}'

    def connect(self, cols=80, rows=24):
        if is_wsl() and is_windows_com_device(self.device):
            return self._connect_wsl_windows_com()

        try:
            serial_lib, _ = get_serial_modules()
        except Exception:
            return False, {
                'message': 'UART requires pyserial. Re-run the launcher with --force to install dependencies.',
                'error_code': 'uart_dependency_missing',
            }

        try:
            self.serial = serial_lib.Serial(
                port=self.device,
                baudrate=self.baud_rate,
                timeout=0,
                write_timeout=1,
            )
            print(f"[+] UART opened for {self.sid}: {self.device} @ {self.baud_rate}")
            return True, None
        except serial_lib.SerialException as exc:
            print(f"[!] UART open error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_open_failed'}
        except PermissionError as exc:
            print(f"[!] UART permission error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_permission_denied'}
        except Exception as exc:
            print(f"[!] UART start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_open_failed'}

    def _connect_wsl_windows_com(self):
        helper_python, helper_error = find_windows_python_with_pyserial()
        if not helper_python:
            return False, {
                'message': helper_error or 'WSL Windows COM access requires Windows Python with pyserial installed.',
                'error_code': 'uart_windows_python_unavailable',
            }

        try:
            self.serial = subprocess.Popen(
                [
                    helper_python,
                    '-u',
                    '-c',
                    WINDOWS_SERIAL_HELPER,
                    self.device,
                    str(self.baud_rate),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as exc:
            print(f"[!] Windows UART helper start error: {exc}")
            return False, {'message': str(exc), 'error_code': 'uart_helper_start_failed'}

        status = self._read_helper_status(timeout_seconds=5)
        if status.get('event') == 'ready':
            print(f"[+] Windows UART helper opened for {self.sid}: {self.device} @ {self.baud_rate}")
            return True, None

        message = status.get('message') or 'Windows UART helper did not become ready.'
        close_process(self.serial)
        self.serial = None
        return False, {'message': message, 'error_code': 'uart_open_failed'}

    def _read_helper_status(self, timeout_seconds):
        if not self.serial or not self.serial.stderr:
            return {'event': 'error', 'message': 'Windows UART helper stderr is unavailable.'}

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            timeout = max(0, deadline - time.time())
            try:
                readable, _, _ = select.select([self.serial.stderr], [], [], timeout)
            except Exception as exc:
                return {'event': 'error', 'message': str(exc)}
            if not readable:
                continue
            line = self.serial.stderr.readline()
            if not line:
                break
            try:
                data = json.loads(line.decode('utf-8', errors='replace'))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get('event') in {'ready', 'error'}:
                return data

        if self.serial and self.serial.poll() is not None:
            return {'event': 'error', 'message': 'Windows UART helper exited before opening the port.'}
        return {'event': 'error', 'message': 'Timed out while opening Windows UART port.'}

    def read_loop(self):
        print(f"[*] Starting UART read loop for {self.sid}")
        while True:
            socketio.sleep(0.01)
            if not self.serial:
                break

            try:
                if isinstance(self.serial, subprocess.Popen):
                    data = self._read_windows_helper_once()
                else:
                    waiting = self.serial.in_waiting
                    data = self.serial.read(waiting or 1)
                if data:
                    self.emit_output({
                        'message_type': 'terminal',
                        'data': data.decode('utf-8', errors='replace'),
                    })
                elif isinstance(self.serial, subprocess.Popen) and self.serial.poll() is not None:
                    self.emit_output({
                        'message_type': 'ssh_closed',
                        'message': 'UART helper exited.',
                        'error_code': 'uart_helper_exited',
                    })
                    break
            except Exception as exc:
                if self.closing:
                    break
                print(f"[!] UART read error: {exc}")
                self.emit_output({
                    'message_type': 'ssh_closed',
                    'message': 'UART connection closed due to a read error.',
                    'error_code': 'uart_read_error',
                })
                break

        print(f"[*] UART read loop terminated for {self.sid}")
        unregister_terminal_bridge(self.owner_session, self.terminal_id, self)

    def _read_windows_helper_once(self):
        if not self.serial or not self.serial.stdout:
            return b''
        readable, _, _ = select.select([self.serial.stdout], [], [], 0)
        if not readable:
            return b''
        return self.serial.stdout.read(4096)

    def write(self, data):
        if not self.serial:
            return
        try:
            encoded = data.encode('utf-8', errors='replace')
            if isinstance(self.serial, subprocess.Popen):
                if self.serial.stdin:
                    self.serial.stdin.write(encoded)
                    self.serial.stdin.flush()
            else:
                self.serial.write(encoded)
        except Exception as exc:
            print(f"[!] UART write error: {exc}")

    def resize(self, cols, rows):
        return

    def close(self):
        if not self.serial:
            return
        try:
            if isinstance(self.serial, subprocess.Popen):
                close_process(self.serial)
            else:
                self.serial.close()
        except Exception:
            pass
        self.serial = None

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

class SSHBackendPlugin(TerminalBackendPlugin):
    connection_type = CONNECTION_TYPE_SSH
    label = 'SSH'

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        host = data.get('host', SSH_HOST)
        if not isinstance(host, str):
            return None, 'Host must be a string.'
        host = host.strip()
        if not host or len(host) > MAX_HOST_LENGTH:
            return None, 'Host is empty or too long.'
        if has_control_chars(host):
            return None, 'Host contains invalid control characters.'

        try:
            port = int(data.get('port', SSH_PORT))
        except (TypeError, ValueError):
            return None, 'Port must be a number.'
        if port < 1 or port > 65535:
            return None, 'Port must be between 1 and 65535.'

        user = data.get('username', SSH_USER)
        if not isinstance(user, str):
            return None, 'Username must be a string.'
        user = user.strip()
        if not user or len(user) > MAX_USERNAME_LENGTH:
            return None, 'Username is empty or too long.'
        if has_control_chars(user):
            return None, 'Username contains invalid control characters.'

        password = data.get('password') or ''
        if not isinstance(password, str):
            return None, 'Password must be a string.'
        if len(password.encode('utf-8', errors='ignore')) > MAX_PASSWORD_BYTES:
            return None, 'Password is too long.'

        return {
            'host': host,
            'port': port,
            'username': user,
            'password': password,
        }, None

    def create_bridge(self, session_token, terminal_id, payload):
        return SSHBridge(session_token, terminal_id)

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(
            payload['host'],
            payload['port'],
            payload['username'],
            payload['password'],
            cols=cols,
            rows=rows,
        )

    def build_connection_failure(self, sid, bridge, payload, result):
        failure = super().build_connection_failure(sid, bridge, payload, result)
        action_type = None
        action_message = None
        action_question = None

        if isinstance(result, dict):
            action_type = result.get('action_type')
            action_message = result.get('action_message')
            action_question = result.get('action_question')
        if action_type not in ALLOWED_CONNECTION_ACTION_TYPES:
            action_type = None
            action_message = None
            action_question = None

        action_id = None
        if action_type == 'offer_localhost_key_setup':
            missing_entries = bridge._get_missing_local_public_keys()
            if missing_entries:
                action_id = secrets.token_urlsafe(16)
                pending_localhost_key_setups[sid] = {
                    'action_id': action_id,
                    'host': payload['host'],
                    'port': payload['port'],
                    'username': payload['username'],
                    'terminal_id': payload['terminal_id'],
                    'key_entry': missing_entries[0],
                    'expires_at': time.time() + LOCALHOST_KEY_SETUP_TTL_SECONDS,
                }
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

class LocalShellBackendPlugin(TerminalBackendPlugin):
    connection_type = CONNECTION_TYPE_LOCAL_SHELL
    label = 'Local Shell'

    def build_policy_option(self, browser_authorized=False):
        client_ip = get_request_client_ip()
        allowed = is_local_shell_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        option = {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
        }
        if is_wsl():
            option['shell_options'] = get_wsl_local_shell_options()
            option['default_shell_kind'] = LOCAL_SHELL_KIND_BASH
        return option

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        if not is_local_shell_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'Local Shell is not available for this client.',
                'error_code': 'local_shell_unavailable_for_client',
            }
        shell_config, shell_error = get_local_shell_config(data.get('local_shell_kind'))
        if shell_error:
            return None, shell_error
        return {'local_shell_config': shell_config}, None

    def create_bridge(self, session_token, terminal_id, payload):
        return LocalShellBridge(session_token, terminal_id, shell_config=payload.get('local_shell_config'))

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols=cols, rows=rows)

class UARTBackendPlugin(TerminalBackendPlugin):
    connection_type = CONNECTION_TYPE_UART
    label = 'UART'

    def build_policy_option(self, browser_authorized=False):
        client_ip = get_request_client_ip()
        allowed = is_uart_allowed_for_client(client_ip, browser_authorized=browser_authorized)
        return {
            'connection_type': self.connection_type,
            'label': self.label,
            'allowed': allowed,
            'authorization_available': not allowed,
            'browser_authorized': bool(browser_authorized),
            'available_ports': detect_serial_ports() if allowed else [],
            'default_baud_rate': DEFAULT_UART_BAUD_RATE,
            'baud_rates': UART_BAUD_RATES,
        }

    def validate_start_payload(self, data, terminal_id, client_ip, browser_authorized=False):
        if not is_uart_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'UART is not available for this client.',
                'error_code': 'uart_unavailable_for_client',
            }

        port_info = get_detected_serial_port(data.get('serial_port'))
        if not port_info:
            return None, {
                'message': 'Select an available UART port.',
                'error_code': 'uart_port_unavailable',
            }

        try:
            baud_rate = int(data.get('baud_rate', DEFAULT_UART_BAUD_RATE))
        except (TypeError, ValueError):
            return None, {
                'message': 'UART baud rate must be a number.',
                'error_code': 'uart_invalid_baud_rate',
            }
        if baud_rate < MIN_UART_BAUD_RATE or baud_rate > MAX_UART_BAUD_RATE:
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
        return UARTBridge(
            session_token,
            terminal_id,
            payload['serial_port_info'],
            payload['baud_rate'],
        )

    def connect_bridge(self, bridge, payload, cols, rows):
        return bridge.connect(cols=cols, rows=rows)

class TerminalBackendRegistry:
    def __init__(self, plugins):
        self._plugins = {}
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
        if allowed_connections.get(CONNECTION_TYPE_LOCAL_SHELL):
            return CONNECTION_TYPE_LOCAL_SHELL
        if allowed_connections.get(CONNECTION_TYPE_SSH):
            return CONNECTION_TYPE_SSH
        for connection_type, allowed in allowed_connections.items():
            if allowed:
                return connection_type
        return CONNECTION_TYPE_SSH

TERMINAL_BACKEND_REGISTRY = TerminalBackendRegistry([
    SSHBackendPlugin(),
    LocalShellBackendPlugin(),
    UARTBackendPlugin(),
])

bridges = {}
pending_localhost_key_setups = {}
active_sessions = {}
socket_session_tokens = {}
socket_client_ips = {}
socket_browser_identities = {}
socket_browser_authorized = {}
socket_browser_auth_challenges = {}
agent_states = {}
agent_session_ids = {}
agent_viewer_ids = {}
agent_lock = threading.RLock()
external_agent_lock = threading.RLock()
session_cleanup_task_started = False

class AgentAuditStore:
    def __init__(self):
        self._entries = {}

    def append(self, session_token, terminal_id, event_type, **fields):
        if not session_token or not terminal_id or not isinstance(event_type, str):
            return None
        key = (session_token, terminal_id)
        now = time.time()
        entry = {
            'event_type': event_type,
            'recorded_at': now,
            'session_id': get_agent_session_id(session_token),
            'terminal_id': terminal_id,
        }
        for field, value in fields.items():
            if value is not None:
                entry[field] = value
        bucket = self._entries.setdefault(key, deque(maxlen=AGENT_AUDIT_EVENTS))
        bucket.append(entry)
        self._trim_bucket(key, now)
        return dict(entry)

    def get_recent(self, session_token, terminal_id):
        key = (session_token, terminal_id)
        bucket = self._entries.get(key)
        if not bucket:
            return []
        self._trim_bucket(key, time.time())
        return [dict(entry) for entry in bucket]

    def discard(self, session_token, terminal_id=None):
        if terminal_id is not None:
            self._entries.pop((session_token, terminal_id), None)
            return
        for key in [
            key for key in self._entries
            if key[0] == session_token
        ]:
            self._entries.pop(key, None)

    def clear(self):
        self._entries.clear()

    def _trim_bucket(self, key, now):
        bucket = self._entries.get(key)
        if not bucket:
            return
        expires_before = now - AGENT_AUDIT_TTL_SECONDS
        while bucket and bucket[0].get('recorded_at', 0) < expires_before:
            bucket.popleft()
        if not bucket:
            self._entries.pop(key, None)

agent_audit_store = AgentAuditStore()

class AgentTranscriptStore:
    def __init__(self):
        self._entries = {}

    def append_terminal_output(self, session_token, terminal_id, data):
        if not session_token or not terminal_id or not isinstance(data, str):
            return
        sanitized = sanitize_agent_transcript_text(data)
        if not sanitized:
            return
        encoded = sanitized.encode('utf-8', errors='ignore')
        if len(encoded) > AGENT_TRANSCRIPT_MAX_EVENT_BYTES:
            encoded = encoded[:AGENT_TRANSCRIPT_MAX_EVENT_BYTES]
            sanitized = encoded.decode('utf-8', errors='ignore')
        key = (session_token, terminal_id)
        now = time.time()
        bucket = self._entries.setdefault(key, {'events': deque(), 'bytes': 0})
        event = {
            'captured_at': now,
            'data': sanitized,
            'byte_length': len(encoded),
            'untrusted': True,
        }
        bucket['events'].append(event)
        bucket['bytes'] += event['byte_length']
        self._trim_bucket(bucket, now)

    def get_recent(self, session_token, terminal_id):
        key = (session_token, terminal_id)
        bucket = self._entries.get(key)
        if not bucket:
            return []
        self._trim_bucket(bucket, time.time())
        return [dict(event) for event in bucket['events']]

    def discard(self, session_token, terminal_id=None):
        if terminal_id is not None:
            self._entries.pop((session_token, terminal_id), None)
            return
        for key in [
            key for key in self._entries
            if key[0] == session_token
        ]:
            self._entries.pop(key, None)

    def clear(self):
        self._entries.clear()

    def _trim_bucket(self, bucket, now):
        expires_before = now - AGENT_TRANSCRIPT_TTL_SECONDS
        events = bucket['events']
        while events and (
            len(events) > AGENT_TRANSCRIPT_MAX_EVENTS
            or bucket['bytes'] > AGENT_TRANSCRIPT_MAX_BYTES
            or events[0]['captured_at'] < expires_before
        ):
            removed = events.popleft()
            bucket['bytes'] -= removed['byte_length']

def strip_terminal_display_text(value):
    value = ANSI_OSC_PATTERN.sub('', value)
    value = ANSI_CSI_PATTERN.sub('', value)
    value = ANSI_ESCAPE_PATTERN.sub('', value)
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    value = AGENT_TRANSCRIPT_CONTROL_PATTERN.sub('', value)
    return value

def sanitize_agent_transcript_text(value):
    return strip_terminal_display_text(value)

agent_transcript_store = AgentTranscriptStore()

class AgentUserInputMetadataStore:
    def __init__(self):
        self._entries = {}

    def append_input(self, session_token, terminal_id, data, privacy_state=AGENT_PRIVACY_NORMAL):
        if not session_token or not terminal_id or not isinstance(data, str):
            return
        metadata = summarize_agent_user_input_metadata(terminal_id, data, privacy_state)
        key = (session_token, terminal_id)
        now = time.time()
        metadata['timestamp'] = now
        bucket = self._entries.setdefault(key, deque())
        bucket.append(metadata)
        self._trim_bucket(bucket, now)

    def get_recent(self, session_token, terminal_id):
        key = (session_token, terminal_id)
        bucket = self._entries.get(key)
        if not bucket:
            return []
        self._trim_bucket(bucket, time.time())
        return [dict(event) for event in bucket]

    def discard(self, session_token, terminal_id=None):
        if terminal_id is not None:
            self._entries.pop((session_token, terminal_id), None)
            return
        for key in [
            key for key in self._entries
            if key[0] == session_token
        ]:
            self._entries.pop(key, None)

    def clear(self):
        self._entries.clear()

    def _trim_bucket(self, bucket, now):
        expires_before = now - AGENT_USER_INPUT_METADATA_TTL_SECONDS
        while bucket and (
            len(bucket) > AGENT_USER_INPUT_METADATA_MAX_EVENTS
            or bucket[0]['timestamp'] < expires_before
        ):
            bucket.popleft()

def summarize_agent_user_input_metadata(terminal_id, value, privacy_state=AGENT_PRIVACY_NORMAL):
    encoded = value.encode('utf-8', errors='ignore')
    contains_control_chars = has_agent_control_chars(value)
    if privacy_state not in AGENT_PRIVACY_STATES:
        privacy_state = AGENT_PRIVACY_NORMAL
    metadata = {
        'terminal_id': terminal_id,
        'byte_length': len(encoded),
        'line_count': value.count('\n') + (1 if value and not value.endswith('\n') else 0),
        'contains_control_chars': contains_control_chars,
        'privacy_state': privacy_state,
    }
    if privacy_state != AGENT_PRIVACY_NORMAL:
        metadata['redacted'] = True
    elif value and not contains_control_chars:
        preview = escape_agent_preview(value)
        if len(preview) > AGENT_USER_INPUT_PREVIEW_CHARS:
            preview = preview[:AGENT_USER_INPUT_PREVIEW_CHARS] + '...'
        metadata['escaped_preview'] = preview
    return metadata

agent_user_input_metadata_store = AgentUserInputMetadataStore()

class AgentViewportSnapshotStore:
    def __init__(self):
        self._entries = {}

    def put(self, session_token, terminal_id, sid, snapshot):
        if not session_token or not terminal_id or not sid:
            return None, AGENT_ERROR_SNAPSHOT_INVALID
        key = (session_token, terminal_id, sid)
        now = time.time()
        existing = self._entries.get(key)
        snapshot_seq = snapshot.get('snapshot_seq')
        if existing and isinstance(snapshot_seq, int) and snapshot_seq <= existing.get('snapshot_seq', -1):
            stale = dict(snapshot)
            stale['status'] = 'stale'
            stale['stored_at'] = now
            return stale, AGENT_ERROR_SNAPSHOT_STALE
        stored = dict(snapshot)
        stored['stored_at'] = now
        stored['status'] = 'accepted'
        stored['untrusted'] = True
        self._entries[key] = stored
        self._trim(now)
        return dict(stored), None

    def get_latest(self, session_token, terminal_id, sid):
        key = (session_token, terminal_id, sid)
        snapshot = self._entries.get(key)
        if not snapshot:
            return None
        if snapshot.get('stored_at', 0) < time.time() - AGENT_VIEWPORT_SNAPSHOT_TTL_SECONDS:
            self._entries.pop(key, None)
            return None
        return dict(snapshot)

    def discard(self, session_token, terminal_id=None, sid=None):
        for key in [
            key for key in self._entries
            if key[0] == session_token
            and (terminal_id is None or key[1] == terminal_id)
            and (sid is None or key[2] == sid)
        ]:
            self._entries.pop(key, None)

    def clear(self):
        self._entries.clear()

    def _trim(self, now):
        expires_before = now - AGENT_VIEWPORT_SNAPSHOT_TTL_SECONDS
        for key in [
            key for key, snapshot in self._entries.items()
            if snapshot.get('stored_at', 0) < expires_before
        ]:
            self._entries.pop(key, None)

def validate_agent_viewport_snapshot_payload(data):
    if not isinstance(data, dict):
        return None, AGENT_ERROR_SNAPSHOT_INVALID
    terminal_id = validate_terminal_id_payload(data)
    if not terminal_id:
        return None, AGENT_ERROR_SNAPSHOT_INVALID
    try:
        cols = int(data.get('cols'))
        rows = int(data.get('rows'))
        viewport_y = int(data.get('viewport_y'))
        base_y = int(data.get('base_y'))
        snapshot_seq = int(data.get('snapshot_seq'))
        output_seq = int(data.get('output_seq', 0))
    except (TypeError, ValueError):
        return None, AGENT_ERROR_SNAPSHOT_INVALID
    if not (MIN_TERMINAL_COLS <= cols <= MAX_TERMINAL_COLS):
        return None, AGENT_ERROR_SNAPSHOT_INVALID
    if not (MIN_TERMINAL_ROWS <= rows <= MAX_TERMINAL_ROWS):
        return None, AGENT_ERROR_SNAPSHOT_INVALID
    if viewport_y < 0 or base_y < 0 or snapshot_seq < 1 or output_seq < 0:
        return None, AGENT_ERROR_SNAPSHOT_INVALID
    lines = data.get('lines')
    if not isinstance(lines, list) or len(lines) != rows:
        return None, AGENT_ERROR_SNAPSHOT_INVALID
    total_bytes = 0
    normalized_lines = []
    for line in lines:
        if not isinstance(line, str):
            return None, AGENT_ERROR_SNAPSHOT_INVALID
        line_bytes = len(line.encode('utf-8', errors='ignore'))
        if line_bytes > AGENT_VIEWPORT_SNAPSHOT_MAX_LINE_BYTES:
            return None, AGENT_ERROR_SNAPSHOT_TOO_LARGE
        total_bytes += line_bytes
        if total_bytes > AGENT_VIEWPORT_SNAPSHOT_MAX_BYTES:
            return None, AGENT_ERROR_SNAPSHOT_TOO_LARGE
        normalized_lines.append(line)
    return {
        'terminal_id': terminal_id,
        'cols': cols,
        'rows': rows,
        'viewport_y': viewport_y,
        'base_y': base_y,
        'snapshot_seq': snapshot_seq,
        'output_seq': output_seq,
        'captured_at': data.get('captured_at') if isinstance(data.get('captured_at'), str) else None,
        'line_count': len(normalized_lines),
        'byte_length': total_bytes,
        'lines': normalized_lines,
    }, None

agent_viewport_snapshot_store = AgentViewportSnapshotStore()

def parse_agent_viewport_render_wait_ms(value):
    try:
        wait_ms = int(value if value is not None else AGENT_VIEWPORT_RENDER_WAIT_MS)
    except (TypeError, ValueError):
        wait_ms = AGENT_VIEWPORT_RENDER_WAIT_MS
    return max(0, min(wait_ms, AGENT_VIEWPORT_RENDER_MAX_WAIT_MS))

def validate_agent_viewport_render_result_payload(data, expected_request):
    if not isinstance(data, dict) or not isinstance(expected_request, dict):
        return None, AGENT_ERROR_RENDER_INVALID
    if data.get('request_id') != expected_request.get('request_id'):
        return None, AGENT_ERROR_RENDER_STALE
    terminal_id = validate_terminal_id_payload(data)
    if not terminal_id or terminal_id != expected_request.get('terminal_id'):
        return None, AGENT_ERROR_RENDER_INVALID
    render_type = data.get('render_type')
    mime_type = data.get('mime_type')
    if render_type != 'xterm_viewport' or mime_type != 'image/png':
        return None, AGENT_ERROR_RENDER_INVALID
    try:
        cols = int(data.get('cols'))
        rows = int(data.get('rows'))
        pixel_width = int(data.get('pixel_width'))
        pixel_height = int(data.get('pixel_height'))
        output_seq = int(data.get('output_seq', 0))
    except (TypeError, ValueError):
        return None, AGENT_ERROR_RENDER_INVALID
    if not (MIN_TERMINAL_COLS <= cols <= MAX_TERMINAL_COLS):
        return None, AGENT_ERROR_RENDER_INVALID
    if not (MIN_TERMINAL_ROWS <= rows <= MAX_TERMINAL_ROWS):
        return None, AGENT_ERROR_RENDER_INVALID
    if pixel_width <= 0 or pixel_height <= 0:
        return None, AGENT_ERROR_RENDER_INVALID
    if pixel_width * pixel_height > AGENT_VIEWPORT_RENDER_MAX_PIXELS:
        return None, AGENT_ERROR_RENDER_TOO_LARGE
    if output_seq < 0:
        return None, AGENT_ERROR_RENDER_INVALID
    image_base64 = data.get('image_base64')
    if not isinstance(image_base64, str) or not image_base64:
        return None, AGENT_ERROR_RENDER_INVALID
    try:
        image_bytes = base64.b64decode(image_base64.encode('ascii'), validate=True)
    except Exception:
        return None, AGENT_ERROR_RENDER_INVALID
    if not image_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return None, AGENT_ERROR_RENDER_INVALID
    if len(image_bytes) > AGENT_VIEWPORT_RENDER_MAX_IMAGE_BYTES:
        return None, AGENT_ERROR_RENDER_TOO_LARGE
    return {
        'request_id': expected_request.get('request_id'),
        'terminal_id': terminal_id,
        'render_type': render_type,
        'mime_type': mime_type,
        'image_base64': image_base64,
        'image_byte_length': len(image_bytes),
        'cols': cols,
        'rows': rows,
        'pixel_width': pixel_width,
        'pixel_height': pixel_height,
        'output_seq': output_seq,
        'captured_at': data.get('captured_at') if isinstance(data.get('captured_at'), str) else None,
    }, None

class AgentViewportRenderRequestStore:
    def __init__(self):
        self._requests = {}
        self._lock = threading.RLock()

    def create(self, session_token, terminal_id, sid, state, bridge):
        request_id = 'agrv_' + secrets.token_urlsafe(12)
        now = time.time()
        request_payload = {
            'request_id': request_id,
            'terminal_id': terminal_id,
            'render_type': 'xterm_viewport',
            'mime_type': 'image/png',
            'session_id': state.session_id,
            'viewer_id': state.viewer_id,
            'agent_binding_id': state.agent_binding_id,
            'mode_version': state.mode_version,
            'privacy_version': state.privacy_version,
            'cols': bridge.cols,
            'rows': bridge.rows,
            'output_seq': bridge.output_seq,
            'created_at': now,
        }
        entry = {
            'request': request_payload,
            'session_token': session_token,
            'terminal_id': terminal_id,
            'sid': sid,
            'created_at': now,
            'expires_at': now + AGENT_VIEWPORT_RENDER_REQUEST_TTL_SECONDS,
            'event': threading.Event(),
            'result': None,
            'error_code': None,
        }
        with self._lock:
            self._trim(now)
            self._requests[request_id] = entry
        return dict(request_payload)

    def resolve(self, session_token, terminal_id, sid, data):
        if not isinstance(data, dict):
            return None, AGENT_ERROR_RENDER_INVALID
        request_id = data.get('request_id')
        if not isinstance(request_id, str):
            return None, AGENT_ERROR_RENDER_INVALID
        with self._lock:
            self._trim(time.time())
            entry = self._requests.get(request_id)
            if not entry:
                return None, AGENT_ERROR_RENDER_STALE
            if (
                entry.get('session_token') != session_token
                or entry.get('terminal_id') != terminal_id
                or entry.get('sid') != sid
            ):
                return None, AGENT_ERROR_RENDER_STALE
            client_error = data.get('error_code')
            if data.get('status') == AGENT_STATUS_FAILED and isinstance(client_error, str):
                if client_error not in {
                    AGENT_ERROR_RENDER_INVALID,
                    AGENT_ERROR_RENDER_TOO_LARGE,
                    AGENT_ERROR_RENDER_TIMEOUT,
                    AGENT_ERROR_RENDER_STALE,
                    AGENT_ERROR_PRIVACY_BLOCKED,
                    AGENT_ERROR_PAUSED,
                    AGENT_ERROR_TERMINAL_NOT_FOUND,
                    AGENT_ERROR_NOT_ATTACHED,
                }:
                    client_error = AGENT_ERROR_RENDER_INVALID
                entry['result'] = None
                entry['error_code'] = client_error
                entry['event'].set()
                return None, client_error
            result, error_code = validate_agent_viewport_render_result_payload(
                data,
                entry.get('request'),
            )
            entry['result'] = result
            entry['error_code'] = error_code
            entry['event'].set()
            return result, error_code

    def fail(self, session_token, terminal_id, sid, request_id, error_code):
        if not isinstance(request_id, str):
            return AGENT_ERROR_RENDER_INVALID
        with self._lock:
            self._trim(time.time())
            entry = self._requests.get(request_id)
            if not entry:
                return AGENT_ERROR_RENDER_STALE
            if (
                entry.get('session_token') != session_token
                or entry.get('terminal_id') != terminal_id
                or entry.get('sid') != sid
            ):
                return AGENT_ERROR_RENDER_STALE
            entry['result'] = None
            entry['error_code'] = error_code
            entry['event'].set()
            return error_code

    def wait(self, request_id, wait_ms):
        with self._lock:
            entry = self._requests.get(request_id)
        if not entry:
            return None, AGENT_ERROR_RENDER_STALE
        if not entry['event'].wait(wait_ms / 1000.0):
            with self._lock:
                self._requests.pop(request_id, None)
            return None, AGENT_ERROR_RENDER_TIMEOUT
        with self._lock:
            entry = self._requests.pop(request_id, entry)
        if entry.get('error_code'):
            return None, entry.get('error_code')
        return dict(entry.get('result') or {}), None

    def discard(self, session_token, terminal_id=None, sid=None):
        with self._lock:
            for request_id, entry in list(self._requests.items()):
                if entry.get('session_token') == session_token \
                        and (terminal_id is None or entry.get('terminal_id') == terminal_id) \
                        and (sid is None or entry.get('sid') == sid):
                    entry['error_code'] = AGENT_ERROR_RENDER_STALE
                    entry['event'].set()
                    self._requests.pop(request_id, None)

    def clear(self):
        with self._lock:
            for entry in self._requests.values():
                entry['error_code'] = AGENT_ERROR_RENDER_STALE
                entry['event'].set()
            self._requests.clear()

    def _trim(self, now):
        for request_id, entry in list(self._requests.items()):
            if entry.get('expires_at', 0) < now:
                entry['error_code'] = AGENT_ERROR_RENDER_TIMEOUT
                entry['event'].set()
                self._requests.pop(request_id, None)

agent_viewport_render_request_store = AgentViewportRenderRequestStore()

def hash_external_agent_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

class ExternalAgentAttachStore:
    def __init__(self):
        self._tokens = {}

    def create(self, state, idle_timeout_seconds=AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS):
        if not state:
            return None
        token = 'agt_' + secrets.token_urlsafe(24)
        now = time.time()
        expires_at = None if idle_timeout_seconds is None else now + idle_timeout_seconds
        token_hash = hash_external_agent_token(token)
        self._tokens[token_hash] = {
            'token_hash': token_hash,
            'session_token': state.session_token,
            'terminal_id': state.terminal_id,
            'sid': state.sid,
            'session_id': state.session_id,
            'viewer_id': state.viewer_id,
            'agent_binding_id': state.agent_binding_id,
            'created_at': now,
            'last_used_at': now,
            'idle_timeout_seconds': idle_timeout_seconds,
            'expires_at': expires_at,
            'revoked': False,
            'attached': False,
            'external_agent_id': 'exa_' + secrets.token_urlsafe(12),
        }
        return token, dict(self._tokens[token_hash])

    def validate(self, token, terminal_id=None):
        if not isinstance(token, str) or not token.startswith('agt_'):
            return None, AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED
        token_hash = hash_external_agent_token(token)
        record = self._tokens.get(token_hash)
        if not record:
            return None, AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED
        if record.get('revoked'):
            return None, AGENT_ERROR_EXTERNAL_AGENT_REVOKED
        expires_at = record.get('expires_at')
        if expires_at is not None and expires_at < time.time():
            return None, AGENT_ERROR_EXTERNAL_AGENT_EXPIRED
        if terminal_id is not None and record.get('terminal_id') != terminal_id:
            return None, AGENT_ERROR_TERMINAL_MISMATCH
        stored = self._tokens.get(token_hash)
        if stored and stored.get('idle_timeout_seconds') is not None:
            now = time.time()
            stored['last_used_at'] = now
            stored['expires_at'] = now + stored['idle_timeout_seconds']
            record = dict(stored)
        return dict(record), None

    def mark_attached(self, token):
        record, error_code = self.validate(token)
        if error_code:
            return None, error_code
        stored = self._tokens.get(record['token_hash'])
        if stored:
            stored['attached'] = True
            stored['attached_at'] = time.time()
            return dict(stored), None
        return None, AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED

    def revoke(self, token):
        record, error_code = self.validate(token)
        if error_code:
            return None, error_code
        stored = self._tokens.get(record['token_hash'])
        if stored:
            stored['revoked'] = True
            stored['revoked_at'] = time.time()
            return dict(stored), None
        return None, AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED

    def discard(self, session_token, terminal_id=None, sid=None):
        for token_hash, record in list(self._tokens.items()):
            if record.get('session_token') == session_token \
                    and (terminal_id is None or record.get('terminal_id') == terminal_id) \
                    and (sid is None or record.get('sid') == sid):
                self._tokens.pop(token_hash, None)

    def clear(self):
        self._tokens.clear()

external_agent_attach_store = ExternalAgentAttachStore()

def get_agent_session_id(session_token):
    if not session_token:
        return None
    session_id = agent_session_ids.get(session_token)
    if not session_id:
        session_id = 'ags_' + secrets.token_urlsafe(12)
        agent_session_ids[session_token] = session_id
    return session_id

def get_agent_viewer_id(sid):
    if not sid:
        return None
    viewer_id = agent_viewer_ids.get(sid)
    if not viewer_id:
        viewer_id = 'agv_' + secrets.token_urlsafe(12)
        agent_viewer_ids[sid] = viewer_id
    return viewer_id

def normalize_agent_privacy_state(value):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace('-', '_')
    if normalized in AGENT_PRIVACY_STATES:
        return normalized
    return None

class AgentControlState:
    def __init__(self, session_token, terminal_id, sid):
        self.session_token = session_token
        self.session_id = get_agent_session_id(session_token)
        self.terminal_id = terminal_id
        self.sid = sid
        self.viewer_id = get_agent_viewer_id(sid)
        self.agent_binding_id = 'agb_' + secrets.token_urlsafe(12)
        self.mode = AGENT_MODE_DISABLED
        self.paused = False
        self.control_epoch = 0
        self.mode_version = 0
        self.privacy_state = AGENT_PRIVACY_NORMAL
        self.privacy_version = 0
        self.run_id = None
        self.human_activity_seq = 0
        self.human_activity_at = None
        self.human_input_lease_expires_at = None
        self.pending_actions = {}
        self.audit_ring = deque(maxlen=AGENT_AUDIT_EVENTS)

    def public_state(self):
        human_input_lease_active = is_agent_human_input_lease_active(self)
        return {
            'session_id': self.session_id,
            'viewer_id': self.viewer_id,
            'agent_binding_id': self.agent_binding_id,
            'terminal_id': self.terminal_id,
            'mode': self.mode,
            'paused': self.paused,
            'control_epoch': self.control_epoch,
            'mode_version': self.mode_version,
            'privacy_state': self.privacy_state,
            'privacy_version': self.privacy_version,
            'run_id': self.run_id,
            'human_activity_seq': self.human_activity_seq,
            'human_activity_at': self.human_activity_at,
            'human_input_lease_expires_at': self.human_input_lease_expires_at,
            'human_input_lease_active': human_input_lease_active,
            'pending_actions': len([
                action for action in self.pending_actions.values()
                if action.get('status') in AGENT_STATUS_OPEN
            ]),
        }

class MockAgentBridge:
    def create_terminal_input_action(self, state, request_payload):
        data = ''
        if isinstance(request_payload, dict) and isinstance(request_payload.get('mock_input'), str):
            data = request_payload['mock_input']
        return {
            'action_type': AGENT_ACTION_TERMINAL_INPUT,
            'terminal_id': state.terminal_id,
            'data': data,
        }


AGENT_BRIDGE = MockAgentBridge()

class AgentProviderError(Exception):
    def __init__(self, error_code=AGENT_ERROR_PROVIDER_FAILED, message=None):
        super().__init__(message or error_code)
        self.error_code = error_code
        self.message = message

class AgentProvider:
    name = 'base'
    version = '0'

    def create_terminal_input_proposal(self, context, run):
        raise NotImplementedError

    def metadata(self):
        return {
            'provider_name': self.name,
            'provider_version': self.version,
        }

class MockAgentProvider(AgentProvider):
    name = 'mock'
    version = '1'

    def create_terminal_input_proposal(self, context, run):
        return {
            'action_type': AGENT_ACTION_TERMINAL_INPUT,
            'terminal_id': context.get('terminal_id'),
            'data': 'pwd\n',
        }

class StaticEnvAgentProvider(AgentProvider):
    name = 'static_env'
    version = '1'

    def __init__(self, terminal_input):
        self.terminal_input = terminal_input

    def create_terminal_input_proposal(self, context, run):
        return {
            'action_type': AGENT_ACTION_TERMINAL_INPUT,
            'terminal_id': context.get('terminal_id'),
            'data': self.terminal_input,
        }

class UnavailableAgentProvider(AgentProvider):
    version = '0'

    def __init__(self, name, reason):
        self.name = name or 'unavailable'
        self.reason = reason

    def create_terminal_input_proposal(self, context, run):
        raise AgentProviderError(AGENT_ERROR_PROVIDER_UNAVAILABLE, self.reason)

def build_agent_provider_from_env():
    provider_name = os.getenv('WEBSSH_AGENT_PROVIDER', 'mock').strip().lower() or 'mock'
    if provider_name == 'mock':
        return MockAgentProvider()
    if provider_name == 'static_env':
        terminal_input = os.getenv('WEBSSH_AGENT_STATIC_INPUT')
        if not isinstance(terminal_input, str) or terminal_input == '':
            return UnavailableAgentProvider(provider_name, 'WEBSSH_AGENT_STATIC_INPUT is required')
        return StaticEnvAgentProvider(terminal_input)
    return UnavailableAgentProvider(provider_name, 'Unknown Agent provider')

AGENT_PROVIDER = build_agent_provider_from_env()

def set_agent_provider_for_test(provider):
    global AGENT_PROVIDER
    AGENT_PROVIDER = provider

def get_agent_provider():
    return AGENT_PROVIDER

def create_agent_run_id():
    return 'agr_' + secrets.token_urlsafe(12)

class AgentTerminalMirror:
    source = None
    provisional = True

    def get_active_screen(self, session_token, terminal_id, sid):
        raise NotImplementedError

    def metadata(self):
        return {
            'source': self.source,
            'provisional': self.provisional,
        }

class BrowserViewportMirrorAdapter(AgentTerminalMirror):
    source = 'browser_viewport_snapshot'
    provisional = True

    def get_active_screen(self, session_token, terminal_id, sid):
        snapshot = agent_viewport_snapshot_store.get_latest(session_token, terminal_id, sid)
        if not snapshot:
            return None
        return {
            'source': self.source,
            'provisional': self.provisional,
            'terminal_id': terminal_id,
            'cols': snapshot.get('cols'),
            'rows': snapshot.get('rows'),
            'viewport_y': snapshot.get('viewport_y'),
            'base_y': snapshot.get('base_y'),
            'snapshot_seq': snapshot.get('snapshot_seq'),
            'output_seq': snapshot.get('output_seq'),
            'captured_at': snapshot.get('captured_at'),
            'line_count': snapshot.get('line_count'),
            'byte_length': snapshot.get('byte_length'),
            'lines': list(snapshot.get('lines') or []),
        }

AGENT_TERMINAL_MIRROR = BrowserViewportMirrorAdapter()

def parse_external_agent_screen_options(command):
    has_tail_lines = command.get('tail_lines') is not None
    has_region = command.get('region') is not None
    if has_tail_lines and has_region:
        return None, AGENT_ERROR_ACTION_INVALID_DATA

    if has_tail_lines:
        try:
            tail_lines = int(command.get('tail_lines'))
        except (TypeError, ValueError):
            return None, AGENT_ERROR_ACTION_INVALID_DATA
        if tail_lines < 0:
            return None, AGENT_ERROR_ACTION_INVALID_DATA
        return {'mode': 'tail_lines', 'tail_lines': tail_lines}, None

    if has_region:
        region = command.get('region')
        if not isinstance(region, dict):
            return None, AGENT_ERROR_ACTION_INVALID_DATA
        try:
            top = int(region.get('top'))
            bottom = int(region.get('bottom'))
        except (TypeError, ValueError):
            return None, AGENT_ERROR_ACTION_INVALID_DATA
        if top < 0 or bottom < top:
            return None, AGENT_ERROR_ACTION_INVALID_DATA
        return {'mode': 'region', 'top': top, 'bottom': bottom}, None

    return {'mode': 'full'}, None

def apply_external_agent_screen_options(screen, options):
    if not isinstance(screen, dict) or options.get('mode') == 'full':
        return screen
    lines = list(screen.get('lines') or [])
    original_line_count = len(lines)
    if options.get('mode') == 'tail_lines':
        tail_lines = options['tail_lines']
        start = max(0, original_line_count - tail_lines)
        end = original_line_count
        selected = lines[start:end]
        region = {
            'top': start,
            'bottom': end,
            'tail_lines': tail_lines,
        }
    else:
        start = min(options['top'], original_line_count)
        end = min(options['bottom'], original_line_count)
        selected = lines[start:end]
        region = {
            'top': start,
            'bottom': end,
        }
    sliced = dict(screen)
    sliced['lines'] = selected
    sliced['line_count'] = len(selected)
    sliced['original_line_count'] = original_line_count
    sliced['region'] = region
    sliced['truncated'] = len(selected) != original_line_count
    return sliced

def build_agent_context(session_token, terminal_id, sid):
    bridge = get_bridge(session_token, terminal_id)
    session_metadata = bridge.session_metadata() if bridge else {
        'session_token': session_token,
        'terminal_id': terminal_id,
    }
    session_metadata = dict(session_metadata)
    session_metadata.pop('session_token', None)
    session_metadata['session_id'] = get_agent_session_id(session_token)
    state = get_agent_state(session_token, terminal_id, sid)
    privacy_state = state.privacy_state if state else AGENT_PRIVACY_NORMAL
    context_allowed = privacy_state not in AGENT_CONTEXT_BLOCKING_PRIVACY_STATES
    return {
        'session_id': get_agent_session_id(session_token),
        'viewer_id': get_agent_viewer_id(sid),
        'agent_binding_id': state.agent_binding_id if state else None,
        'terminal_id': terminal_id,
        'privacy': {
            'state': privacy_state,
            'version': state.privacy_version if state else 0,
            'context_allowed': context_allowed,
        },
        'terminal_mirror': AGENT_TERMINAL_MIRROR.metadata(),
        'terminal_session': session_metadata,
        'active_screen': AGENT_TERMINAL_MIRROR.get_active_screen(session_token, terminal_id, sid) if context_allowed else None,
        'viewport_snapshot': agent_viewport_snapshot_store.get_latest(session_token, terminal_id, sid) if context_allowed else None,
        'transcript_events': agent_transcript_store.get_recent(session_token, terminal_id) if context_allowed else [],
        'human_input_metadata': agent_user_input_metadata_store.get_recent(session_token, terminal_id) if context_allowed else [],
    }

def agent_state_key(session_token, terminal_id, sid):
    return (session_token, terminal_id, sid)

def normalize_agent_mode(value):
    if not isinstance(value, str):
        return None
    return AGENT_CLIENT_MODE_MAP.get(value.strip().lower().replace('-', '_'))

def bump_agent_mode_version(state):
    state.control_epoch += 1
    state.mode_version = state.control_epoch

def set_agent_privacy_state(state, privacy_state):
    if privacy_state == state.privacy_state:
        return False
    state.privacy_state = privacy_state
    state.privacy_version += 1
    return True

def is_agent_human_input_lease_active(state, now=None):
    if not state or state.human_input_lease_expires_at is None:
        return False
    now = time.time() if now is None else now
    return state.human_input_lease_expires_at > now

def note_agent_human_input(state):
    now = time.time()
    state.human_activity_seq += 1
    state.human_activity_at = now
    state.human_input_lease_expires_at = now + AGENT_HUMAN_INPUT_LEASE_SECONDS

def note_agent_human_input_for_terminal(session_token, terminal_id):
    updated_states = []
    for state in agent_states.values():
        if state.session_token == session_token and state.terminal_id == terminal_id:
            note_agent_human_input(state)
            updated_states.append(state)
    return updated_states

def is_agent_context_allowed(state):
    return state and state.privacy_state not in AGENT_CONTEXT_BLOCKING_PRIVACY_STATES

def get_agent_state(session_token, terminal_id, sid):
    return agent_states.get(agent_state_key(session_token, terminal_id, sid))

def get_or_create_agent_state(session_token, terminal_id, sid):
    key = agent_state_key(session_token, terminal_id, sid)
    state = agent_states.get(key)
    if not state:
        state = AgentControlState(session_token, terminal_id, sid)
        agent_states[key] = state
    return state

def external_agent_error(error_code, terminal_id=None):
    payload = {
        'status': AGENT_STATUS_FAILED,
        'error_code': error_code,
    }
    if terminal_id:
        payload['terminal_id'] = terminal_id
    return payload

def is_external_agent_state_visible(state):
    return (
        state
        and not state.paused
        and state.mode != AGENT_MODE_DISABLED
        and state.mode != AGENT_MODE_PAUSED
    )

def get_external_agent_authorized_state(record):
    if not isinstance(record, dict):
        return None, AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED
    state = get_agent_state(
        record.get('session_token'),
        record.get('terminal_id'),
        record.get('sid'),
    )
    if not state:
        return None, AGENT_ERROR_NOT_ATTACHED
    if state.session_id != record.get('session_id') \
            or state.viewer_id != record.get('viewer_id') \
            or state.agent_binding_id != record.get('agent_binding_id'):
        return state, AGENT_ERROR_STALE_PROPOSAL
    if not is_external_agent_state_visible(state):
        return state, AGENT_ERROR_EXTERNAL_AGENT_DISABLED
    return state, None

def mint_external_agent_attach_token(session_token, terminal_id, sid,
                                     idle_timeout_seconds=AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS):
    with agent_lock:
        if not get_bridge(session_token, terminal_id):
            return None, None, AGENT_ERROR_TERMINAL_NOT_FOUND
        state = get_agent_state(session_token, terminal_id, sid)
        if not state:
            return None, None, AGENT_ERROR_NOT_ATTACHED
        if not is_external_agent_state_visible(state):
            return None, None, AGENT_ERROR_EXTERNAL_AGENT_DISABLED
        with external_agent_lock:
            token, record = external_agent_attach_store.create(
                state,
                idle_timeout_seconds=idle_timeout_seconds,
            )
        record_agent_audit_event(
            state,
            AGENT_AUDIT_EXTERNAL_AGENT_TOKEN_CREATED,
            external_agent_id=record.get('external_agent_id'),
            expires_at=record.get('expires_at'),
        )
        return token, record, None

def mint_external_agent_attach_token_for_viewer(session_token, terminal_id, viewer_id,
                                                agent_binding_id, mode_version=None,
                                                privacy_version=None):
    if not session_token or not terminal_id:
        return None, None, AGENT_ERROR_ACTION_INVALID_DATA
    with agent_lock:
        matches = [
            state for state in agent_states.values()
            if state.session_token == session_token
            and state.terminal_id == terminal_id
            and state.viewer_id == viewer_id
            and state.agent_binding_id == agent_binding_id
        ]
        if not matches:
            return None, None, AGENT_ERROR_NOT_ATTACHED
        state = matches[-1]
        if mode_version is not None and state.mode_version != mode_version:
            return None, None, AGENT_ERROR_STALE_MODE_VERSION
        if privacy_version is not None and state.privacy_version != privacy_version:
            return None, None, AGENT_ERROR_STALE_PROPOSAL
        return mint_external_agent_attach_token(session_token, terminal_id, state.sid)

def validate_external_agent_command_token(command, require_terminal=True):
    if not isinstance(command, dict):
        return None, None, None, AGENT_ERROR_ACTION_INVALID_DATA
    terminal_id = command.get('terminal_id')
    if require_terminal and not validate_terminal_id_payload(command):
        return None, None, terminal_id if isinstance(terminal_id, str) else None, AGENT_ERROR_ACTION_INVALID_DATA
    if require_terminal:
        terminal_id = validate_terminal_id_payload(command)
    token = command.get('token')
    with external_agent_lock:
        record, error_code = external_agent_attach_store.validate(token, terminal_id=terminal_id)
    if error_code:
        return None, None, terminal_id, error_code
    with agent_lock:
        state, error_code = get_external_agent_authorized_state(record)
        if error_code:
            return record, state, terminal_id, error_code
    return record, state, terminal_id, None

def build_external_agent_state_payload(record, state):
    payload = state.public_state()
    payload['external_agent_id'] = record.get('external_agent_id')
    payload['status'] = 'ok'
    return payload

def build_external_agent_tail_payload(bridge, since_output_seq=None, limit=AGENT_EXTERNAL_TAIL_MAX_EVENTS):
    try:
        since_output_seq = int(since_output_seq if since_output_seq is not None else 0)
    except (TypeError, ValueError):
        since_output_seq = 0
    since_output_seq = max(0, since_output_seq)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = AGENT_EXTERNAL_TAIL_MAX_EVENTS
    limit = max(1, min(limit, AGENT_EXTERNAL_TAIL_MAX_EVENTS))
    with bridge.output_condition:
        output_seq = bridge.output_seq
        replay_events = [
            dict(payload) for payload in list(bridge.replay_buffer)
            if isinstance(payload.get('output_seq'), int)
        ]
    events = [
        payload for payload in replay_events
        if payload.get('output_seq') > since_output_seq
    ]
    first_available_output_seq = None
    if replay_events:
        first_available_output_seq = replay_events[0].get('output_seq')
    elif output_seq > 0:
        first_available_output_seq = output_seq + 1
    dropped_before_output_seq = max(0, (first_available_output_seq or 1) - 1)
    gap_detected = (
        output_seq > 0
        and first_available_output_seq is not None
        and since_output_seq < first_available_output_seq - 1
    )
    gap = {
        'detected': gap_detected,
        'from_output_seq': since_output_seq + 1 if gap_detected else None,
        'to_output_seq': dropped_before_output_seq if gap_detected else None,
        'missing_count': dropped_before_output_seq - since_output_seq if gap_detected else 0,
    }
    return {
        'output_seq': output_seq,
        'since_output_seq': since_output_seq,
        'limit': limit,
        'first_available_output_seq': first_available_output_seq,
        'dropped_before_output_seq': dropped_before_output_seq,
        'gap': gap,
        'events': events[:limit],
    }

def format_external_agent_tail_payload(tail, strip_ansi=False):
    payload = dict(tail)
    payload['events'] = [dict(event) for event in tail.get('events', [])]
    if not strip_ansi:
        return payload
    payload['strip_ansi'] = True
    payload['data_format'] = 'plain'
    for event in payload['events']:
        data = event.get('data')
        if isinstance(data, str):
            stripped = strip_terminal_display_text(data)
            event['data'] = stripped
            if 'byte_length' in event:
                event['byte_length'] = len(stripped.encode('utf-8', errors='ignore'))
    return payload

def parse_external_agent_tail_wait_ms(value):
    try:
        wait_ms = int(value if value is not None else 0)
    except (TypeError, ValueError):
        wait_ms = 0
    return max(0, min(wait_ms, AGENT_EXTERNAL_TAIL_MAX_WAIT_MS))

def parse_external_agent_send_capture_wait_ms(value):
    try:
        wait_ms = int(value if value is not None else AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_WAIT_MS)
    except (TypeError, ValueError):
        wait_ms = AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_WAIT_MS
    return max(0, min(wait_ms, AGENT_EXTERNAL_TAIL_MAX_WAIT_MS))

def parse_external_agent_send_capture_settle_ms(value):
    try:
        settle_ms = int(value if value is not None else AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_SETTLE_MS)
    except (TypeError, ValueError):
        settle_ms = AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_SETTLE_MS
    return max(0, min(settle_ms, AGENT_EXTERNAL_SEND_CAPTURE_MAX_SETTLE_MS))

def external_agent_flag_enabled(value):
    return value is True or value == 1

def should_external_agent_capture_send(command):
    return external_agent_flag_enabled(command.get('capture'))

def should_external_agent_strip_ansi(command):
    return external_agent_flag_enabled(command.get('strip_ansi'))

def get_external_agent_capture_context_error(state):
    if state.paused or state.mode == AGENT_MODE_PAUSED:
        return AGENT_ERROR_PAUSED
    if state.mode == AGENT_MODE_DISABLED:
        return AGENT_ERROR_EXTERNAL_AGENT_DISABLED
    if not is_agent_context_allowed(state):
        return AGENT_ERROR_PRIVACY_BLOCKED
    return None

def build_external_agent_send_capture_payload(bridge, state, before_output_seq,
                                              limit=AGENT_EXTERNAL_TAIL_MAX_EVENTS,
                                              wait_ms=None, settle_ms=None,
                                              strip_ansi=False):
    wait_ms = parse_external_agent_send_capture_wait_ms(wait_ms)
    settle_ms = parse_external_agent_send_capture_settle_ms(settle_ms)
    context_error = get_external_agent_capture_context_error(state)
    if context_error:
        return None, context_error
    tail = build_external_agent_tail_payload(
        bridge,
        since_output_seq=before_output_seq,
        limit=limit,
    )
    deadline = time.monotonic() + wait_ms / 1000.0
    timed_out = False

    while not tail['events'] and not tail['gap']['detected']:
        context_error = get_external_agent_capture_context_error(state)
        if context_error:
            return None, context_error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        with bridge.output_condition:
            bridge.output_condition.wait(timeout=min(remaining, 0.25))
        tail = build_external_agent_tail_payload(
            bridge,
            since_output_seq=before_output_seq,
            limit=limit,
        )

    settled = not timed_out
    if tail['events'] and settle_ms > 0:
        settle_deadline = time.monotonic() + settle_ms / 1000.0
        last_output_seq = tail['output_seq']
        while True:
            context_error = get_external_agent_capture_context_error(state)
            if context_error:
                return None, context_error
            remaining = settle_deadline - time.monotonic()
            if remaining <= 0:
                break
            with bridge.output_condition:
                bridge.output_condition.wait(timeout=min(remaining, 0.25))
            latest = build_external_agent_tail_payload(
                bridge,
                since_output_seq=before_output_seq,
                limit=limit,
            )
            if latest['output_seq'] != last_output_seq:
                tail = latest
                last_output_seq = latest['output_seq']
                settle_deadline = time.monotonic() + settle_ms / 1000.0
        settled = True

    context_error = get_external_agent_capture_context_error(state)
    if context_error:
        return None, context_error
    return format_external_agent_tail_payload({
        'status': 'timeout' if timed_out else 'ok',
        'mode': 'tail',
        'before_output_seq': before_output_seq,
        'output_seq': tail['output_seq'],
        'after_output_seq': tail['output_seq'],
        'since_output_seq': tail['since_output_seq'],
        'limit': tail['limit'],
        'wait_ms': wait_ms,
        'settle_ms': settle_ms,
        'settled': settled,
        'timed_out': timed_out,
        'first_available_output_seq': tail['first_available_output_seq'],
        'dropped_before_output_seq': tail['dropped_before_output_seq'],
        'gap': tail['gap'],
        'events': tail['events'],
    }, strip_ansi=strip_ansi), None

def build_external_agent_tail_payload_waiting(bridge, state, since_output_seq=None,
                                              limit=AGENT_EXTERNAL_TAIL_MAX_EVENTS,
                                              wait_ms=0):
    tail = build_external_agent_tail_payload(
        bridge,
        since_output_seq=since_output_seq,
        limit=limit,
    )
    wait_ms = parse_external_agent_tail_wait_ms(wait_ms)
    if wait_ms <= 0 or tail['events'] or tail['gap']['detected']:
        return tail, None

    deadline = time.monotonic() + wait_ms / 1000.0
    while True:
        if state.paused or state.mode == AGENT_MODE_PAUSED:
            return None, AGENT_ERROR_PAUSED
        if state.mode == AGENT_MODE_DISABLED:
            return None, AGENT_ERROR_EXTERNAL_AGENT_DISABLED
        if not is_agent_context_allowed(state):
            return None, AGENT_ERROR_PRIVACY_BLOCKED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return tail, None
        with bridge.output_condition:
            bridge.output_condition.wait(timeout=min(remaining, 0.25))
        tail = build_external_agent_tail_payload(
            bridge,
            since_output_seq=tail['since_output_seq'],
            limit=tail['limit'],
        )
        if tail['events'] or tail['gap']['detected']:
            return tail, None

def build_external_agent_viewport_render_payload(record, state, terminal_id, bridge, wait_ms=None):
    wait_ms = parse_agent_viewport_render_wait_ms(wait_ms)
    request_payload = agent_viewport_render_request_store.create(
        record.get('session_token'),
        terminal_id,
        record.get('sid'),
        state,
        bridge,
    )
    socketio.emit(AGENT_EVENT_VIEWPORT_RENDER_REQUEST, request_payload, room=state.sid)
    result, error_code = agent_viewport_render_request_store.wait(
        request_payload['request_id'],
        wait_ms,
    )
    if error_code:
        return None, error_code, request_payload, wait_ms
    return result, None, request_payload, wait_ms

def external_agent_build_terminal_input_action(state, data):
    return {
        'action_type': AGENT_ACTION_TERMINAL_INPUT,
        'terminal_id': state.terminal_id,
        'data': data,
        'provider_name': 'external_agent',
        'provider_version': '1',
    }

def escape_agent_preview(value):
    return value.encode('unicode_escape', errors='backslashreplace').decode('ascii')

def has_agent_control_chars(value):
    return any(ord(ch) < 32 and ch not in '\r\n\t' for ch in value)

def summarize_agent_input(value):
    encoded = value.encode('utf-8', errors='ignore')
    preview = escape_agent_preview(value)
    if len(preview) > AGENT_PREVIEW_CHARS:
        preview = preview[:AGENT_PREVIEW_CHARS] + '...'
    return {
        'byte_length': len(encoded),
        'line_count': value.count('\n') + (1 if value and not value.endswith('\n') else 0),
        'contains_control_chars': has_agent_control_chars(value),
        'ends_with_newline': value.endswith('\n') or value.endswith('\r'),
        'escaped_preview': preview,
    }

def build_agent_action(state, proposal, requires_approval):
    action_data = proposal.get('data')
    if not isinstance(action_data, str):
        return None, AGENT_ERROR_ACTION_INVALID_DATA
    if len(action_data.encode('utf-8', errors='ignore')) > AGENT_MAX_INPUT_BYTES:
        return None, AGENT_ERROR_ACTION_TOO_LARGE
    action_id = secrets.token_urlsafe(12)
    proposal_id = 'agp_' + secrets.token_urlsafe(12)
    run_id = proposal.get('run_id') if isinstance(proposal.get('run_id'), str) else None
    run_id = run_id or state.run_id or create_agent_run_id()
    provider_name = proposal.get('provider_name') if isinstance(proposal.get('provider_name'), str) else None
    provider_version = proposal.get('provider_version') if isinstance(proposal.get('provider_version'), str) else None
    provider_status = proposal.get('provider_status') if isinstance(proposal.get('provider_status'), str) else None
    action = {
        'action_id': action_id,
        'proposal_id': proposal_id,
        'action_type': AGENT_ACTION_TERMINAL_INPUT,
        'session_id': state.session_id,
        'viewer_id': state.viewer_id,
        'agent_binding_id': state.agent_binding_id,
        'terminal_id': state.terminal_id,
        'data': action_data,
        'requires_approval': requires_approval,
        'status': AGENT_STATUS_PENDING_APPROVAL if requires_approval else AGENT_STATUS_DIRECT_PENDING,
        'created_at': time.time(),
        'control_epoch': state.control_epoch,
        'mode_version': state.mode_version,
        'privacy_state': state.privacy_state,
        'privacy_version': state.privacy_version,
        'run_id': run_id,
    }
    if provider_name:
        action['provider_name'] = provider_name
    if provider_version:
        action['provider_version'] = provider_version
    if provider_status:
        action['provider_status'] = provider_status
    action.update(summarize_agent_input(action_data))
    state.pending_actions[action_id] = action
    state.run_id = run_id
    record_agent_audit_event(
        state,
        AGENT_AUDIT_PROPOSAL_CREATED,
        action=action,
        status=action['status'],
    )
    return action, None

def public_agent_action(action):
    return {
        'action_id': action.get('action_id'),
        'proposal_id': action.get('proposal_id'),
        'action_type': action.get('action_type'),
        'session_id': action.get('session_id'),
        'viewer_id': action.get('viewer_id'),
        'agent_binding_id': action.get('agent_binding_id'),
        'terminal_id': action.get('terminal_id'),
        'requires_approval': action.get('requires_approval'),
        'status': action.get('status'),
        'control_epoch': action.get('control_epoch'),
        'mode_version': action.get('mode_version'),
        'privacy_state': action.get('privacy_state'),
        'privacy_version': action.get('privacy_version'),
        'run_id': action.get('run_id'),
        'provider_name': action.get('provider_name'),
        'provider_version': action.get('provider_version'),
        'provider_status': action.get('provider_status'),
        'byte_length': action.get('byte_length'),
        'line_count': action.get('line_count'),
        'contains_control_chars': action.get('contains_control_chars'),
        'ends_with_newline': action.get('ends_with_newline'),
        'escaped_preview': action.get('escaped_preview'),
    }

def build_agent_audit_identity(state):
    if not state:
        return {}
    return {
        'viewer_id': state.viewer_id,
        'agent_binding_id': state.agent_binding_id,
        'mode': state.mode,
        'control_epoch': state.control_epoch,
        'mode_version': state.mode_version,
        'privacy_state': state.privacy_state,
        'privacy_version': state.privacy_version,
        'run_id': state.run_id,
    }

def record_agent_audit_event(state, event_type, action=None, **fields):
    if not state:
        return None
    event_fields = build_agent_audit_identity(state)
    event_fields.update(fields)
    if action:
        action_metadata = public_agent_action(action)
        action_metadata.pop('escaped_preview', None)
        event_fields['action'] = action_metadata
    entry = agent_audit_store.append(
        state.session_token,
        state.terminal_id,
        event_type,
        **event_fields,
    )
    if entry:
        state.audit_ring.append(entry)
    return entry

def summarize_agent_context_for_audit(context):
    if not isinstance(context, dict):
        return None
    active_screen = context.get('active_screen') or {}
    terminal_session = context.get('terminal_session') or {}
    privacy = context.get('privacy') or {}
    return {
        'privacy_state': privacy.get('state'),
        'context_allowed': privacy.get('context_allowed'),
        'terminal_output_seq': terminal_session.get('output_seq'),
        'active_screen_source': active_screen.get('source'),
        'active_screen_provisional': active_screen.get('provisional'),
        'active_screen_output_seq': active_screen.get('output_seq'),
        'active_screen_rows': active_screen.get('rows'),
        'active_screen_cols': active_screen.get('cols'),
        'transcript_event_count': len(context.get('transcript_events') or []),
        'human_input_event_count': len(context.get('human_input_metadata') or []),
    }

def record_agent_audit(state, action, status, error_code=None):
    record_agent_audit_event(
        state,
        AGENT_AUDIT_ACTION_RESULT,
        action=action,
        status=status,
        error_code=error_code,
    )

def emit_agent_state(sid, state):
    socketio.emit(AGENT_EVENT_STATE, state.public_state(), room=sid)

def emit_agent_action_result(sid, action, status, error_code=None):
    payload = public_agent_action(action)
    payload['status'] = status
    if error_code:
        payload['error_code'] = error_code
    socketio.emit(AGENT_EVENT_ACTION_RESULT, payload, room=sid)

def emit_agent_error(sid, terminal_id, error_code, message=None):
    payload = {
        'terminal_id': terminal_id,
        'status': AGENT_STATUS_FAILED,
        'error_code': error_code,
    }
    if message:
        payload['message'] = message
    socketio.emit(AGENT_EVENT_ACTION_RESULT, payload, room=sid)

def cancel_agent_pending_actions(state, reason):
    cancelled = []
    for action in state.pending_actions.values():
        if action.get('status') in AGENT_STATUS_OPEN:
            action['status'] = reason
            record_agent_audit(state, action, reason, error_code=reason)
            cancelled.append(dict(action))
    return cancelled

def invalidate_agent_states(session_token, terminal_id=None, sid=None, reason=AGENT_REASON_INVALIDATED):
    with agent_lock:
        matching_keys = [
            key for key, state in agent_states.items()
            if state.session_token == session_token
            and (terminal_id is None or state.terminal_id == terminal_id)
            and (sid is None or state.sid == sid)
        ]
        invalidated = []
        for key in matching_keys:
            state = agent_states.pop(key)
            state.paused = True
            state.mode = AGENT_MODE_DISABLED
            set_agent_privacy_state(state, AGENT_PRIVACY_PAUSED)
            bump_agent_mode_version(state)
            with external_agent_lock:
                external_agent_attach_store.discard(state.session_token, state.terminal_id, state.sid)
            invalidated.extend((state.sid, action) for action in cancel_agent_pending_actions(state, reason))
        return invalidated

def iter_text_chunks(value, max_chunk_bytes):
    chunk = []
    chunk_bytes = 0
    for ch in value:
        ch_bytes = len(ch.encode('utf-8', errors='ignore'))
        if chunk and chunk_bytes + ch_bytes > max_chunk_bytes:
            yield ''.join(chunk)
            chunk = []
            chunk_bytes = 0
        chunk.append(ch)
        chunk_bytes += ch_bytes
    if chunk:
        yield ''.join(chunk)

def check_agent_write_allowed(session_token, terminal_id, sid, action_id, control_epoch,
                              mode_version=None, proposal_id=None):
    state = get_agent_state(session_token, terminal_id, sid)
    if not state:
        return None, None, AGENT_ERROR_NOT_ATTACHED
    if state.paused or state.mode == AGENT_MODE_PAUSED:
        return state, None, AGENT_ERROR_PAUSED
    if state.privacy_state in AGENT_CONTEXT_BLOCKING_PRIVACY_STATES:
        return state, None, AGENT_ERROR_PRIVACY_BLOCKED
    if state.control_epoch != control_epoch:
        return state, None, AGENT_ERROR_STALE_EPOCH
    if mode_version is not None and state.mode_version != mode_version:
        return state, None, AGENT_ERROR_STALE_MODE_VERSION
    action = state.pending_actions.get(action_id)
    if not action:
        return state, None, AGENT_ERROR_ACTION_NOT_FOUND
    if proposal_id is not None and action.get('proposal_id') != proposal_id:
        return state, action, AGENT_ERROR_STALE_PROPOSAL
    if action.get('session_id') != state.session_id:
        return state, action, AGENT_ERROR_STALE_PROPOSAL
    if action.get('viewer_id') != state.viewer_id:
        return state, action, AGENT_ERROR_STALE_PROPOSAL
    if action.get('agent_binding_id') != state.agent_binding_id:
        return state, action, AGENT_ERROR_STALE_PROPOSAL
    if action.get('control_epoch') != control_epoch:
        return state, action, AGENT_ERROR_STALE_ACTION
    if action.get('mode_version') != state.mode_version:
        return state, action, AGENT_ERROR_STALE_MODE_VERSION
    if action.get('privacy_version') != state.privacy_version:
        return state, action, AGENT_ERROR_PRIVACY_BLOCKED
    if action.get('status') not in AGENT_STATUS_WRITABLE:
        return state, action, AGENT_ERROR_ACTION_NOT_WRITABLE
    if action.get('terminal_id') != terminal_id:
        return state, action, AGENT_ERROR_TERMINAL_MISMATCH
    if is_agent_human_input_lease_active(state):
        return state, action, AGENT_ERROR_HUMAN_INPUT_ACTIVE
    bridge = get_bridge(session_token, terminal_id)
    if not bridge:
        return state, action, AGENT_ERROR_TERMINAL_NOT_FOUND
    return state, action, None

def write_agent_terminal_input(session_token, terminal_id, sid, action_id, control_epoch,
                               mode_version=None, proposal_id=None):
    bytes_written = 0
    while True:
        with agent_lock:
            state, action, error_code = check_agent_write_allowed(
                session_token,
                terminal_id,
                sid,
                action_id,
                control_epoch,
                mode_version=mode_version,
                proposal_id=proposal_id,
            )
            if error_code:
                if state and action:
                    action['status'] = AGENT_STATUS_FAILED
                    record_agent_audit(state, action, AGENT_STATUS_FAILED, error_code=error_code)
                return False, {'error_code': error_code, 'bytes_written': bytes_written}
            data = action.get('data', '')
            if not isinstance(data, str):
                return False, {'error_code': AGENT_ERROR_ACTION_INVALID_DATA, 'bytes_written': bytes_written}
            chunks = list(iter_text_chunks(data, AGENT_INPUT_CHUNK_BYTES))
            if not chunks:
                action['status'] = AGENT_STATUS_COMPLETED
                record_agent_audit(state, action, AGENT_STATUS_COMPLETED)
                return True, {'bytes_written': 0}
            break

    for chunk in chunks:
        bridge = get_bridge(session_token, terminal_id)
        if not bridge:
            return False, {'error_code': AGENT_ERROR_TERMINAL_NOT_FOUND, 'bytes_written': bytes_written}
        with bridge.input_lock:
            with agent_lock:
                state, action, error_code = check_agent_write_allowed(
                    session_token,
                    terminal_id,
                    sid,
                    action_id,
                    control_epoch,
                    mode_version=mode_version,
                    proposal_id=proposal_id,
                )
                if error_code:
                    if state and action:
                        action['status'] = AGENT_STATUS_FAILED
                        record_agent_audit(state, action, AGENT_STATUS_FAILED, error_code=error_code)
                    return False, {'error_code': error_code, 'bytes_written': bytes_written}
            bridge.write(chunk)
        bytes_written += len(chunk.encode('utf-8', errors='ignore'))

    with agent_lock:
        state = get_agent_state(session_token, terminal_id, sid)
        if state:
            action = state.pending_actions.get(action_id)
            if action:
                if not action.get('requires_approval'):
                    record_agent_audit_event(
                        state,
                        AGENT_AUDIT_DIRECT_WRITE,
                        action=action,
                        bytes_written=bytes_written,
                    )
                action['status'] = AGENT_STATUS_COMPLETED
                record_agent_audit(state, action, AGENT_STATUS_COMPLETED)
    return True, {'bytes_written': bytes_written}

def is_valid_access_token(token):
    if not isinstance(token, str):
        return False
    return secrets.compare_digest(token.strip(), ACCESS_TOKEN)

def is_valid_session(session_token):
    if not isinstance(session_token, str):
        return False
    expires_at = active_sessions.get(session_token)
    if not expires_at:
        return False
    if time.time() > expires_at:
        active_sessions.pop(session_token, None)
        close_all_terminal_bridges(session_token)
        agent_session_ids.pop(session_token, None)
        return False
    return True

def cleanup_expired_sessions():
    now = time.time()
    expired_tokens = [
        session_token
        for session_token, expires_at in list(active_sessions.items())
        if now > expires_at
    ]
    for session_token in expired_tokens:
        active_sessions.pop(session_token, None)
        close_all_terminal_bridges(session_token)
        for sid, sid_session_token in list(socket_session_tokens.items()):
            if sid_session_token == session_token:
                socket_session_tokens.pop(sid, None)
                socket_client_ips.pop(sid, None)
                socket_browser_identities.pop(sid, None)
                socket_browser_authorized.pop(sid, None)
                socket_browser_auth_challenges.pop(sid, None)
                agent_viewer_ids.pop(sid, None)
        agent_session_ids.pop(session_token, None)

def session_cleanup_loop():
    while True:
        socketio.sleep(SESSION_CLEANUP_INTERVAL_SECONDS)
        cleanup_expired_sessions()

def ensure_session_cleanup_task():
    global session_cleanup_task_started
    if session_cleanup_task_started:
        return
    session_cleanup_task_started = True
    socketio.start_background_task(target=session_cleanup_loop)

def get_request_session_token():
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not is_valid_session(session_token):
        return None
    return session_token

def has_control_chars(value):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)

def add_common_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

def build_access_required_response():
    response = make_response('''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WebSSH Access Required</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 3rem; line-height: 1.5; color: #1f2937; }
    code { background: #f3f4f6; padding: 0.1rem 0.3rem; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>WebSSH access required</h1>
  <p>Open the full Access URL printed by the launcher, including <code>?token=...</code>.</p>
  <p>If you copied the URL from another browser after it loaded, copy the launcher URL again instead.</p>
  <p>For Windows browsers connecting to a WSL IP over HTTPS, the browser may also require trusting the WebSSH local CA.</p>
</body>
</html>
''', 403)
    return add_common_headers(response)

def close_bridge(bridge):
    if not bridge:
        return
    bridge.closing = True
    bridge.close()

def record_agent_terminal_cleanup(session_token, terminal_id, reason):
    if not session_token or not terminal_id:
        return
    states = [
        state for state in agent_states.values()
        if state.session_token == session_token and state.terminal_id == terminal_id
    ]
    if states:
        for state in states:
            record_agent_audit_event(state, AGENT_AUDIT_TERMINAL_CLEANUP, reason=reason)
        return
    agent_audit_store.append(
        session_token,
        terminal_id,
        AGENT_AUDIT_TERMINAL_CLEANUP,
        reason=reason,
    )

def get_bridge(session_token, terminal_id):
    return bridges.get(session_token, {}).get(terminal_id)

def set_bridge(session_token, terminal_id, bridge):
    bridges.setdefault(session_token, {})[terminal_id] = bridge

def pop_bridge(session_token, terminal_id):
    terminals = bridges.get(session_token)
    if not terminals:
        return None
    bridge = terminals.pop(terminal_id, None)
    if not terminals:
        bridges.pop(session_token, None)
    return bridge

def unregister_terminal_bridge(session_token, terminal_id, bridge):
    if get_bridge(session_token, terminal_id) is not bridge:
        return
    pop_bridge(session_token, terminal_id)
    record_agent_terminal_cleanup(session_token, terminal_id, AGENT_REASON_TERMINAL_CLOSED)
    invalidate_agent_states(session_token, terminal_id=terminal_id, reason=AGENT_REASON_TERMINAL_CLOSED)
    agent_transcript_store.discard(session_token, terminal_id)
    agent_user_input_metadata_store.discard(session_token, terminal_id)
    agent_viewport_snapshot_store.discard(session_token, terminal_id=terminal_id)
    agent_viewport_render_request_store.discard(session_token, terminal_id=terminal_id)
    close_bridge(bridge)

def close_terminal_bridge(session_token, terminal_id):
    record_agent_terminal_cleanup(session_token, terminal_id, AGENT_REASON_TERMINAL_CLOSED)
    invalidate_agent_states(session_token, terminal_id=terminal_id, reason=AGENT_REASON_TERMINAL_CLOSED)
    agent_transcript_store.discard(session_token, terminal_id)
    agent_user_input_metadata_store.discard(session_token, terminal_id)
    agent_viewport_snapshot_store.discard(session_token, terminal_id=terminal_id)
    agent_viewport_render_request_store.discard(session_token, terminal_id=terminal_id)
    close_bridge(pop_bridge(session_token, terminal_id))

def close_all_terminal_bridges(session_token):
    for terminal_id in list(bridges.get(session_token, {})):
        record_agent_terminal_cleanup(session_token, terminal_id, AGENT_REASON_TERMINAL_CLOSED)
    invalidate_agent_states(session_token, reason=AGENT_REASON_TERMINAL_CLOSED)
    agent_transcript_store.discard(session_token)
    agent_user_input_metadata_store.discard(session_token)
    agent_viewport_snapshot_store.discard(session_token)
    agent_viewport_render_request_store.discard(session_token)
    terminals = bridges.pop(session_token, {})
    for bridge in list(terminals.values()):
        close_bridge(bridge)

def detach_session_bridges(session_token, sid):
    for bridge in bridges.get(session_token, {}).values():
        bridge.detach(sid)

def get_session_sids(session_token):
    return [
        sid
        for sid, sid_session_token in socket_session_tokens.items()
        if sid_session_token == session_token
    ]

def build_terminal_list(session_token):
    terminals = []
    for terminal_id, bridge in sorted(bridges.get(session_token, {}).items()):
        terminal_info = {
            'terminal_id': terminal_id,
            'connection_type': bridge.connection_type,
            'terminal_kind': bridge.terminal_kind,
            'terminal_label': bridge.terminal_label,
            'term': SSH_TERM,
            'connected': True,
            'buffered_events': len(bridge.replay_buffer),
        }
        terminals.append(terminal_info)
    return terminals

def emit_terminal_policy(sid):
    browser_authorized = socket_browser_authorized.get(sid, False)
    client_ip = socket_client_ips.get(sid, 'unknown')
    policy = build_terminal_policy(browser_authorized=browser_authorized)
    local_shell_option = next(
        (
            option for option in policy.get('connection_options', [])
            if option.get('connection_type') == CONNECTION_TYPE_LOCAL_SHELL
        ),
        {},
    )
    if DEBUG_POLICY:
        print(
            '[policy] '
            f'sid={sid} client_ip={client_ip} '
            f'browser_authorized={browser_authorized} '
            f'default={policy.get("default_connection")} '
            f'local_shell_allowed={local_shell_option.get("allowed")}',
            flush=True,
        )
    socketio.emit(
        'terminal_policy',
        policy,
        room=sid,
    )

def is_valid_terminal_id(terminal_id):
    if not isinstance(terminal_id, str):
        return False
    if not terminal_id or len(terminal_id) > MAX_TERMINAL_ID_LENGTH:
        return False
    return bool(TERMINAL_ID_PATTERN.fullmatch(terminal_id))

def validate_terminal_id_payload(data, default=None):
    if not isinstance(data, dict):
        return None
    terminal_id = data.get('terminal_id', default)
    if not is_valid_terminal_id(terminal_id):
        return None
    return terminal_id

def get_detected_serial_port(device):
    if not isinstance(device, str):
        return None
    selected_device = device.strip()
    if not selected_device or len(selected_device) > MAX_UART_PORT_LENGTH:
        return None
    if has_control_chars(selected_device):
        return None
    for port_info in detect_serial_ports():
        if port_info['device'] == selected_device:
            return port_info
    return get_manual_serial_port(selected_device)

def escape_debug_text(value):
    return value.encode('unicode_escape', errors='backslashreplace').decode('ascii')

def log_terminal_input(sid, terminal_id, data):
    if not DEBUG_INPUT:
        return
    data_bytes = data.encode('utf-8', errors='backslashreplace')
    hex_bytes = ' '.join(f'{byte:02x}' for byte in data_bytes)
    codepoints = ' '.join(f'U+{ord(ch):04X}' for ch in data)
    print(
        '[debug-input] '
        f'sid={sid} terminal_id={terminal_id} '
        f'chars={len(data)} bytes={len(data_bytes)} '
        f'hex={hex_bytes} '
        f'codepoints={codepoints} '
        f'text={escape_debug_text(data)}',
        flush=True,
    )

def emit_connection_error(sid, message, error_code=None, action_type=None, action_message=None,
                          action_question=None, action_id=None, terminal_id=TERMINAL_ID_MAIN):
    socketio.emit(
        'ssh_output',
        {
            'message_type': 'connection_error',
            'terminal_id': terminal_id,
            'message': message,
            'error_code': error_code,
            'action_type': action_type,
            'action_message': action_message,
            'action_question': action_question,
            'action_id': action_id,
        },
        room=sid,
    )

def validate_start_ssh_payload(data, client_ip, browser_authorized=False):
    if not isinstance(data, dict):
        return None, 'Invalid connection payload.'

    connection_type = normalize_connection_type(data.get('connection_type', DEFAULT_CONNECTION_TYPE))
    if not connection_type:
        return None, 'Connection type must be ssh, local_shell, or uart.'
    plugin = TERMINAL_BACKEND_REGISTRY.get(connection_type)
    if not plugin:
        return None, 'Connection type must be ssh, local_shell, or uart.'
    if FORCE_CONNECTION_TYPE and connection_type != FORCE_CONNECTION_TYPE:
        return None, f'Connection type is locked to {FORCE_CONNECTION_TYPE}.'

    terminal_id = validate_terminal_id_payload(data, default=TERMINAL_ID_MAIN)
    if not terminal_id:
        return None, 'Invalid terminal id.'

    plugin_payload, validation_error = plugin.validate_start_payload(
        data,
        terminal_id,
        client_ip,
        browser_authorized=browser_authorized,
    )
    if validation_error:
        return None, validation_error
    if plugin_payload is None:
        plugin_payload = {}
    if not isinstance(plugin_payload, dict):
        return None, {
            'message': 'Backend payload is invalid.',
            'error_code': 'invalid_backend_payload',
        }
    reserved_keys = RESERVED_BACKEND_PAYLOAD_KEYS.intersection(plugin_payload)
    if reserved_keys:
        return None, {
            'message': 'Backend payload used reserved fields.',
            'error_code': 'backend_payload_reserved_fields',
        }
    payload = {
        'connection_type': connection_type,
        'terminal_id': terminal_id,
    }
    payload.update(plugin_payload)
    return payload, None

def parse_terminal_size(data):
    if not isinstance(data, dict):
        return None
    try:
        cols = int(data.get('cols'))
        rows = int(data.get('rows'))
    except (TypeError, ValueError):
        return None
    if cols < MIN_TERMINAL_COLS or cols > MAX_TERMINAL_COLS:
        return None
    if rows < MIN_TERMINAL_ROWS or rows > MAX_TERMINAL_ROWS:
        return None
    return cols, rows

def build_session_response():
    ensure_session_cleanup_task()
    session_token = secrets.token_urlsafe(32)
    active_sessions[session_token] = time.time() + SESSION_COOKIE_MAX_AGE
    response = make_response(render_template(
        'index.html',
        ssh_term=SSH_TERM,
        terminal_policy=build_terminal_policy(),
    ))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite='Strict',
        secure=HTTPS_ENABLED,
    )
    return add_common_headers(response)

def get_pending_localhost_key_setup(sid, action_id):
    pending_setup = pending_localhost_key_setups.get(sid)
    if not pending_setup:
        return None, 'localhost_key_setup_no_pending_action'
    if time.time() > pending_setup['expires_at']:
        pending_localhost_key_setups.pop(sid, None)
        return None, 'localhost_key_setup_expired'
    if not isinstance(action_id, str) or not secrets.compare_digest(action_id, pending_setup['action_id']):
        return None, 'localhost_key_setup_no_pending_action'
    return pending_setup, None

@app.route('/')
def index():
    token = request.args.get('token')
    if is_valid_access_token(token):
        return build_session_response()

    if not is_valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return build_access_required_response()

    response = make_response(render_template(
        'index.html',
        ssh_term=SSH_TERM,
        terminal_policy=build_terminal_policy(),
    ))
    return add_common_headers(response)

def is_loopback_client_request():
    client_ip = get_request_client_ip()
    try:
        return ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        return client_ip in {'localhost'}

def external_agent_json_response(payload, status_code=200):
    response = jsonify(payload)
    response.status_code = status_code
    return add_common_headers(response)

def get_external_agent_tls_ca_cert_path():
    if HTTPS_ENABLED and not (CLI_ARGS.certfile or CLI_ARGS.keyfile) and LOCAL_CA_CERT_PATH.is_file():
        return str(LOCAL_CA_CERT_PATH)
    return None

def get_external_agent_cli_tls_args():
    ca_cert_path = get_external_agent_tls_ca_cert_path()
    if ca_cert_path:
        return ['--ca-file', ca_cert_path]
    return []

def is_loopback_url_host(host):
    if not host:
        return False
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def build_external_agent_loopback_base_url(base_url):
    base_url = base_url.rstrip('/')
    try:
        parsed = urllib.parse.urlsplit(base_url)
        if is_loopback_url_host(parsed.hostname):
            return base_url
        netloc = '127.0.0.1'
        if parsed.port is not None:
            netloc = f'{netloc}:{parsed.port}'
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path.rstrip('/'), '', ''))
    except ValueError:
        return base_url

def get_external_agent_local_base_url():
    scheme = 'https' if HTTPS_ENABLED else 'http'
    return f'{scheme}://127.0.0.1:{DEFAULT_PORT}'

def build_external_agent_cli_command(base_url, token, terminal_id, op='send', text='pwd\n',
                                     extra_args=None):
    args = [
        'tools/.venv_wsl/bin/python',
        'scripts/webssh_agent_cli.py',
        '--url',
        base_url,
        '--token',
        token,
        '--terminal',
        terminal_id,
    ]
    args.extend(get_external_agent_cli_tls_args())
    if op == 'send':
        args.extend(['send', '--text', text])
    elif op == 'send-wait':
        args.extend(['send-wait', '--text', text])
    else:
        args.append(op)
    if extra_args:
        args.extend(extra_args)
    return ' '.join(shlex.quote(arg) for arg in args)

def build_external_agent_repl_command(base_url, token, terminal_id):
    args = [
        'tools/.venv_wsl/bin/python',
        'scripts/webssh_agent_repl.py',
        '--url',
        base_url,
        '--token',
        token,
        '--terminal',
        terminal_id,
    ]
    args.extend(get_external_agent_cli_tls_args())
    return ' '.join(shlex.quote(arg) for arg in args)

def build_external_agent_jsonl_command(base_url, token, terminal_id):
    args = [
        'tools/.venv_wsl/bin/python',
        'scripts/webssh_agent_jsonl.py',
        '--url',
        base_url,
        '--token',
        token,
        '--terminal',
        terminal_id,
    ]
    args.extend(get_external_agent_cli_tls_args())
    return ' '.join(shlex.quote(arg) for arg in args)

def build_external_agent_cli_commands(base_url, token, terminal_id):
    return {
        'hello': build_external_agent_cli_command(base_url, token, terminal_id, op='hello'),
        'state': build_external_agent_cli_command(base_url, token, terminal_id, op='state'),
        'screen': build_external_agent_cli_command(base_url, token, terminal_id, op='screen'),
        'screen_tail': build_external_agent_cli_command(
            base_url,
            token,
            terminal_id,
            op='screen',
            extra_args=['--tail-lines', '12'],
        ),
        'screen_region': build_external_agent_cli_command(
            base_url,
            token,
            terminal_id,
            op='screen',
            extra_args=['--region', '0:12'],
        ),
        'render': build_external_agent_cli_command(base_url, token, terminal_id, op='render'),
        'tail': build_external_agent_cli_command(base_url, token, terminal_id, op='tail'),
        'tail_plain': build_external_agent_cli_command(
            base_url,
            token,
            terminal_id,
            op='tail',
            extra_args=['--strip-ansi'],
        ),
        'send_pwd': build_external_agent_cli_command(base_url, token, terminal_id, op='send', text='pwd\n'),
        'send_wait_pwd': build_external_agent_cli_command(base_url, token, terminal_id, op='send-wait', text='pwd\n'),
        'send_wait_plain_pwd': build_external_agent_cli_command(
            base_url,
            token,
            terminal_id,
            op='send-wait',
            text='pwd\n',
            extra_args=['--strip-ansi'],
        ),
        'repl': build_external_agent_repl_command(base_url, token, terminal_id),
        'jsonl': build_external_agent_jsonl_command(base_url, token, terminal_id),
    }

def build_external_agent_discovery_payload(base_url, token, terminal_id):
    command_base_url = build_external_agent_loopback_base_url(base_url)
    transport = {
        'type': 'loopback_http_json',
        'command_endpoint': command_base_url.rstrip('/') + '/agent/external/command',
        'loopback_only': True,
        'tls_verify': True,
    }
    ca_cert_path = get_external_agent_tls_ca_cert_path()
    if ca_cert_path:
        transport['tls_ca_cert_path'] = ca_cert_path
    return {
        'handoff_schema': 'webssh_external_agent_handoff',
        'schema_version': 1,
        'protocol_version': EXTERNAL_AGENT_PROTOCOL_VERSION,
        'transport': transport,
        'capabilities': list(EXTERNAL_AGENT_CAPABILITIES),
        'operations': {
            'hello': {'op': 'hello'},
            'state': {'op': 'state'},
            'screen': {'op': 'screen'},
            'screen_tail': {'op': 'screen', 'tail_lines': 12},
            'screen_region': {'op': 'screen', 'region': {'top': 0, 'bottom': 12}},
            'render': {'op': 'render', 'wait_ms': AGENT_VIEWPORT_RENDER_WAIT_MS},
            'tail': {
                'op': 'tail',
                'since_output_seq': 0,
                'limit': AGENT_EXTERNAL_TAIL_MAX_EVENTS,
                'wait_ms': AGENT_EXTERNAL_TAIL_MAX_WAIT_MS,
            },
            'tail_plain': {
                'op': 'tail',
                'since_output_seq': 0,
                'limit': AGENT_EXTERNAL_TAIL_MAX_EVENTS,
                'wait_ms': AGENT_EXTERNAL_TAIL_MAX_WAIT_MS,
                'strip_ansi': True,
            },
            'send': {'op': 'send', 'data': 'pwd\n'},
            'send_capture': {
                'op': 'send',
                'data': 'pwd\n',
                'capture': True,
                'wait_ms': AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_WAIT_MS,
                'settle_ms': AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_SETTLE_MS,
                'limit': AGENT_EXTERNAL_TAIL_MAX_EVENTS,
            },
            'send_wait': {
                'op': 'send-wait',
                'data': 'pwd\n',
                'capture': True,
                'wait_ms': AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_WAIT_MS,
                'settle_ms': AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_SETTLE_MS,
                'limit': AGENT_EXTERNAL_TAIL_MAX_EVENTS,
            },
            'send_wait_plain': {
                'op': 'send-wait',
                'data': 'pwd\n',
                'capture': True,
                'wait_ms': AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_WAIT_MS,
                'settle_ms': AGENT_EXTERNAL_SEND_CAPTURE_DEFAULT_SETTLE_MS,
                'limit': AGENT_EXTERNAL_TAIL_MAX_EVENTS,
                'strip_ansi': True,
            },
            'revoke': {'op': 'revoke'},
        },
        'cli_commands': build_external_agent_cli_commands(command_base_url, token, terminal_id),
        'security': {
            'token_prefix': 'agt_',
            'token_is_secret': True,
            'token_lifetime': 'session' if AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS is None else 'idle_timeout',
            'idle_timeout_seconds': AGENT_EXTERNAL_ATTACH_TOKEN_IDLE_TIMEOUT_SECONDS,
            'remote_use_requires_loopback_tunnel': True,
            'image_bytes_in_audit': False,
        },
    }

def write_external_agent_handoff(payload):
    handoff_path = EXTERNAL_AGENT_HANDOFF_PATH
    tmp_path = handoff_path.with_suffix('.json.tmp')
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    tmp_path.replace(handoff_path)
    return str(handoff_path)

def build_external_agent_token_payload(token, record, terminal_id, base_url):
    command_base_url = build_external_agent_loopback_base_url(base_url)
    discovery = build_external_agent_discovery_payload(command_base_url, token, terminal_id)
    cli_command = discovery['cli_commands']['send_pwd']
    payload = {
        'status': 'ok',
        'token': token,
        'terminal_id': terminal_id,
        'external_agent_id': record.get('external_agent_id'),
        'expires_at': record.get('expires_at'),
        'url': command_base_url,
        'browser_url': base_url,
        'cli_command': cli_command,
    }
    payload.update(discovery)
    payload['handoff_path'] = write_external_agent_handoff(payload)
    return payload

def quote_local_command(args, platform_name=None):
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name.startswith('win'):
        return subprocess.list2cmdline(args)
    return ' '.join(shlex.quote(arg) for arg in args)

def build_external_agent_startup_lines():
    python_arg = sys.executable
    cli_arg = str(APP_DIR / 'scripts' / 'webssh_agent_cli.py')
    handoff_arg = str(EXTERNAL_AGENT_HANDOFF_PATH)
    loopback_url = build_external_agent_loopback_base_url(get_external_agent_local_base_url())
    hello_command = quote_local_command([
        python_arg, cli_arg, '--handoff', handoff_arg, '--url', loopback_url,
        *get_external_agent_cli_tls_args(), 'hello',
    ])
    render_command = quote_local_command([
        python_arg, cli_arg, '--handoff', handoff_arg, '--url', loopback_url,
        *get_external_agent_cli_tls_args(), 'render',
    ])
    return [
        f"External Agent Handoff: {EXTERNAL_AGENT_HANDOFF_PATH}",
        "External Agent Handoff is created or refreshed after browser Agent attach and external token mint.",
        f"External Agent CLI hello: {hello_command}",
        f"External Agent CLI render: {render_command}",
        "External Agent multi-terminal tests should pass explicit --url, --token, and --terminal; the handoff file stores the latest minted token.",
    ]

def find_external_agent_dev_state(terminal_id):
    with agent_lock:
        matches = [
            state for state in agent_states.values()
            if state.terminal_id == terminal_id
            and get_bridge(state.session_token, state.terminal_id)
            and is_external_agent_state_visible(state)
        ]
        return matches[-1] if matches else None

@app.route('/agent/external/token', methods=['POST'])
def external_agent_token():
    session_token = get_request_session_token()
    if not session_token:
        return external_agent_json_response(external_agent_error(AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED), 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return external_agent_json_response(external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA), 400)
    terminal_id = validate_terminal_id_payload(data)
    if not terminal_id:
        return external_agent_json_response(external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA), 400)
    mode_version = data.get('mode_version')
    privacy_version = data.get('privacy_version')
    if mode_version is not None:
        try:
            mode_version = int(mode_version)
        except (TypeError, ValueError):
            return external_agent_json_response(external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA), 400)
    if privacy_version is not None:
        try:
            privacy_version = int(privacy_version)
        except (TypeError, ValueError):
            return external_agent_json_response(external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA), 400)
    token, record, error_code = mint_external_agent_attach_token_for_viewer(
        session_token,
        terminal_id,
        data.get('viewer_id'),
        data.get('agent_binding_id'),
        mode_version=mode_version,
        privacy_version=privacy_version,
    )
    if error_code:
        return external_agent_json_response(external_agent_error(error_code, terminal_id=terminal_id), 409)
    return external_agent_json_response(build_external_agent_token_payload(
        token,
        record,
        terminal_id,
        request.host_url.rstrip('/'),
    ))

@app.route('/agent/external/dev-token', methods=['GET', 'POST'])
def external_agent_dev_token():
    if not AGENT_EXTERNAL_DEV_TOKEN_ENABLED or not is_loopback_client_request():
        return external_agent_json_response(external_agent_error(AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED), 403)
    requested_terminal_id = request.args.get('terminal_id') or TERMINAL_ID_MAIN
    if not is_valid_terminal_id(requested_terminal_id):
        return external_agent_json_response(external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA), 400)
    state = find_external_agent_dev_state(requested_terminal_id)
    if not state:
        return external_agent_json_response(
            external_agent_error(AGENT_ERROR_NOT_ATTACHED, terminal_id=requested_terminal_id),
            404,
        )
    token, record, error_code = mint_external_agent_attach_token(
        state.session_token,
        state.terminal_id,
        state.sid,
    )
    if error_code:
        return external_agent_json_response(external_agent_error(error_code, terminal_id=requested_terminal_id), 409)
    payload = build_external_agent_token_payload(
        token,
        record,
        state.terminal_id,
        request.host_url.rstrip('/'),
    )
    payload['dev_token'] = True
    return external_agent_json_response(payload)

@app.route('/agent/external/dev-command', methods=['POST'])
def external_agent_dev_command():
    if not AGENT_EXTERNAL_DEV_TOKEN_ENABLED or not is_loopback_client_request():
        return external_agent_json_response(external_agent_error(AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED), 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return external_agent_json_response(external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA), 400)
    terminal_id = validate_terminal_id_payload(data, default=TERMINAL_ID_MAIN)
    if not terminal_id:
        return external_agent_json_response(external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA), 400)
    state = find_external_agent_dev_state(terminal_id)
    if not state:
        return external_agent_json_response(
            external_agent_error(AGENT_ERROR_NOT_ATTACHED, terminal_id=terminal_id),
            404,
        )
    token, _record, error_code = mint_external_agent_attach_token(
        state.session_token,
        state.terminal_id,
        state.sid,
    )
    if error_code:
        return external_agent_json_response(external_agent_error(error_code, terminal_id=terminal_id), 409)
    command = dict(data)
    command['token'] = token
    command['terminal_id'] = state.terminal_id
    payload = process_external_agent_command(command)
    status_code = 200 if payload.get('status') != AGENT_STATUS_FAILED else 400
    payload['dev_token'] = True
    return external_agent_json_response(payload, status_code)

@app.route('/agent/external/command', methods=['POST'])
def external_agent_command():
    if not is_loopback_client_request():
        return external_agent_json_response(external_agent_error(AGENT_ERROR_EXTERNAL_AGENT_UNAUTHORIZED), 403)
    data = request.get_json(silent=True)
    payload = process_external_agent_command(data)
    status_code = 200 if payload.get('status') != AGENT_STATUS_FAILED else 400
    return external_agent_json_response(payload, status_code)

@app.route('/download_ca')
def download_ca():
    if not is_valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return abort(403, description="Invalid or missing session.")
    if not HTTPS_ENABLED or CLI_ARGS.certfile or CLI_ARGS.keyfile or not LOCAL_CA_CERT_PATH.is_file():
        return abort(404, description="Local CA certificate is not available.")
    response = send_file(
        LOCAL_CA_CERT_PATH,
        mimetype='application/x-x509-ca-cert',
        as_attachment=True,
        download_name='webssh-local-ca.crt',
    )
    return add_common_headers(response)

@socketio.on('connect')
def on_connect():
    ensure_session_cleanup_task()
    cleanup_expired_sessions()
    session_token = get_request_session_token()
    client_ip = get_request_client_ip()
    if not session_token:
        print(f"[!] Unauthorized WebSocket attempt: {request.sid} from {client_ip}")
        return False 
    socket_session_tokens[request.sid] = session_token
    socket_client_ips[request.sid] = client_ip
    socket_browser_authorized[request.sid] = False
    get_agent_session_id(session_token)
    get_agent_viewer_id(request.sid)
    print(f"[+] Client connected: {request.sid} from {client_ip}")

@socketio.on('register_browser_identity')
def on_register_browser_identity(data):
    session_token = socket_session_tokens.get(request.sid)
    if not session_token or not isinstance(data, dict):
        return

    browser_id = data.get('browser_id')
    public_key = data.get('public_key')
    if not validate_browser_identity(browser_id, public_key):
        socketio.emit(
            'browser_authorization_status',
            {
                'status': 'failed',
                'message': 'Browser identity is invalid.',
                'error_code': 'browser_identity_invalid',
            },
            room=request.sid,
        )
        emit_terminal_policy(request.sid)
        return

    socket_browser_identities[request.sid] = {
        'browser_id': browser_id,
        'public_key': public_key,
    }
    socket_browser_authorized[request.sid] = False

    if accept_browser_pairing_file(browser_id, public_key) or is_browser_authorized(browser_id, public_key):
        nonce = secrets.token_urlsafe(32)
        socket_browser_auth_challenges[request.sid] = nonce
        socketio.emit(
            'browser_auth_challenge',
            {
                'nonce': nonce,
                'browser_id': browser_id,
            },
            room=request.sid,
        )
        return

    emit_terminal_policy(request.sid)

@socketio.on('browser_auth_signature')
def on_browser_auth_signature(data):
    session_token = socket_session_tokens.get(request.sid)
    identity = socket_browser_identities.get(request.sid)
    nonce = socket_browser_auth_challenges.pop(request.sid, None)
    if not session_token or not identity or not nonce or not isinstance(data, dict):
        return

    signature = data.get('signature')
    if verify_browser_signature(identity['public_key'], nonce, signature):
        socket_browser_authorized[request.sid] = True
        socketio.emit(
            'browser_authorization_status',
            {
                'status': 'authorized',
                'message': 'Browser authorized for local resources.',
            },
            room=request.sid,
        )
    else:
        socket_browser_authorized[request.sid] = False
        socketio.emit(
            'browser_authorization_status',
            {
                'status': 'failed',
                'message': 'Browser authorization signature failed.',
                'error_code': 'browser_signature_invalid',
            },
            room=request.sid,
        )
    emit_terminal_policy(request.sid)

@socketio.on('request_browser_pairing')
def on_request_browser_pairing():
    session_token = socket_session_tokens.get(request.sid)
    identity = socket_browser_identities.get(request.sid)
    if not session_token:
        return
    if not identity:
        socketio.emit(
            'browser_authorization_status',
            {
                'status': 'failed',
                'message': 'Browser identity is not registered yet. Refresh the page and try again.',
                'error_code': 'browser_identity_missing',
            },
            room=request.sid,
        )
        return

    AUTHORIZED_DIR.mkdir(parents=True, exist_ok=True)
    pairing = build_pairing_file(identity['browser_id'], identity['public_key'])
    filename = f"webssh-authorize_{pairing['pairing_id']}.json"
    socketio.emit(
        'browser_pairing_file',
        {
            'filename': filename,
            'content': json.dumps(pairing, indent=2, sort_keys=True) + '\n',
            'authorized_dir': str(AUTHORIZED_DIR),
            'expires_at': pairing['expires_at'],
        },
        room=request.sid,
    )

@socketio.on('check_browser_pairing')
def on_check_browser_pairing():
    session_token = socket_session_tokens.get(request.sid)
    identity = socket_browser_identities.get(request.sid)
    if not session_token:
        return
    if not identity:
        socketio.emit(
            'browser_authorization_status',
            {
                'status': 'failed',
                'message': 'Browser identity is not registered yet. Refresh the page and try again.',
                'error_code': 'browser_identity_missing',
            },
            room=request.sid,
        )
        return

    if accept_browser_pairing_file(identity['browser_id'], identity['public_key']):
        nonce = secrets.token_urlsafe(32)
        socket_browser_auth_challenges[request.sid] = nonce
        socketio.emit(
            'browser_auth_challenge',
            {
                'nonce': nonce,
                'browser_id': identity['browser_id'],
            },
            room=request.sid,
        )
        return

    socketio.emit(
        'browser_authorization_status',
        {
            'status': 'pending',
            'message': 'Authorization file has not been accepted yet.',
            'error_code': 'browser_pairing_pending',
        },
        room=request.sid,
    )
    emit_terminal_policy(request.sid)

@socketio.on('list_terminals')
def on_list_terminals():
    cleanup_expired_sessions()
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    socketio.emit(
        'terminal_list',
        {
            'terminals': build_terminal_list(session_token),
        },
        room=request.sid,
    )

@socketio.on('refresh_terminal_policy')
def on_refresh_terminal_policy():
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    emit_terminal_policy(request.sid)

@socketio.on('replay_terminal')
def on_replay_terminal(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    if bridge:
        bridge.attach(request.sid)
        bridge.replay_to(request.sid)

@socketio.on(AGENT_EVENT_ATTACH)
def on_agent_attach(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    if not bridge:
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_TERMINAL_NOT_FOUND)
        return
    bridge.attach(request.sid)
    with agent_lock:
        state = get_or_create_agent_state(session_token, terminal_id, request.sid)
        if state.mode == AGENT_MODE_DISABLED:
            state.mode = AGENT_MODE_OBSERVE
            bump_agent_mode_version(state)
        record_agent_audit_event(state, AGENT_AUDIT_VIEWER_ATTACH)
        emit_agent_state(request.sid, state)

@socketio.on(AGENT_EVENT_DETACH)
def on_agent_detach(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    with agent_lock:
        state = get_agent_state(session_token, terminal_id, request.sid)
        if state:
            record_agent_audit_event(state, AGENT_AUDIT_VIEWER_DETACH, reason=AGENT_REASON_DETACHED)
    cancelled = invalidate_agent_states(
        session_token,
        terminal_id=terminal_id,
        sid=request.sid,
        reason=AGENT_REASON_DETACHED,
    )
    for sid, action in cancelled:
        emit_agent_action_result(sid, action, AGENT_REASON_DETACHED, error_code=AGENT_REASON_DETACHED)
    socketio.emit(
        AGENT_EVENT_STATE,
        {
            'session_id': get_agent_session_id(session_token),
            'viewer_id': get_agent_viewer_id(request.sid),
            'agent_binding_id': None,
            'terminal_id': terminal_id,
            'mode': AGENT_MODE_DISABLED,
            'paused': False,
            'control_epoch': None,
            'mode_version': None,
            'privacy_state': AGENT_PRIVACY_NORMAL,
            'privacy_version': None,
            'run_id': None,
            'human_activity_seq': 0,
            'human_activity_at': None,
            'human_input_lease_expires_at': None,
            'human_input_lease_active': False,
            'pending_actions': 0,
        },
        room=request.sid,
    )

@socketio.on(AGENT_EVENT_MODE_SET)
def on_agent_mode_set(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id or not isinstance(data, dict):
        return
    mode = normalize_agent_mode(data.get('mode'))
    if not mode:
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_INVALID_MODE)
        return
    bridge = get_bridge(session_token, terminal_id)
    if not bridge:
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_TERMINAL_NOT_FOUND)
        return
    bridge.attach(request.sid)
    with agent_lock:
        state = get_or_create_agent_state(session_token, terminal_id, request.sid)
        if state.paused and mode != AGENT_MODE_DISABLED:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_PAUSED)
            emit_agent_state(request.sid, state)
            return
        previous_mode = state.mode
        if mode != state.mode:
            bump_agent_mode_version(state)
            state.run_id = None
            cancel_reason = AGENT_REASON_DISABLED if mode == AGENT_MODE_DISABLED else AGENT_REASON_MODE_CHANGED
            for action in cancel_agent_pending_actions(state, cancel_reason):
                emit_agent_action_result(request.sid, action, cancel_reason, error_code=cancel_reason)
        state.mode = mode
        state.paused = False
        record_agent_audit_event(
            state,
            AGENT_AUDIT_MODE_SET,
            previous_mode=previous_mode,
            requested_mode=mode,
        )
        emit_agent_state(request.sid, state)

@socketio.on(AGENT_EVENT_PAUSE)
def on_agent_pause(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    if not get_bridge(session_token, terminal_id):
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_TERMINAL_NOT_FOUND)
        return
    with agent_lock:
        state = get_or_create_agent_state(session_token, terminal_id, request.sid)
        state.paused = True
        state.mode = AGENT_MODE_PAUSED
        set_agent_privacy_state(state, AGENT_PRIVACY_PAUSED)
        bump_agent_mode_version(state)
        state.run_id = None
        cancelled = cancel_agent_pending_actions(state, AGENT_ERROR_PAUSED)
        for action in cancelled:
            emit_agent_action_result(request.sid, action, AGENT_ERROR_PAUSED, error_code=AGENT_ERROR_PAUSED)
        record_agent_audit_event(state, AGENT_AUDIT_PAUSE, cancelled_actions=len(cancelled))
        emit_agent_state(request.sid, state)

@socketio.on(AGENT_EVENT_RESUME)
def on_agent_resume(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    if not get_bridge(session_token, terminal_id):
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_TERMINAL_NOT_FOUND)
        return
    target_mode = AGENT_MODE_OBSERVE
    if isinstance(data, dict) and data.get('mode') is not None:
        requested_mode = normalize_agent_mode(data.get('mode'))
        if requested_mode in {AGENT_MODE_OBSERVE, AGENT_MODE_APPROVAL_PENDING, AGENT_MODE_DIRECT_ACTIVE}:
            target_mode = requested_mode
        else:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_INVALID_MODE)
            return
    with agent_lock:
        state = get_or_create_agent_state(session_token, terminal_id, request.sid)
        state.paused = False
        state.mode = target_mode
        set_agent_privacy_state(state, AGENT_PRIVACY_NORMAL)
        bump_agent_mode_version(state)
        state.run_id = secrets.token_urlsafe(12)
        record_agent_audit_event(state, AGENT_AUDIT_RESUME, requested_mode=target_mode)
        emit_agent_state(request.sid, state)

@socketio.on(AGENT_EVENT_PRIVACY_SET)
def on_agent_privacy_set(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id or not isinstance(data, dict):
        return
    privacy_state = normalize_agent_privacy_state(data.get('privacy_state'))
    if not privacy_state:
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_ACTION_INVALID_DATA)
        return
    if not get_bridge(session_token, terminal_id):
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_TERMINAL_NOT_FOUND)
        return
    with agent_lock:
        state = get_or_create_agent_state(session_token, terminal_id, request.sid)
        cancelled = []
        previous_privacy_state = state.privacy_state
        if privacy_state == AGENT_PRIVACY_PAUSED:
            state.paused = True
            state.mode = AGENT_MODE_PAUSED
            set_agent_privacy_state(state, AGENT_PRIVACY_PAUSED)
            bump_agent_mode_version(state)
            state.run_id = None
            cancelled = cancel_agent_pending_actions(state, AGENT_ERROR_PRIVACY_BLOCKED)
        elif state.paused or state.mode == AGENT_MODE_PAUSED:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_PAUSED)
            emit_agent_state(request.sid, state)
            return
        elif set_agent_privacy_state(state, privacy_state):
            state.run_id = None
            cancelled = cancel_agent_pending_actions(state, AGENT_ERROR_PRIVACY_BLOCKED)
        for action in cancelled:
            emit_agent_action_result(request.sid, action, AGENT_ERROR_PRIVACY_BLOCKED, error_code=AGENT_ERROR_PRIVACY_BLOCKED)
        record_agent_audit_event(
            state,
            AGENT_AUDIT_PRIVACY_SET,
            previous_privacy_state=previous_privacy_state,
            requested_privacy_state=privacy_state,
            cancelled_actions=len(cancelled),
        )
        emit_agent_state(request.sid, state)

def process_agent_terminal_input_proposal(data, proposal_builder,
                                          invalid_proposal_error=AGENT_ERROR_ACTION_NOT_ALLOWED):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id or not isinstance(data, dict):
        return
    bridge = get_bridge(session_token, terminal_id)
    if not bridge:
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_TERMINAL_NOT_FOUND)
        return
    bridge.attach(request.sid)
    with bridge.input_lock:
        with agent_lock:
            state = get_agent_state(session_token, terminal_id, request.sid)
            if not state:
                emit_agent_error(request.sid, terminal_id, AGENT_ERROR_NOT_ATTACHED)
                return
            if state.paused or state.mode == AGENT_MODE_PAUSED:
                emit_agent_error(request.sid, terminal_id, AGENT_ERROR_PAUSED)
                emit_agent_state(request.sid, state)
                return
            if not is_agent_context_allowed(state):
                emit_agent_error(request.sid, terminal_id, AGENT_ERROR_PRIVACY_BLOCKED)
                emit_agent_state(request.sid, state)
                return
            if is_agent_human_input_lease_active(state):
                emit_agent_error(request.sid, terminal_id, AGENT_ERROR_HUMAN_INPUT_ACTIVE)
                emit_agent_state(request.sid, state)
                return
            if state.mode not in {AGENT_MODE_APPROVAL_PENDING, AGENT_MODE_DIRECT_ACTIVE}:
                emit_agent_error(request.sid, terminal_id, AGENT_ERROR_MODE_NOT_WRITABLE)
                emit_agent_state(request.sid, state)
                return
            try:
                proposal = proposal_builder(session_token, terminal_id, request.sid, state, data)
            except AgentProviderError as exc:
                if invalid_proposal_error != AGENT_ERROR_PROVIDER_INVALID_PROPOSAL:
                    raise
                record_agent_audit_event(
                    state,
                    AGENT_AUDIT_PROVIDER_RUN_ERROR,
                    status=AGENT_RUN_STATUS_FAILED,
                    error_code=exc.error_code,
                )
                emit_agent_error(request.sid, terminal_id, exc.error_code, message=exc.message)
                emit_agent_state(request.sid, state)
                return
            except TimeoutError:
                if invalid_proposal_error != AGENT_ERROR_PROVIDER_INVALID_PROPOSAL:
                    raise
                record_agent_audit_event(
                    state,
                    AGENT_AUDIT_PROVIDER_RUN_ERROR,
                    status=AGENT_RUN_STATUS_TIMEOUT,
                    error_code=AGENT_ERROR_PROVIDER_TIMEOUT,
                )
                emit_agent_error(request.sid, terminal_id, AGENT_ERROR_PROVIDER_TIMEOUT)
                emit_agent_state(request.sid, state)
                return
            except Exception:
                if invalid_proposal_error != AGENT_ERROR_PROVIDER_INVALID_PROPOSAL:
                    raise
                record_agent_audit_event(
                    state,
                    AGENT_AUDIT_PROVIDER_RUN_ERROR,
                    status=AGENT_RUN_STATUS_FAILED,
                    error_code=AGENT_ERROR_PROVIDER_FAILED,
                )
                emit_agent_error(request.sid, terminal_id, AGENT_ERROR_PROVIDER_FAILED)
                emit_agent_state(request.sid, state)
                return
            if not isinstance(proposal, dict) or proposal.get('action_type') != AGENT_ACTION_TERMINAL_INPUT:
                if invalid_proposal_error == AGENT_ERROR_PROVIDER_INVALID_PROPOSAL:
                    record_agent_audit_event(
                        state,
                        AGENT_AUDIT_PROVIDER_RUN_ERROR,
                        status=AGENT_RUN_STATUS_FAILED,
                        error_code=invalid_proposal_error,
                    )
                emit_agent_error(request.sid, terminal_id, invalid_proposal_error)
                return
            requires_approval = state.mode != AGENT_MODE_DIRECT_ACTIVE
            action, error_code = build_agent_action(state, proposal, requires_approval)
            if error_code:
                emit_agent_error(request.sid, terminal_id, error_code)
                return
            socketio.emit(AGENT_EVENT_ACTION_REQUEST, public_agent_action(action), room=request.sid)
            emit_agent_state(request.sid, state)

        if requires_approval:
            return

        ok, result = write_agent_terminal_input(
            session_token,
            terminal_id,
            request.sid,
            action['action_id'],
            action['control_epoch'],
            mode_version=action['mode_version'],
            proposal_id=action['proposal_id'],
        )
    status = AGENT_STATUS_COMPLETED if ok else AGENT_STATUS_FAILED
    if result.get('error_code'):
        status = result['error_code']
    emit_agent_action_result(request.sid, action, status, error_code=result.get('error_code'))
    with agent_lock:
        state = get_agent_state(session_token, terminal_id, request.sid)
        if state:
            emit_agent_state(request.sid, state)


def process_external_agent_command(command):
    if not isinstance(command, dict):
        return external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA)
    op = command.get('op')
    if not isinstance(op, str):
        return external_agent_error(AGENT_ERROR_ACTION_INVALID_DATA)
    op = op.strip().lower()

    if op == 'hello':
        record, state, terminal_id, error_code = validate_external_agent_command_token(
            command,
            require_terminal=False,
        )
        if error_code:
            return external_agent_error(error_code, terminal_id=terminal_id)
        return {
            'status': 'ok',
            'version': EXTERNAL_AGENT_PROTOCOL_VERSION,
            'external_agent_id': record.get('external_agent_id'),
            'terminal_id': record.get('terminal_id'),
            'capabilities': list(EXTERNAL_AGENT_CAPABILITIES),
            'state': state.public_state(),
        }

    if op == 'attach':
        record, state, terminal_id, error_code = validate_external_agent_command_token(command)
        if error_code:
            return external_agent_error(error_code, terminal_id=terminal_id)
        with external_agent_lock:
            record, error_code = external_agent_attach_store.mark_attached(command.get('token'))
        if error_code:
            return external_agent_error(error_code, terminal_id=terminal_id)
        record_agent_audit_event(
            state,
            AGENT_AUDIT_EXTERNAL_AGENT_ATTACHED,
            external_agent_id=record.get('external_agent_id'),
        )
        return build_external_agent_state_payload(record, state)

    if op == 'revoke':
        record, state, terminal_id, error_code = validate_external_agent_command_token(command)
        if error_code:
            return external_agent_error(error_code, terminal_id=terminal_id)
        with external_agent_lock:
            record, error_code = external_agent_attach_store.revoke(command.get('token'))
        if error_code:
            return external_agent_error(error_code, terminal_id=terminal_id)
        record_agent_audit_event(
            state,
            AGENT_AUDIT_EXTERNAL_AGENT_REVOKED,
            external_agent_id=record.get('external_agent_id'),
        )
        return {
            'status': 'ok',
            'terminal_id': terminal_id,
            'external_agent_id': record.get('external_agent_id'),
            'revoked': True,
        }

    record, state, terminal_id, error_code = validate_external_agent_command_token(command)
    if error_code:
        return external_agent_error(error_code, terminal_id=terminal_id)

    if op == 'state':
        return build_external_agent_state_payload(record, state)

    if op in {'screen', 'render', 'tail'}:
        if not is_agent_context_allowed(state):
            return external_agent_error(AGENT_ERROR_PRIVACY_BLOCKED, terminal_id=terminal_id)
        bridge = get_bridge(record.get('session_token'), terminal_id)
        if not bridge:
            return external_agent_error(AGENT_ERROR_TERMINAL_NOT_FOUND, terminal_id=terminal_id)
        if op == 'screen':
            screen_options, screen_options_error = parse_external_agent_screen_options(command)
            if screen_options_error:
                return external_agent_error(screen_options_error, terminal_id=terminal_id)
            context = build_agent_context(record.get('session_token'), terminal_id, record.get('sid'))
            active_screen = apply_external_agent_screen_options(
                context.get('active_screen'),
                screen_options,
            )
            record_agent_audit_event(
                state,
                AGENT_AUDIT_EXTERNAL_AGENT_SCREEN,
                external_agent_id=record.get('external_agent_id'),
                context=summarize_agent_context_for_audit(context),
                screen_options=screen_options,
            )
            return {
                'status': 'ok',
                'terminal_id': terminal_id,
                'external_agent_id': record.get('external_agent_id'),
                'output_seq': bridge.output_seq,
                'state': state.public_state(),
                'screen': active_screen,
            }
        if op == 'render':
            render, render_error, request_payload, wait_ms = build_external_agent_viewport_render_payload(
                record,
                state,
                terminal_id,
                bridge,
                wait_ms=command.get('wait_ms'),
            )
            record_agent_audit_event(
                state,
                AGENT_AUDIT_EXTERNAL_AGENT_RENDER,
                external_agent_id=record.get('external_agent_id'),
                request_id=request_payload.get('request_id'),
                status=AGENT_STATUS_FAILED if render_error else 'ok',
                error_code=render_error,
                wait_ms=wait_ms,
                render_type=request_payload.get('render_type'),
                mime_type=request_payload.get('mime_type'),
                image_byte_length=render.get('image_byte_length') if render else None,
                cols=render.get('cols') if render else None,
                rows=render.get('rows') if render else None,
                pixel_width=render.get('pixel_width') if render else None,
                pixel_height=render.get('pixel_height') if render else None,
                output_seq=render.get('output_seq') if render else bridge.output_seq,
            )
            if render_error:
                return external_agent_error(render_error, terminal_id=terminal_id)
            return {
                'status': 'ok',
                'terminal_id': terminal_id,
                'external_agent_id': record.get('external_agent_id'),
                'state': state.public_state(),
                'render': render,
            }
        tail, error_code = build_external_agent_tail_payload_waiting(
            bridge,
            state,
            since_output_seq=command.get('since_output_seq'),
            limit=command.get('limit', AGENT_EXTERNAL_TAIL_MAX_EVENTS),
            wait_ms=command.get('wait_ms'),
        )
        if error_code:
            return external_agent_error(error_code, terminal_id=terminal_id)
        strip_ansi = should_external_agent_strip_ansi(command)
        tail = format_external_agent_tail_payload(tail, strip_ansi=strip_ansi)
        record_agent_audit_event(
            state,
            AGENT_AUDIT_EXTERNAL_AGENT_TAIL,
            external_agent_id=record.get('external_agent_id'),
            event_count=len(tail['events']),
            output_seq=tail['output_seq'],
            wait_ms=parse_external_agent_tail_wait_ms(command.get('wait_ms')),
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
            'wait_ms': parse_external_agent_tail_wait_ms(command.get('wait_ms')),
            'first_available_output_seq': tail['first_available_output_seq'],
            'dropped_before_output_seq': tail['dropped_before_output_seq'],
            'gap': tail['gap'],
            'events': tail['events'],
        }
        if strip_ansi:
            payload['strip_ansi'] = True
            payload['data_format'] = tail['data_format']
        return payload

    if op in {'send', 'send-wait'}:
        data = command.get('data')
        bridge = get_bridge(record.get('session_token'), terminal_id)
        if not bridge:
            return external_agent_error(AGENT_ERROR_TERMINAL_NOT_FOUND, terminal_id=terminal_id)
        capture_requested = op == 'send-wait' or should_external_agent_capture_send(command)
        strip_ansi = should_external_agent_strip_ansi(command)
        before_output_seq = None
        with bridge.input_lock:
            with agent_lock:
                if state.paused or state.mode == AGENT_MODE_PAUSED:
                    return external_agent_error(AGENT_ERROR_PAUSED, terminal_id=terminal_id)
                if not is_agent_context_allowed(state):
                    return external_agent_error(AGENT_ERROR_PRIVACY_BLOCKED, terminal_id=terminal_id)
                if is_agent_human_input_lease_active(state):
                    return external_agent_error(AGENT_ERROR_HUMAN_INPUT_ACTIVE, terminal_id=terminal_id)
                if state.mode not in {AGENT_MODE_APPROVAL_PENDING, AGENT_MODE_DIRECT_ACTIVE}:
                    return external_agent_error(AGENT_ERROR_MODE_NOT_WRITABLE, terminal_id=terminal_id)
                proposal = external_agent_build_terminal_input_action(state, data)
                requires_approval = state.mode != AGENT_MODE_DIRECT_ACTIVE
                action, error_code = build_agent_action(state, proposal, requires_approval)
                if error_code:
                    return external_agent_error(error_code, terminal_id=terminal_id)
                record_agent_audit_event(
                    state,
                    AGENT_AUDIT_EXTERNAL_AGENT_SEND,
                    action=action,
                    external_agent_id=record.get('external_agent_id'),
                    capture_requested=capture_requested,
                    strip_ansi=strip_ansi if capture_requested else False,
                )
                socketio.emit(AGENT_EVENT_ACTION_REQUEST, public_agent_action(action), room=state.sid)
                emit_agent_state(state.sid, state)
            if requires_approval:
                payload = public_agent_action(action)
                payload['status'] = AGENT_STATUS_PENDING_APPROVAL
                if capture_requested:
                    payload['capture'] = {
                        'status': 'skipped',
                        'reason': 'pending_approval',
                        'requested': True,
                    }
                return payload
            with bridge.output_condition:
                before_output_seq = bridge.output_seq
            ok, result = write_agent_terminal_input(
                record.get('session_token'),
                terminal_id,
                state.sid,
                action['action_id'],
                action['control_epoch'],
                mode_version=action['mode_version'],
                proposal_id=action['proposal_id'],
            )
        status = AGENT_STATUS_COMPLETED if ok else AGENT_STATUS_FAILED
        if result.get('error_code'):
            status = result['error_code']
        emit_agent_action_result(state.sid, action, status, error_code=result.get('error_code'))
        with agent_lock:
            current_state = get_agent_state(record.get('session_token'), terminal_id, state.sid)
            if current_state:
                emit_agent_state(state.sid, current_state)
        payload = public_agent_action(action)
        payload['status'] = status
        if result.get('error_code'):
            payload['error_code'] = result.get('error_code')
        payload['bytes_written'] = result.get('bytes_written', 0)
        if capture_requested and ok:
            capture, capture_error = build_external_agent_send_capture_payload(
                bridge,
                state,
                before_output_seq if isinstance(before_output_seq, int) else 0,
                limit=command.get('limit', AGENT_EXTERNAL_TAIL_MAX_EVENTS),
                wait_ms=command.get('wait_ms'),
                settle_ms=command.get('settle_ms'),
                strip_ansi=strip_ansi,
            )
            if capture_error:
                payload['capture'] = {
                    'status': AGENT_STATUS_FAILED,
                    'error_code': capture_error,
                    'requested': True,
                }
            else:
                capture['requested'] = True
                payload['capture'] = capture
                payload['before_output_seq'] = capture['before_output_seq']
                payload['after_output_seq'] = capture['output_seq']
        return payload

    return external_agent_error(AGENT_ERROR_ACTION_NOT_ALLOWED, terminal_id=terminal_id)


@socketio.on(AGENT_EVENT_SUGGESTION_REQUEST)
def on_agent_suggestion_request(data):
    process_agent_terminal_input_proposal(
        data,
        lambda _session_token, _terminal_id, _sid, state, payload: (
            AGENT_BRIDGE.create_terminal_input_action(state, payload)
        ),
    )


@socketio.on(AGENT_EVENT_PROVIDER_RUN_REQUEST)
def on_agent_provider_run_request(data):
    def build_provider_proposal(session_token, terminal_id, sid, state, _payload):
        provider = get_agent_provider()
        provider_metadata = provider.metadata()
        run_id = create_agent_run_id()
        state.run_id = run_id
        record_agent_audit_event(
            state,
            AGENT_AUDIT_PROVIDER_RUN_REQUEST,
            run_id=run_id,
            status=AGENT_RUN_STATUS_REQUESTED,
            **provider_metadata,
        )
        context = build_agent_context(session_token, terminal_id, sid)
        record_agent_audit_event(
            state,
            AGENT_AUDIT_CONTEXT_BUILT,
            context=summarize_agent_context_for_audit(context),
            run_id=run_id,
            **provider_metadata,
        )
        record_agent_audit_event(
            state,
            AGENT_AUDIT_PROVIDER_RUN_START,
            run_id=run_id,
            status=AGENT_RUN_STATUS_RUNNING,
            **provider_metadata,
        )
        proposal = provider.create_terminal_input_proposal(
            context,
            {
                'run_id': run_id,
                'provider_name': provider_metadata['provider_name'],
                'provider_version': provider_metadata['provider_version'],
            },
        )
        if isinstance(proposal, dict):
            proposal = dict(proposal)
            proposal['run_id'] = run_id
            proposal['provider_name'] = provider_metadata['provider_name']
            proposal['provider_version'] = provider_metadata['provider_version']
            proposal['provider_status'] = AGENT_RUN_STATUS_COMPLETED
            if proposal.get('action_type') == AGENT_ACTION_TERMINAL_INPUT:
                record_agent_audit_event(
                    state,
                    AGENT_AUDIT_PROVIDER_RUN_COMPLETE,
                    run_id=run_id,
                    status=AGENT_RUN_STATUS_COMPLETED,
                    **provider_metadata,
                )
        return proposal

    process_agent_terminal_input_proposal(
        data,
        build_provider_proposal,
        invalid_proposal_error=AGENT_ERROR_PROVIDER_INVALID_PROPOSAL,
    )

@socketio.on(AGENT_EVENT_ACTION_APPROVE)
def on_agent_action_approve(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id or not isinstance(data, dict):
        return
    action_id = data.get('action_id')
    proposal_id = data.get('proposal_id')
    if not isinstance(action_id, str) and not isinstance(proposal_id, str):
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_ACTION_NOT_FOUND)
        return
    with agent_lock:
        state = get_agent_state(session_token, terminal_id, request.sid)
        if not state:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_NOT_ATTACHED)
            return
        if data.get('session_id') is not None and data.get('session_id') != state.session_id:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_STALE_PROPOSAL)
            emit_agent_state(request.sid, state)
            return
        if data.get('viewer_id') is not None and data.get('viewer_id') != state.viewer_id:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_STALE_PROPOSAL)
            emit_agent_state(request.sid, state)
            return
        if data.get('agent_binding_id') is not None and data.get('agent_binding_id') != state.agent_binding_id:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_STALE_PROPOSAL)
            emit_agent_state(request.sid, state)
            return
        if data.get('mode_version') is not None and data.get('mode_version') != state.mode_version:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_STALE_MODE_VERSION)
            emit_agent_state(request.sid, state)
            return
        if data.get('privacy_version') is not None and data.get('privacy_version') != state.privacy_version:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_STALE_PROPOSAL)
            emit_agent_state(request.sid, state)
            return
        action = state.pending_actions.get(action_id) if isinstance(action_id, str) else None
        if not action and isinstance(proposal_id, str):
            action = next(
                (
                    candidate for candidate in state.pending_actions.values()
                    if candidate.get('proposal_id') == proposal_id
                ),
                None,
            )
        if not action:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_ACTION_NOT_FOUND)
            emit_agent_state(request.sid, state)
            return
        if isinstance(proposal_id, str) and action.get('proposal_id') != proposal_id:
            emit_agent_action_result(request.sid, action, AGENT_ERROR_STALE_PROPOSAL, error_code=AGENT_ERROR_STALE_PROPOSAL)
            emit_agent_state(request.sid, state)
            return
        action_id = action['action_id']
        if state.paused or state.mode == AGENT_MODE_PAUSED:
            emit_agent_action_result(request.sid, action, AGENT_ERROR_PAUSED, error_code=AGENT_ERROR_PAUSED)
            emit_agent_state(request.sid, state)
            return
        if not is_agent_context_allowed(state):
            emit_agent_action_result(request.sid, action, AGENT_ERROR_PRIVACY_BLOCKED, error_code=AGENT_ERROR_PRIVACY_BLOCKED)
            emit_agent_state(request.sid, state)
            return
        if action.get('status') != AGENT_STATUS_PENDING_APPROVAL:
            emit_agent_action_result(request.sid, action, AGENT_ERROR_ACTION_NOT_PENDING, error_code=AGENT_ERROR_ACTION_NOT_PENDING)
            emit_agent_state(request.sid, state)
            return
        if action.get('control_epoch') != state.control_epoch:
            emit_agent_action_result(request.sid, action, AGENT_ERROR_STALE_ACTION, error_code=AGENT_ERROR_STALE_ACTION)
            emit_agent_state(request.sid, state)
            return
        action['status'] = AGENT_STATUS_APPROVED
        record_agent_audit_event(state, AGENT_AUDIT_ACTION_APPROVE, action=action)
        record_agent_audit(state, action, AGENT_STATUS_APPROVED)
        control_epoch = state.control_epoch

    ok, result = write_agent_terminal_input(
        session_token,
        terminal_id,
        request.sid,
        action_id,
        control_epoch,
        mode_version=action['mode_version'],
        proposal_id=action['proposal_id'],
    )
    status = AGENT_STATUS_COMPLETED if ok else AGENT_STATUS_FAILED
    if result.get('error_code'):
        status = result['error_code']
    emit_agent_action_result(request.sid, action, status, error_code=result.get('error_code'))
    with agent_lock:
        state = get_agent_state(session_token, terminal_id, request.sid)
        if state:
            emit_agent_state(request.sid, state)

@socketio.on(AGENT_EVENT_ACTION_REJECT)
def on_agent_action_reject(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id or not isinstance(data, dict):
        return
    action_id = data.get('action_id')
    proposal_id = data.get('proposal_id')
    if not isinstance(action_id, str) and not isinstance(proposal_id, str):
        emit_agent_error(request.sid, terminal_id, AGENT_ERROR_ACTION_NOT_FOUND)
        return
    with agent_lock:
        state = get_agent_state(session_token, terminal_id, request.sid)
        if not state:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_NOT_ATTACHED)
            return
        action = state.pending_actions.get(action_id) if isinstance(action_id, str) else None
        if not action and isinstance(proposal_id, str):
            action = next(
                (
                    candidate for candidate in state.pending_actions.values()
                    if candidate.get('proposal_id') == proposal_id
                ),
                None,
            )
        if not action:
            emit_agent_error(request.sid, terminal_id, AGENT_ERROR_ACTION_NOT_FOUND)
            emit_agent_state(request.sid, state)
            return
        if isinstance(proposal_id, str) and action.get('proposal_id') != proposal_id:
            emit_agent_action_result(request.sid, action, AGENT_ERROR_STALE_PROPOSAL, error_code=AGENT_ERROR_STALE_PROPOSAL)
            emit_agent_state(request.sid, state)
            return
        action['status'] = AGENT_STATUS_REJECTED
        record_agent_audit_event(state, AGENT_AUDIT_ACTION_REJECT, action=action)
        record_agent_audit(state, action, AGENT_STATUS_REJECTED)
        emit_agent_action_result(request.sid, action, AGENT_STATUS_REJECTED)
        emit_agent_state(request.sid, state)


@socketio.on(AGENT_EVENT_VIEWPORT_SNAPSHOT)
def on_agent_viewport_snapshot(data):
    session_token = socket_session_tokens.get(request.sid)
    snapshot, error_code = validate_agent_viewport_snapshot_payload(data)
    terminal_id = snapshot['terminal_id'] if snapshot else validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    result_payload = {
        'terminal_id': terminal_id,
        'status': 'failed',
    }
    if error_code:
        result_payload['error_code'] = error_code
        socketio.emit(AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT, result_payload, room=request.sid)
        return

    bridge = get_bridge(session_token, terminal_id)
    if not bridge:
        result_payload['error_code'] = AGENT_ERROR_TERMINAL_NOT_FOUND
        socketio.emit(AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT, result_payload, room=request.sid)
        return
    if request.sid not in bridge.attached_sids:
        result_payload['error_code'] = AGENT_ERROR_NOT_ATTACHED
        socketio.emit(AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT, result_payload, room=request.sid)
        return

    stored, store_error = agent_viewport_snapshot_store.put(
        session_token,
        terminal_id,
        request.sid,
        snapshot,
    )
    if store_error:
        result_payload.update({
            'status': 'stale' if store_error == AGENT_ERROR_SNAPSHOT_STALE else 'failed',
            'error_code': store_error,
            'snapshot_seq': snapshot.get('snapshot_seq'),
        })
        socketio.emit(AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT, result_payload, room=request.sid)
        return

    socketio.emit(
        AGENT_EVENT_VIEWPORT_SNAPSHOT_RESULT,
        {
            'terminal_id': terminal_id,
            'status': 'accepted',
            'snapshot_seq': stored['snapshot_seq'],
            'cols': stored['cols'],
            'rows': stored['rows'],
            'line_count': stored['line_count'],
            'byte_length': stored['byte_length'],
            'stored_at': stored['stored_at'],
        },
        room=request.sid,
    )

@socketio.on(AGENT_EVENT_VIEWPORT_RENDER_RESULT)
def on_agent_viewport_render_result(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    request_id = data.get('request_id') if isinstance(data, dict) else None
    if not session_token or not terminal_id or not isinstance(request_id, str):
        return
    bridge = get_bridge(session_token, terminal_id)
    if not bridge or request.sid not in bridge.attached_sids:
        agent_viewport_render_request_store.fail(
            session_token,
            terminal_id,
            request.sid,
            request_id,
            AGENT_ERROR_TERMINAL_NOT_FOUND if not bridge else AGENT_ERROR_NOT_ATTACHED,
        )
        return
    with agent_lock:
        state = get_agent_state(session_token, terminal_id, request.sid)
        if (
            not state
            or state.paused
            or state.mode == AGENT_MODE_PAUSED
            or state.mode == AGENT_MODE_DISABLED
            or not is_agent_context_allowed(state)
        ):
            if not state:
                error_code = AGENT_ERROR_NOT_ATTACHED
            elif state.paused or state.mode == AGENT_MODE_PAUSED:
                error_code = AGENT_ERROR_PAUSED
            elif state.mode == AGENT_MODE_DISABLED:
                error_code = AGENT_ERROR_EXTERNAL_AGENT_DISABLED
            else:
                error_code = AGENT_ERROR_PRIVACY_BLOCKED
            agent_viewport_render_request_store.fail(
                session_token,
                terminal_id,
                request.sid,
                request_id,
                error_code,
            )
            return
    agent_viewport_render_request_store.resolve(
        session_token,
        terminal_id,
        request.sid,
        data,
    )


@socketio.on('start_ssh')
def on_start_ssh(data):
    cleanup_expired_sessions()
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    client_ip = socket_client_ips.get(request.sid, 'unknown')
    payload, validation_error = validate_start_ssh_payload(
        data,
        client_ip,
        browser_authorized=socket_browser_authorized.get(request.sid, False),
    )
    if validation_error:
        if isinstance(validation_error, dict):
            message = validation_error.get('message', 'Invalid connection payload.')
            error_code = validation_error.get('error_code', 'invalid_start_ssh_payload')
        else:
            message = validation_error
            error_code = 'invalid_start_ssh_payload'
        terminal_id = validate_terminal_id_payload(data, default=TERMINAL_ID_MAIN) or TERMINAL_ID_MAIN
        emit_connection_error(request.sid, message, error_code=error_code, terminal_id=terminal_id)
        return

    pending_localhost_key_setups.pop(request.sid, None)
    terminal_id = payload['terminal_id']
    replacing_existing = get_bridge(session_token, terminal_id) is not None
    if not replacing_existing and len(bridges.get(session_token, {})) >= MAX_TERMINALS_PER_CLIENT:
        emit_connection_error(
            request.sid,
            'Terminal limit reached.',
            error_code='terminal_limit_reached',
            terminal_id=terminal_id,
        )
        return

    connection_type = payload['connection_type']
    plugin = TERMINAL_BACKEND_REGISTRY.get(connection_type)
    if not plugin:
        emit_connection_error(
            request.sid,
            'Connection type must be ssh, local_shell, or uart.',
            error_code='invalid_start_ssh_payload',
            terminal_id=terminal_id,
        )
        return
    cols = 80
    rows = 24
    bridge = None
    try:
        bridge = plugin.create_bridge(session_token, terminal_id, payload)
        if not isinstance(bridge, TerminalBridge):
            raise TypeError('Backend did not return a terminal bridge.')
        if bridge.connection_type != connection_type:
            raise ValueError('Backend returned a bridge with a mismatched connection type.')
        bridge.attach(request.sid)
        success, result = plugin.connect_bridge(bridge, payload, cols, rows)
    except Exception as exc:
        print(f"[!] Backend start error for {connection_type}: {exc}")
        close_bridge(bridge)
        emit_connection_error(
            request.sid,
            'Connection failed.',
            error_code='backend_start_failed',
            terminal_id=terminal_id,
        )
        return

    if success:
        close_terminal_bridge(session_token, terminal_id)
        bridge.update_terminal_size(cols, rows)
        set_bridge(session_token, terminal_id, bridge)
        connected_payload = {'message_type': 'ssh_connected'}
        connected_payload.update(bridge.metadata())
        bridge.emit_output(connected_payload)
        socketio.start_background_task(target=bridge.read_loop)
        return

    failure = plugin.build_connection_failure(request.sid, bridge, payload, result)
    close_bridge(bridge)
    emit_connection_error(
        request.sid,
        failure['message'],
        error_code=failure.get('error_code'),
        action_type=failure.get('action_type'),
        action_message=failure.get('action_message'),
        action_question=failure.get('action_question'),
        action_id=failure.get('action_id'),
        terminal_id=terminal_id,
    )

@socketio.on('setup_localhost_key_access')
def on_setup_localhost_key_access(data):
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    data = data if isinstance(data, dict) else {}
    action_id = data.get('action_id')
    pending_setup, pending_error_code = get_pending_localhost_key_setup(request.sid, action_id)
    bridge = SSHBridge(session_token, pending_setup.get('terminal_id', TERMINAL_ID_MAIN) if pending_setup else TERMINAL_ID_MAIN)

    if not pending_setup:
        result = {
            'status': 'failed',
            'message': 'No pending localhost key setup request is available.',
            'error_code': pending_error_code,
        }
    elif not bridge._can_offer_local_key_setup(pending_setup['username']):
        pending_localhost_key_setups.pop(request.sid, None)
        result = {
            'status': 'failed',
            'message': 'Automatic localhost key setup is only available for the current local user.',
            'error_code': 'localhost_key_setup_unavailable',
        }
    else:
        pending_localhost_key_setups.pop(request.sid, None)
        _, result = bridge._append_public_key_entry_to_authorized_keys(pending_setup['key_entry'])

    socketio.emit(
        'ssh_output',
        {
            'message_type': 'setup_result',
            'terminal_id': bridge.terminal_id,
            'message': result['message'],
            'setup_status': result['status'],
            'error_code': result.get('error_code'),
        },
        room=request.sid,
    )

@socketio.on('ssh_input')
def on_ssh_input(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    if not bridge:
        return
    bridge.attach(request.sid)
    ssh_input = data.get('data')
    if not isinstance(ssh_input, str):
        return
    if len(ssh_input.encode('utf-8', errors='ignore')) > MAX_SSH_INPUT_BYTES:
        return
    log_terminal_input(request.sid, terminal_id, ssh_input)
    with bridge.input_lock:
        with agent_lock:
            state = get_agent_state(session_token, terminal_id, request.sid)
            privacy_state = state.privacy_state if state else AGENT_PRIVACY_NORMAL
            updated_states = note_agent_human_input_for_terminal(session_token, terminal_id)
        agent_user_input_metadata_store.append_input(session_token, terminal_id, ssh_input, privacy_state=privacy_state)
        bridge.write(ssh_input)
    for updated_state in updated_states:
        emit_agent_state(updated_state.sid, updated_state)

@socketio.on('resize')
def on_resize(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    size = parse_terminal_size(data)
    if bridge and size:
        cols, rows = size
        bridge.update_terminal_size(cols, rows)
        bridge.resize(cols, rows)

@socketio.on('close_terminal')
def on_close_terminal(data):
    session_token = socket_session_tokens.get(request.sid)
    terminal_id = validate_terminal_id_payload(data)
    if not session_token or not terminal_id:
        return
    bridge = get_bridge(session_token, terminal_id)
    if bridge:
        bridge.emit_output({
            'message_type': 'ssh_closed',
            'message': 'Terminal session closed.',
        })
    close_terminal_bridge(session_token, terminal_id)

@socketio.on('close_all_terminals')
def on_close_all_terminals():
    session_token = socket_session_tokens.get(request.sid)
    if not session_token:
        return
    session_sids = get_session_sids(session_token)
    close_all_terminal_bridges(session_token)
    for sid in session_sids:
        socketio.emit(
            'terminal_list',
            {
                'terminals': [],
            },
            room=sid,
        )

@socketio.on('disconnect')
def on_disconnect(reason=None):
    pending_localhost_key_setups.pop(request.sid, None)
    session_token = socket_session_tokens.pop(request.sid, None)
    client_ip = socket_client_ips.pop(request.sid, 'unknown')
    socket_browser_identities.pop(request.sid, None)
    socket_browser_authorized.pop(request.sid, None)
    socket_browser_auth_challenges.pop(request.sid, None)
    agent_viewer_ids.pop(request.sid, None)
    if session_token:
        with agent_lock:
            for state in [
                state for state in agent_states.values()
                if state.session_token == session_token and state.sid == request.sid
            ]:
                record_agent_audit_event(state, AGENT_AUDIT_VIEWER_DETACH, reason=AGENT_REASON_DISCONNECTED)
        invalidate_agent_states(session_token, sid=request.sid, reason=AGENT_REASON_DISCONNECTED)
        agent_viewport_snapshot_store.discard(session_token, sid=request.sid)
        agent_viewport_render_request_store.discard(session_token, sid=request.sid)
        detach_session_bridges(session_token, request.sid)
    print(f"[-] Client disconnected: {request.sid} from {client_ip}; reason={reason or 'unknown'}")

def is_wsl():
    if not sys.platform.startswith('linux'):
        return False

    if os.getenv('WSL_DISTRO_NAME'):
        return True

    try:
        with open('/proc/version', 'r', encoding='utf-8') as version_file:
            return 'microsoft' in version_file.read().lower()
    except OSError:
        return False

def get_primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_wsl_ip():
    global wsl_ip_cache
    if wsl_ip_cache:
        return wsl_ip_cache
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, check=False)
        ips = result.stdout.strip().split()
        if ips:
            wsl_ip_cache = ips[0]
            return wsl_ip_cache
    except Exception:
        pass

    primary_ip = get_primary_ip()
    if primary_ip != "127.0.0.1":
        wsl_ip_cache = primary_ip
    return primary_ip

def get_bind_host():
    if DEFAULT_BIND_HOST:
        return DEFAULT_BIND_HOST

    if is_wsl():
        return "0.0.0.0"

    return "127.0.0.1"

def get_access_host(bind_host):
    if is_wsl() and bind_host in {"0.0.0.0", "::"}:
        return get_wsl_ip()
    if bind_host in {"0.0.0.0", "::"}:
        return get_primary_ip()

    return bind_host

def get_url_scheme():
    return "https" if HTTPS_ENABLED else "http"

def get_localhost_access_url(port):
    return f"{get_url_scheme()}://127.0.0.1:{port}/?token={ACCESS_TOKEN}"

def get_ssl_context(bind_host, access_host):
    if CLI_ARGS.certfile or CLI_ARGS.keyfile:
        if not CLI_ARGS.certfile or not CLI_ARGS.keyfile:
            raise SystemExit('--certfile and --keyfile must be provided together.')
        return CLI_ARGS.certfile, CLI_ARGS.keyfile
    if HTTPS_ENABLED:
        return ensure_local_https_certificates(bind_host, access_host)
    return None

def is_loopback_bind(bind_host):
    if not isinstance(bind_host, str):
        return False
    normalized = bind_host.strip().lower().strip('[]')
    if normalized in {'127.0.0.1', 'localhost', '::1'}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False

def should_enable_https(bind_host):
    if HTTPS_REQUESTED:
        return True
    if HTTPS_AUTO_DISABLED:
        return False
    return not is_loopback_bind(bind_host)

def normalize_ip_address(value):
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    ipv4_mapped = getattr(address, 'ipv4_mapped', None)
    if ipv4_mapped:
        return ipv4_mapped
    return address

def get_wsl_host_addresses():
    addresses = set()
    resolv_conf = Path('/etc/resolv.conf')
    try:
        for line in resolv_conf.read_text(encoding='utf-8').splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0] == 'nameserver':
                address = normalize_ip_address(parts[1])
                if address:
                    addresses.add(address)
    except OSError:
        pass

    try:
        result = subprocess.run(
            ['ip', 'route', 'show', 'default'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if 'via' not in parts:
                continue
            gateway = parts[parts.index('via') + 1]
            address = normalize_ip_address(gateway)
            if address:
                addresses.add(address)
    except Exception:
        pass

    try:
        result = subprocess.run(
            [
                'powershell.exe',
                '-NoProfile',
                '-Command',
                'Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.IPAddress }',
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                address = normalize_ip_address(line.strip())
                if address:
                    addresses.add(address)
    except Exception:
        pass

    route_path = Path('/proc/net/route')
    try:
        for line in route_path.read_text(encoding='utf-8').splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) < 3 or parts[1] != '00000000':
                continue
            gateway_hex = parts[2]
            if len(gateway_hex) != 8:
                continue
            gateway_bytes = bytes.fromhex(gateway_hex)[::-1]
            address = normalize_ip_address('.'.join(str(byte) for byte in gateway_bytes))
            if address:
                addresses.add(address)
    except Exception:
        pass
    return addresses

def is_local_client_ip(client_ip):
    address = normalize_ip_address(client_ip)
    if not address:
        return False
    if address.is_loopback:
        return True
    if is_wsl() and address in get_wsl_host_addresses():
        return True
    if is_wsl() and is_wsl_nat_client_ip(address):
        return True
    return False

def is_wsl_nat_client_ip(address):
    if not getattr(address, 'version', None) == 4 or not address.is_private:
        return False
    wsl_address = normalize_ip_address(get_wsl_ip())
    if not wsl_address or getattr(wsl_address, 'version', None) != 4 or not wsl_address.is_private:
        return False
    # WSL NAT host/client addresses can differ outside /24. A /20 fallback
    # covers the vEthernet subnet without treating all RFC1918 clients as local.
    try:
        return address in ipaddress.ip_network(f'{wsl_address}/20', strict=False)
    except ValueError:
        return False

def is_local_shell_allowed_for_client(client_ip, browser_authorized=False):
    if os.getenv('WEBSSH_ALLOW_REMOTE_LOCAL_SHELL') == '1':
        return True
    if browser_authorized:
        return True
    return is_local_client_ip(client_ip)

def is_uart_allowed_for_client(client_ip, browser_authorized=False):
    if os.getenv('WEBSSH_ALLOW_REMOTE_UART') == '1':
        return True
    if browser_authorized:
        return True
    return is_local_client_ip(client_ip)

def is_local_shell_enabled():
    return DEFAULT_CONNECTION_TYPE == CONNECTION_TYPE_LOCAL_SHELL or FORCE_CONNECTION_TYPE == CONNECTION_TYPE_LOCAL_SHELL

def is_uart_enabled():
    return DEFAULT_CONNECTION_TYPE == CONNECTION_TYPE_UART or FORCE_CONNECTION_TYPE == CONNECTION_TYPE_UART

def get_runtime_name():
    if is_wsl():
        return "WSL"
    if sys.platform == 'darwin':
        return "macOS"
    if sys.platform.startswith('win'):
        return "Windows"
    return "Linux"

if __name__ == '__main__':
    print("[*] Python imports completed; resolving bind host...", flush=True)
    bind_host = get_bind_host()
    access_host = get_access_host(bind_host)
    port = DEFAULT_PORT
    HTTPS_ENABLED = should_enable_https(bind_host)
    ssl_context = get_ssl_context(bind_host, access_host)
    scheme = get_url_scheme()
    print("\n" + "="*60)
    print(f"WebSSH Server Starting...")
    print(f"Runtime: {get_runtime_name()}")
    print(f"Async Mode: {ASYNC_MODE}")
    print(f"Debug Input: {'on' if DEBUG_INPUT else 'off'}")
    print(f"Debug Policy: {'on' if DEBUG_POLICY else 'off'}")
    print(f"HTTPS: {'on' if HTTPS_ENABLED else 'off'}")
    if HTTPS_ENABLED and not HTTPS_REQUESTED:
        print("HTTPS Mode: auto-enabled because bind host is non-loopback.")
    print(f"Default Connection: {DEFAULT_CONNECTION_TYPE}")
    if FORCE_CONNECTION_TYPE:
        print(f"Forced Connection: {FORCE_CONNECTION_TYPE}")
    print(f"Access URL: {scheme}://{access_host}:{port}/?token={ACCESS_TOKEN}")
    print(f"Listening on: {bind_host}:{port}")
    for line in build_external_agent_startup_lines():
        print(line)
    if is_wsl() and not is_loopback_bind(bind_host):
        print(f"WSL Localhost URL: {get_localhost_access_url(port)}")
        print("WSL Localhost URL is useful when browsers require a secure context for authorization.")
    if HTTPS_ENABLED and not (CLI_ARGS.certfile and CLI_ARGS.keyfile):
        print(f"HTTPS Local CA: {LOCAL_CA_CERT_PATH}")
        print("Import the HTTPS Local CA into Windows Trusted Root Certification Authorities to trust the WSL IP URL.")
    if is_local_shell_enabled() and not is_loopback_bind(bind_host) and os.getenv('WEBSSH_ALLOW_REMOTE_LOCAL_SHELL') != '1':
        print("[!] WARNING: Local Shell is enabled while listening on a non-loopback address.")
        print("[!] Set WEBSSH_ALLOW_REMOTE_LOCAL_SHELL=1 to acknowledge this deployment mode.")
    if is_uart_enabled() and not is_loopback_bind(bind_host) and os.getenv('WEBSSH_ALLOW_REMOTE_UART') != '1':
        print("[!] WARNING: UART is enabled while listening on a non-loopback address.")
        print("[!] Set WEBSSH_ALLOW_REMOTE_UART=1 to acknowledge this deployment mode.")
    if sys.platform == 'darwin':
        print("Tip: Enable Remote Login in macOS if you want localhost SSH access.")
    print("="*60 + "\n")
    
    sys.stdout.flush()

    run_kwargs = {'host': bind_host, 'port': port, 'log_output': False}
    if ssl_context:
        run_kwargs['ssl_context'] = ssl_context
    if ASYNC_MODE == 'threading':
        run_kwargs['allow_unsafe_werkzeug'] = True

    socketio.run(app, **run_kwargs)
