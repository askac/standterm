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
from collections import deque
from pathlib import Path
from flask import Flask, render_template, request, abort, make_response, redirect, send_file
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
TERMINAL_ID_MAIN = 'main'
MAX_TERMINAL_ID_LENGTH = 64
MAX_TERMINALS_PER_CLIENT = 12
MAX_TERMINAL_REPLAY_EVENTS = 1000
MAX_TERMINAL_REPLAY_BYTES = 200000
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
    if normalized in {CONNECTION_TYPE_SSH, CONNECTION_TYPE_LOCAL_SHELL, CONNECTION_TYPE_UART}:
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
    client_ip = get_request_client_ip()
    local_shell_allowed = is_local_shell_allowed_for_client(client_ip, browser_authorized=browser_authorized)
    uart_allowed = is_uart_allowed_for_client(client_ip, browser_authorized=browser_authorized)
    uart_ports = detect_serial_ports() if uart_allowed else []

    allowed_connections = {
        CONNECTION_TYPE_SSH: True,
        CONNECTION_TYPE_LOCAL_SHELL: local_shell_allowed,
        CONNECTION_TYPE_UART: uart_allowed,
    }
    default_connection = DEFAULT_CONNECTION_TYPE
    if not allowed_connections.get(default_connection):
        default_connection = CONNECTION_TYPE_LOCAL_SHELL if local_shell_allowed else CONNECTION_TYPE_SSH
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
        'connection_options': [
            {
                'connection_type': CONNECTION_TYPE_SSH,
                'label': 'SSH',
                'allowed': True,
            },
            {
                'connection_type': CONNECTION_TYPE_LOCAL_SHELL,
                'label': 'Local Shell',
                'allowed': local_shell_allowed,
                'authorization_available': not local_shell_allowed,
                'browser_authorized': bool(browser_authorized),
            },
            {
                'connection_type': CONNECTION_TYPE_UART,
                'label': 'UART',
                'allowed': uart_allowed,
                'available_ports': uart_ports,
                'default_baud_rate': DEFAULT_UART_BAUD_RATE,
                'baud_rates': UART_BAUD_RATES,
            },
        ],
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
        self.replay_buffer = deque()
        self.replay_buffer_bytes = 0

    def metadata(self, cols=80, rows=24):
        return build_terminal_metadata(
            self.connection_type,
            self.terminal_id,
            self.terminal_kind,
            self.terminal_label,
            cols,
            rows,
        )

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
            self._remember_terminal_payload(payload)
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

    def __init__(self, sid, terminal_id=TERMINAL_ID_MAIN):
        super().__init__(sid, terminal_id)
        self.process = None
        shell = get_windows_shell_path() if sys.platform.startswith('win') else (os.environ.get('SHELL') or '/bin/sh')
        self.shell = shell
        self.terminal_kind = get_windows_shell_kind(shell) if sys.platform.startswith('win') else get_shell_kind(shell)
        self.terminal_label = get_shell_label(self.terminal_kind)

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
                [self.shell],
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

bridges = {}
pending_localhost_key_setups = {}
active_sessions = {}
socket_session_tokens = {}
socket_client_ips = {}
socket_browser_identities = {}
socket_browser_authorized = {}
socket_browser_auth_challenges = {}
session_cleanup_task_started = False

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
    close_bridge(bridge)

def close_terminal_bridge(session_token, terminal_id):
    close_bridge(pop_bridge(session_token, terminal_id))

def close_all_terminal_bridges(session_token):
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
    if FORCE_CONNECTION_TYPE and connection_type != FORCE_CONNECTION_TYPE:
        return None, f'Connection type is locked to {FORCE_CONNECTION_TYPE}.'

    terminal_id = validate_terminal_id_payload(data, default=TERMINAL_ID_MAIN)
    if not terminal_id:
        return None, 'Invalid terminal id.'

    if connection_type == CONNECTION_TYPE_LOCAL_SHELL:
        if not is_local_shell_allowed_for_client(client_ip, browser_authorized=browser_authorized):
            return None, {
                'message': 'Local Shell is not available for this client.',
                'error_code': 'local_shell_unavailable_for_client',
            }
        return {
            'connection_type': connection_type,
            'terminal_id': terminal_id,
        }, None

    if connection_type == CONNECTION_TYPE_UART:
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
            'connection_type': connection_type,
            'terminal_id': terminal_id,
            'serial_port': port_info['device'],
            'serial_port_info': port_info,
            'baud_rate': baud_rate,
        }, None

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
        'connection_type': connection_type,
        'terminal_id': terminal_id,
        'host': host,
        'port': port,
        'username': user,
        'password': password,
    }, None

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
                'message': 'Browser authorized for Local Shell.',
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
    cols = 80
    rows = 24
    if connection_type == CONNECTION_TYPE_LOCAL_SHELL:
        bridge = LocalShellBridge(session_token, terminal_id)
        bridge.attach(request.sid)
        success, result = bridge.connect(cols=cols, rows=rows)
    elif connection_type == CONNECTION_TYPE_UART:
        bridge = UARTBridge(
            session_token,
            terminal_id,
            payload['serial_port_info'],
            payload['baud_rate'],
        )
        bridge.attach(request.sid)
        success, result = bridge.connect(cols=cols, rows=rows)
    else:
        host = payload['host']
        port = payload['port']
        user = payload['username']
        password = payload['password']
        bridge = SSHBridge(session_token, terminal_id)
        bridge.attach(request.sid)
        success, result = bridge.connect(host, port, user, password, cols=cols, rows=rows)

    if success:
        close_terminal_bridge(session_token, terminal_id)
        set_bridge(session_token, terminal_id, bridge)
        connected_payload = {'message_type': 'ssh_connected'}
        connected_payload.update(bridge.metadata(cols=cols, rows=rows))
        bridge.emit_output(connected_payload)
        socketio.start_background_task(target=bridge.read_loop)
        return

    if connection_type in {CONNECTION_TYPE_LOCAL_SHELL, CONNECTION_TYPE_UART}:
        message = 'Connection failed.'
        error_code = None
        if isinstance(result, dict):
            message = result.get('message', message)
            error_code = result.get('error_code')
        elif result:
            message = str(result)

        close_bridge(bridge)
        emit_connection_error(
            request.sid,
            message,
            error_code=error_code,
            terminal_id=terminal_id,
        )
        return

    host = payload['host']
    port = payload['port']
    user = payload['username']
    password = payload['password']
    message = 'Connection failed.'
    error_code = None
    action_type = None
    action_message = None
    action_question = None
    if isinstance(result, dict):
        message = result.get('message', message)
        error_code = result.get('error_code')
        action_type = result.get('action_type')
        action_message = result.get('action_message')
        action_question = result.get('action_question')
    elif result:
        message = str(result)

    action_id = None
    if action_type == 'offer_localhost_key_setup':
        missing_entries = bridge._get_missing_local_public_keys()
        if missing_entries:
            action_id = secrets.token_urlsafe(16)
            pending_localhost_key_setups[request.sid] = {
                'action_id': action_id,
                'host': host,
                'port': port,
                'username': user,
                'terminal_id': terminal_id,
                'key_entry': missing_entries[0],
                'expires_at': time.time() + LOCALHOST_KEY_SETUP_TTL_SECONDS,
            }
        else:
            action_type = None
            action_message = None
            action_question = None

    close_bridge(bridge)
    emit_connection_error(
        request.sid,
        message,
        error_code=error_code,
        action_type=action_type,
        action_message=action_message,
        action_question=action_question,
        action_id=action_id,
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
    bridge.write(ssh_input)

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
    if session_token:
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
