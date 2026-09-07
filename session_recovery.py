import base64
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.helpers.exceptions import WebAuthnException


STORE_VERSION = 1
CEREMONY_TTL_SECONDS = 120
CEREMONY_LIMIT = 128
CEREMONY_START_LIMIT = 12
CEREMONY_START_WINDOW_SECONDS = 60
MAX_CREDENTIAL_ID_BYTES = 1024
DOMAIN_LABEL_PATTERN = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
BASE64URL_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')
CREDENTIAL_RECORD_FIELDS = (
    'credential_id',
    'credential_public_key',
    'sign_count',
    'rp_id',
    'created_at',
    'last_used_at',
    'device_type',
    'backed_up',
    'transports',
)


class SessionRecoveryError(Exception):
    def __init__(self, error_code, message, status_code=400):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code


def base64url_encode(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def base64url_decode(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or not BASE64URL_PATTERN.fullmatch(value)
    ):
        raise SessionRecoveryError(
            'session_recovery_invalid_credential',
            'The platform credential is invalid.',
        )
    try:
        decoded = base64.urlsafe_b64decode(value + ('=' * (-len(value) % 4)))
    except (ValueError, TypeError) as exc:
        raise SessionRecoveryError(
            'session_recovery_invalid_credential',
            'The platform credential is invalid.',
        ) from exc
    if not decoded or len(decoded) > MAX_CREDENTIAL_ID_BYTES:
        raise SessionRecoveryError(
            'session_recovery_invalid_credential',
            'The platform credential is invalid.',
        )
    return decoded


def normalize_credential_id(value):
    return base64url_encode(base64url_decode(value))


def build_webauthn_context(url_root):
    try:
        parsed = urllib.parse.urlsplit(url_root)
        hostname = (parsed.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
    except (UnicodeError, ValueError) as exc:
        raise SessionRecoveryError(
            'session_recovery_origin_unsupported',
            'Platform recovery requires a valid browser hostname.',
        ) from exc

    if not hostname or parsed.username or parsed.password:
        raise SessionRecoveryError(
            'session_recovery_origin_unsupported',
            'Platform recovery requires a valid browser hostname.',
        )
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split('.')
        if any(not DOMAIN_LABEL_PATTERN.fullmatch(label) for label in labels):
            raise SessionRecoveryError(
                'session_recovery_origin_unsupported',
                'Platform recovery requires a valid browser hostname.',
            )
    else:
        raise SessionRecoveryError(
            'session_recovery_ip_origin_unsupported',
            'Platform recovery cannot use an IP address. Open StandTerm through localhost or a stable hostname.',
        )

    scheme = parsed.scheme.lower()
    if scheme != 'https' and not (scheme == 'http' and hostname == 'localhost'):
        raise SessionRecoveryError(
            'session_recovery_secure_origin_required',
            'Platform recovery requires HTTPS, except for http://localhost.',
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise SessionRecoveryError(
            'session_recovery_origin_unsupported',
            'Platform recovery requires a valid browser origin.',
        ) from exc
    default_port = 443 if scheme == 'https' else 80
    port_suffix = f':{port}' if port and port != default_port else ''
    return {
        'rp_id': hostname,
        'origin': f'{scheme}://{hostname}{port_suffix}',
    }


class SessionRecoveryCredentialStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _empty(self):
        return {'version': STORE_VERSION, 'credentials': []}

    def _load_locked(self):
        if not self.path.is_file():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise SessionRecoveryError(
                'session_recovery_store_unavailable',
                'The platform recovery security store is unavailable.',
                503,
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get('version') != STORE_VERSION
            or not isinstance(data.get('credentials'), list)
        ):
            raise SessionRecoveryError(
                'session_recovery_store_invalid',
                'The platform recovery security store is invalid.',
                503,
            )
        return data

    def _write_locked(self, data):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f'.{self.path.name}.',
                suffix='.tmp',
                dir=self.path.parent,
            )
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                    json.dump(data, handle, indent=2, sort_keys=True)
                    handle.write('\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temporary_name, 0o600)
                except OSError:
                    pass
                os.replace(temporary_name, self.path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise SessionRecoveryError(
                'session_recovery_store_unavailable',
                'The platform recovery security store is unavailable.',
                503,
            ) from exc

    def list_for_rp(self, rp_id):
        with self._lock:
            data = self._load_locked()
            return [
                dict(record)
                for record in data['credentials']
                if isinstance(record, dict) and record.get('rp_id') == rp_id
            ]

    def get(self, rp_id, credential_id):
        for record in self.list_for_rp(rp_id):
            if secrets.compare_digest(str(record.get('credential_id', '')), credential_id):
                return record
        return None

    def save(self, record):
        record = {
            field: record[field]
            for field in CREDENTIAL_RECORD_FIELDS
            if field in record
        }
        rp_id = record.get('rp_id')
        credential_id = record.get('credential_id')
        if not isinstance(rp_id, str) or not isinstance(credential_id, str):
            raise SessionRecoveryError(
                'session_recovery_invalid_credential',
                'The platform credential is invalid.',
            )
        with self._lock:
            data = self._load_locked()
            credentials = [
                existing
                for existing in data['credentials']
                if not (
                    isinstance(existing, dict)
                    and existing.get('rp_id') == rp_id
                    and existing.get('credential_id') == credential_id
                )
            ]
            credentials.append(dict(record))
            data['credentials'] = sorted(
                credentials,
                key=lambda item: (str(item.get('rp_id', '')), str(item.get('credential_id', ''))),
            )
            self._write_locked(data)

    def update_authentication(self, rp_id, credential_id, sign_count, device_type, backed_up):
        with self._lock:
            data = self._load_locked()
            updated = False
            for record in data['credentials']:
                if not isinstance(record, dict):
                    continue
                if record.get('rp_id') != rp_id or record.get('credential_id') != credential_id:
                    continue
                record['sign_count'] = int(sign_count)
                record['device_type'] = device_type
                record['backed_up'] = bool(backed_up)
                record['last_used_at'] = int(time.time())
                updated = True
                break
            if not updated:
                raise SessionRecoveryError(
                    'session_recovery_credential_unknown',
                    'This platform credential is not registered with StandTerm.',
                    403,
                )
            self._write_locked(data)

    def remove_for_rp(self, rp_id):
        with self._lock:
            data = self._load_locked()
            removed_ids = [
                record.get('credential_id')
                for record in data['credentials']
                if isinstance(record, dict) and record.get('rp_id') == rp_id
            ]
            data['credentials'] = [
                record
                for record in data['credentials']
                if not (isinstance(record, dict) and record.get('rp_id') == rp_id)
            ]
            if removed_ids:
                self._write_locked(data)
            return [value for value in removed_ids if isinstance(value, str)]


class SessionRecoveryCeremonyStore:
    def __init__(self, time_func=None):
        self._entries = {}
        self._starts = {}
        self._lock = threading.RLock()
        self._time_func = time_func or time.monotonic

    def _prune_locked(self, now):
        for ceremony_id, entry in list(self._entries.items()):
            if now > entry.get('expires_at', 0):
                self._entries.pop(ceremony_id, None)
        for actor, starts in list(self._starts.items()):
            recent = deque(
                started_at
                for started_at in starts
                if now - started_at <= CEREMONY_START_WINDOW_SECONDS
            )
            if recent:
                self._starts[actor] = recent
            else:
                self._starts.pop(actor, None)

    def create(self, kind, actor, **fields):
        now = self._time_func()
        actor = str(actor or 'unknown')
        with self._lock:
            self._prune_locked(now)
            starts = self._starts.setdefault(actor, deque())
            if len(starts) >= CEREMONY_START_LIMIT:
                raise SessionRecoveryError(
                    'session_recovery_rate_limited',
                    'Too many platform recovery attempts. Try again shortly.',
                    429,
                )
            starts.append(now)
            if len(self._entries) >= CEREMONY_LIMIT:
                oldest = min(
                    self._entries,
                    key=lambda key: self._entries[key].get('created_at', 0),
                )
                self._entries.pop(oldest, None)
            ceremony_id = secrets.token_urlsafe(24)
            self._entries[ceremony_id] = {
                'kind': kind,
                'created_at': now,
                'expires_at': now + CEREMONY_TTL_SECONDS,
                **fields,
            }
            return ceremony_id

    def consume(self, ceremony_id, kind):
        if not isinstance(ceremony_id, str) or not ceremony_id:
            raise SessionRecoveryError(
                'session_recovery_ceremony_invalid',
                'The platform recovery request is invalid or expired.',
                403,
            )
        now = self._time_func()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.pop(ceremony_id, None)
        if not entry or entry.get('kind') != kind or now > entry.get('expires_at', 0):
            raise SessionRecoveryError(
                'session_recovery_ceremony_invalid',
                'The platform recovery request is invalid or expired.',
                403,
            )
        return entry

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._starts.clear()


class SessionRecoveryService:
    def __init__(self, credential_store):
        self.credential_store = credential_store
        self.ceremonies = SessionRecoveryCeremonyStore()
        self._bindings = {}
        self._bindings_lock = threading.RLock()

    def _options_payload(self, options, ceremony_id):
        payload = json.loads(options_to_json(options))
        payload['ceremony_id'] = ceremony_id
        return payload

    def get_status(self, url_root, session_token):
        context = build_webauthn_context(url_root)
        credentials = self.credential_store.list_for_rp(context['rp_id'])
        credential_ids = {record.get('credential_id') for record in credentials}
        with self._bindings_lock:
            armed_count = sum(
                1
                for key, bound_session in self._bindings.items()
                if key[0] == context['rp_id']
                and key[1] in credential_ids
                and bound_session == session_token
            )
        return {
            'status': 'ok',
            'available': True,
            'configured_credentials': len(credentials),
            'armed_credentials': armed_count,
            'rp_id': context['rp_id'],
            'origin': context['origin'],
            'scope': 'live_backend_session',
        }

    def begin_registration(self, url_root, session_token, actor):
        context = build_webauthn_context(url_root)
        challenge = secrets.token_bytes(32)
        user_id = secrets.token_bytes(32)
        exclude_credentials = []
        for record in self.credential_store.list_for_rp(context['rp_id']):
            try:
                exclude_credentials.append(PublicKeyCredentialDescriptor(
                    id=base64url_decode(record.get('credential_id')),
                ))
            except SessionRecoveryError:
                continue
        ceremony_id = self.ceremonies.create(
            'registration',
            actor,
            challenge=challenge,
            rp_id=context['rp_id'],
            origin=context['origin'],
            session_token=session_token,
        )
        options = generate_registration_options(
            rp_id=context['rp_id'],
            rp_name='StandTerm',
            user_id=user_id,
            user_name='StandTerm local operator',
            user_display_name='StandTerm local operator',
            challenge=challenge,
            timeout=CEREMONY_TTL_SECONDS * 1000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude_credentials,
        )
        return self._options_payload(options, ceremony_id)

    def finish_registration(self, url_root, session_token, ceremony_id, credential):
        entry = self.ceremonies.consume(ceremony_id, 'registration')
        context = build_webauthn_context(url_root)
        if (
            not secrets.compare_digest(str(entry.get('session_token', '')), session_token)
            or entry.get('rp_id') != context['rp_id']
            or entry.get('origin') != context['origin']
        ):
            raise SessionRecoveryError(
                'session_recovery_ceremony_invalid',
                'The platform recovery request is invalid or expired.',
                403,
            )
        if not isinstance(credential, dict):
            raise SessionRecoveryError(
                'session_recovery_invalid_credential',
                'The platform credential is invalid.',
            )
        try:
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=entry['challenge'],
                expected_rp_id=context['rp_id'],
                expected_origin=context['origin'],
                require_user_presence=True,
                require_user_verification=True,
            )
        except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
            raise SessionRecoveryError(
                'session_recovery_verification_failed',
                'Platform credential verification failed.',
                403,
            ) from exc

        credential_id = base64url_encode(verification.credential_id)
        transports = credential.get('response', {}).get('transports', [])
        if not isinstance(transports, list):
            transports = []
        record = {
            'credential_id': credential_id,
            'credential_public_key': base64url_encode(verification.credential_public_key),
            'sign_count': int(verification.sign_count),
            'rp_id': context['rp_id'],
            'created_at': int(time.time()),
            'device_type': verification.credential_device_type.value,
            'backed_up': bool(verification.credential_backed_up),
            'transports': [
                value
                for value in transports
                if isinstance(value, str) and len(value) <= 32
            ],
        }
        self.credential_store.save(record)
        self.bind(context['rp_id'], credential_id, session_token)
        return {
            'status': 'ok',
            'credential_id': credential_id,
            'scope': 'live_backend_session',
            'backed_up': bool(verification.credential_backed_up),
        }

    def begin_authentication(self, url_root, actor, bound_only=False):
        context = build_webauthn_context(url_root)
        credentials = self.credential_store.list_for_rp(context['rp_id'])
        if not credentials:
            raise SessionRecoveryError(
                'session_recovery_not_configured',
                'No platform recovery credential is registered for this StandTerm hostname.',
                404,
            )
        if bound_only:
            credentials = [
                record
                for record in credentials
                if self.get_binding(
                    context['rp_id'],
                    str(record.get('credential_id', '')),
                )
            ]
            if not credentials:
                raise SessionRecoveryError(
                    'session_recovery_no_live_session',
                    'No live StandTerm session is armed for platform recovery. Enter the current access token.',
                    409,
                )
        allow_credentials = []
        for record in credentials:
            try:
                allow_credentials.append(PublicKeyCredentialDescriptor(
                    id=base64url_decode(record.get('credential_id')),
                ))
            except SessionRecoveryError:
                continue
        if not allow_credentials:
            raise SessionRecoveryError(
                'session_recovery_store_invalid',
                'The platform recovery security store is invalid.',
                503,
            )
        challenge = secrets.token_bytes(32)
        ceremony_id = self.ceremonies.create(
            'authentication',
            actor,
            challenge=challenge,
            rp_id=context['rp_id'],
            origin=context['origin'],
        )
        options = generate_authentication_options(
            rp_id=context['rp_id'],
            challenge=challenge,
            timeout=CEREMONY_TTL_SECONDS * 1000,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return self._options_payload(options, ceremony_id)

    def finish_authentication(self, url_root, ceremony_id, credential):
        entry = self.ceremonies.consume(ceremony_id, 'authentication')
        context = build_webauthn_context(url_root)
        if entry.get('rp_id') != context['rp_id'] or entry.get('origin') != context['origin']:
            raise SessionRecoveryError(
                'session_recovery_ceremony_invalid',
                'The platform recovery request is invalid or expired.',
                403,
            )
        if not isinstance(credential, dict):
            raise SessionRecoveryError(
                'session_recovery_invalid_credential',
                'The platform credential is invalid.',
            )
        credential_id = normalize_credential_id(credential.get('id'))
        record = self.credential_store.get(context['rp_id'], credential_id)
        if not record:
            raise SessionRecoveryError(
                'session_recovery_credential_unknown',
                'This platform credential is not registered with StandTerm.',
                403,
            )
        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=entry['challenge'],
                expected_rp_id=context['rp_id'],
                expected_origin=context['origin'],
                credential_public_key=base64url_decode(record.get('credential_public_key')),
                credential_current_sign_count=int(record.get('sign_count', 0)),
                require_user_verification=True,
            )
        except (WebAuthnException, KeyError, TypeError, ValueError) as exc:
            raise SessionRecoveryError(
                'session_recovery_verification_failed',
                'Platform credential verification failed.',
                403,
            ) from exc
        self.credential_store.update_authentication(
            context['rp_id'],
            credential_id,
            verification.new_sign_count,
            verification.credential_device_type.value,
            verification.credential_backed_up,
        )
        return {
            'credential_id': credential_id,
            'rp_id': context['rp_id'],
            'backed_up': bool(verification.credential_backed_up),
        }

    def bind(self, rp_id, credential_id, session_token):
        with self._bindings_lock:
            self._bindings[(rp_id, credential_id)] = session_token

    def get_binding(self, rp_id, credential_id):
        with self._bindings_lock:
            return self._bindings.get((rp_id, credential_id))

    def unbind_credential(self, rp_id, credential_id):
        with self._bindings_lock:
            self._bindings.pop((rp_id, credential_id), None)

    def unbind_session(self, session_token):
        with self._bindings_lock:
            for key, bound_session in list(self._bindings.items()):
                if bound_session == session_token:
                    self._bindings.pop(key, None)

    def remove_credentials(self, url_root):
        context = build_webauthn_context(url_root)
        removed_ids = self.credential_store.remove_for_rp(context['rp_id'])
        for credential_id in removed_ids:
            self.unbind_credential(context['rp_id'], credential_id)
        return {
            'status': 'ok',
            'removed_credentials': len(removed_ids),
        }

    def clear_runtime_state(self):
        self.ceremonies.clear()
        with self._bindings_lock:
            self._bindings.clear()
