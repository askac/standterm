"""Verify migrated credentials using Core writers/readers and generated keys."""

import base64
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime_smoke import RetentionTests, upgrade

fixture = RetentionTests()
fixture.setUp()
try:
    os.environ['STANDTERM_DISABLE_AGENTINFO_CURRENT'] = '1'
    os.environ['STANDTERM_ACCESS_UI'] = 'off'
    os.environ['STANDTERM_AGENT_RUNTIME_DIR'] = str(fixture.base / 'agent')
    os.environ['STANDTERM_SESSION_RECOVERY_STORE'] = str(fixture.base / 'recovery.json')
    import app as core
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import cbor2
    from session_recovery import SessionRecoveryCredentialStore, base64url_encode
    from webauthn.helpers import decode_credential_public_key

    previous, current = [fixture.fixture(version) for version in [1, 2]]
    key = ec.generate_private_key(ec.SECP256R1())
    public_bytes = key.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    public_key = base64.b64encode(public_bytes).decode('ascii')
    browser_id = core.build_browser_id(public_key)
    with patch.object(core, 'AUTHORIZED_DIR', previous / 'authorized'), \
            patch.object(core, 'AUTHORIZED_BROWSERS_PATH', previous / 'authorized/browsers.json'):
        core.authorize_browser(browser_id, public_key)
    numbers = key.public_key().public_numbers()
    cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
    credential = {'credential_id': base64url_encode(b'isolated-runtime-credential'),
                  'credential_public_key': base64url_encode(cose), 'sign_count': 7, 'rp_id': 'localhost',
                  'created_at': 1, 'last_used_at': 2, 'device_type': 'single_device',
                  'backed_up': False, 'transports': ['internal']}
    SessionRecoveryCredentialStore(previous / 'authorized/session_recovery_credentials.json').save(credential)
    fixture.migrate(current, previous)
    with patch.object(core, 'AUTHORIZED_BROWSERS_PATH', current / 'authorized/browsers.json'):
        assert core.validate_browser_identity(browser_id, public_key)
        assert core.is_browser_authorized(browser_id, public_key)
        restored_key = serialization.load_der_public_key(base64.b64decode(core.get_authorized_browser(browser_id)['public_key']))
        message = b'migrated credential verification'
        restored_key.verify(key.sign(message, ec.ECDSA(hashes.SHA256())),
                            message, ec.ECDSA(hashes.SHA256()))
    restored = SessionRecoveryCredentialStore(current / 'authorized/session_recovery_credentials.json').get('localhost', credential['credential_id'])
    assert restored == credential
    assert decode_credential_public_key(cose).x == numbers.x.to_bytes(32, 'big')
    upgrade.finish(current, True)
    assert not previous.exists()
    print('Core migration smoke passed: browser identity/signature and recovery credential read-back.')
finally:
    fixture.doCleanups()
