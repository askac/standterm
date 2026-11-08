"""Migrate one known predecessor before disposing of managed runtime files."""

import hashlib
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

spec = importlib.util.spec_from_file_location('standterm_runtime', Path(__file__).with_name('runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

DATA_FILES = ('browsers.json', 'session_recovery_credentials.json')
DATA_SCHEMA = {'version': 1, 'browsers': 1, 'sessionRecovery': 1}
MAX_DATA_BYTES = 4 * 1024 * 1024


class MigrationError(Exception):
    pass


def atomic_json(path, value):
    runtime.check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.upgrade-', delete=False) as output:
        temporary = Path(output.name)
        output.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def read_data(root):
    schema = root / '.standterm-data-schema.json'
    runtime.check_path(schema)
    if schema.exists() and runtime.read_marker(schema) != DATA_SCHEMA:
        raise MigrationError('Unsupported Core data schema.')
    result = {}
    for name in DATA_FILES:
        path = root / 'authorized' / name
        runtime.check_path(path)
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > MAX_DATA_BYTES:
            raise MigrationError('Invalid Core data file.')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise MigrationError('Unreadable Core data file.') from error
        if name == 'browsers.json':
            if not isinstance(data, dict) or set(data) != {'browsers'} or not isinstance(data['browsers'], list):
                raise MigrationError('Unsupported browser authorization format.')
            for entry in data['browsers']:
                if (not isinstance(entry, dict) or set(entry) != {'browser_id', 'public_key', 'authorized_at'}
                        or not isinstance(entry['browser_id'], str) or not re.fullmatch('[a-f0-9]{64}', entry['browser_id'])
                        or not isinstance(entry['public_key'], str) or type(entry['authorized_at']) is not int):
                    raise MigrationError('Invalid browser authorization entry.')
        else:
            if (not isinstance(data, dict) or set(data) != {'version', 'credentials'}
                    or data['version'] != 1 or not isinstance(data['credentials'], list)):
                raise MigrationError('Unsupported recovery credential format.')
            allowed = {'credential_id', 'credential_public_key', 'sign_count', 'rp_id', 'created_at',
                       'last_used_at', 'device_type', 'backed_up', 'transports'}
            for entry in data['credentials']:
                if (not isinstance(entry, dict) or set(entry) - allowed
                        or any(not isinstance(entry.get(key), str) for key in ['credential_id', 'credential_public_key', 'rp_id'])
                        or type(entry.get('sign_count')) is not int or entry['sign_count'] < 0):
                    raise MigrationError('Invalid recovery credential entry.')
        result[name] = data
    return result


def fingerprints(data):
    return {name: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            for name, value in data.items()}


def predecessor(current, hint=None):
    active = runtime.runtime_base() / 'active-runtime.json'
    runtime.check_path(active)
    if active.exists():
        value = runtime.read_marker(active)
        if not isinstance(value, dict) or value.get('version') != 1:
            raise MigrationError('Invalid active runtime record.')
        identity = value.get('id')
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
            raise MigrationError('Invalid active runtime identity.')
        if identity == current.name:
            return None
    else:
        identity = hint
    if identity is None or identity == current.name:
        return None
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
        raise MigrationError('Invalid predecessor identity.')
    root = current.parent / identity
    runtime.check_path(root)
    if not root.exists():
        return None
    if runtime.read_marker(root / '.standterm-bundle.json') != {'id': identity}:
        raise MigrationError('Unverified predecessor.')
    return root


def supported(current):
    return (sys.platform != 'darwin' and current.parent == runtime.runtime_base() / 'runtimes'
            and runtime.managed_identity(current) == current.name)


def migrate(current, hint=None):
    if not supported(current):
        return
    with runtime.lease(runtime.runtime_base()), runtime.lease(current):
        if runtime.read_marker(current / '.desktop-ready.json') != {'id': current.name}:
            raise MigrationError('Destination is not prepared.')
        policy = current / '.standterm-retention.json'
        runtime.check_path(policy)
        if not policy.exists():
            # Cleanup is enabled only by the completed installer phase. An
            # interrupted unchecked install must never acquire implicit consent.
            atomic_json(policy, {'version': 1, 'cleanArtifacts': False})
        receipt_path = current / '.standterm-migration.json'
        runtime.check_path(receipt_path)
        receipt = runtime.read_marker(receipt_path) if receipt_path.exists() else None
        source = predecessor(current, hint)
        if receipt is not None:
            if (not isinstance(receipt, dict) or receipt.get('version') != 1 or receipt.get('destination') != current.name
                    or receipt.get('schema') != DATA_SCHEMA or type(receipt.get('complete')) is not bool
                    or not isinstance(receipt.get('files'), dict) or set(receipt['files']) - set(DATA_FILES)
                    or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value)
                           for value in receipt['files'].values())):
                raise MigrationError('Invalid migration receipt.')
            if source is None and receipt['complete']:
                read_data(current)
                return
            if source is None:
                raise MigrationError('Incomplete migration has no verified predecessor.')
        target = read_data(current)
        if source is None:
            atomic_json(current / '.standterm-data-schema.json', DATA_SCHEMA)
            atomic_json(receipt_path, {'version': 1, 'destination': current.name, 'source': None,
                'schema': DATA_SCHEMA, 'files': fingerprints(target), 'complete': True})
            return
        with runtime.lease(source, create=False):
            marker = runtime.read_marker(source / '.standterm-venv.json')
            if marker not in [{'id': source.name, 'lease': 1}, {'id': source.name, 'lease': 2}]:
                raise MigrationError('Predecessor does not support a lifetime lease.')
            data = read_data(source)
            source_hashes, target_hashes = fingerprints(data), fingerprints(target)
            if receipt is not None and receipt.get('source') == source.name:
                if receipt['complete'] and source_hashes == receipt['files']:
                    return
                before = receipt['files'] if receipt['complete'] else receipt.get('before', {})
                if (not isinstance(before, dict) or any(target_hashes.get(name) not in
                        [before.get(name), receipt['files'].get(name)] for name in DATA_FILES)):
                    raise MigrationError('Destination changed during migration.')
            elif any(name not in data or value != data[name] for name, value in target.items()):
                raise MigrationError('Destination data differs from the predecessor.')
            # Keep source and destination locked through copy, read-back and
            # receipt publication. Empty/missing source data never imports older grants.
            pending = {'version': 1, 'destination': current.name, 'source': source.name,
                'schema': DATA_SCHEMA, 'before': target_hashes, 'files': source_hashes, 'complete': False}
            atomic_json(receipt_path, pending)
            for name, value in data.items():
                if target.get(name) != value:
                    atomic_json(current / 'authorized' / name, value)
            for name in set(target) - set(data):
                (current / 'authorized' / name).unlink()
            atomic_json(current / '.standterm-data-schema.json', DATA_SCHEMA)
            if read_data(current) != data:
                raise MigrationError('Migrated data verification failed.')
            atomic_json(receipt_path, {key: value for key, value in {**pending, 'complete': True}.items() if key != 'before'})


def finish(current, clean):
    if not supported(current):
        return []
    with runtime.lease(runtime.runtime_base()), runtime.lease(current):
        receipt = runtime.read_marker(current / '.standterm-migration.json')
        if receipt.get('destination') != current.name or receipt.get('complete') is not True:
            raise MigrationError('Migration is incomplete.')
        read_data(current)
        with verified_predecessor(current, receipt):
            atomic_json(current / '.standterm-retention.json', {'version': 1, 'cleanArtifacts': clean})
            atomic_json(runtime.runtime_base() / 'active-runtime.json', {'version': 1, 'id': current.name})
        return cleanup(current) if clean else []


@contextmanager
def verified_predecessor(current, receipt):
    identity = receipt.get('source')
    if identity is None:
        yield
        return
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity) or identity == current.name:
        raise MigrationError('Invalid migration source.')
    source = current.parent / identity
    runtime.check_path(source)
    if not source.exists():
        yield
        return
    with runtime.lease(source, create=False):
        if (runtime.read_marker(source / '.standterm-bundle.json') != {'id': identity}
                or fingerprints(read_data(source)) != receipt.get('files')):
            raise MigrationError('Predecessor changed after migration. Retry migration.')
        yield


def cleanup(current):
    spec = importlib.util.spec_from_file_location('standterm_cleanup', Path(__file__).with_name('runtime_cleanup.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.prune_obsolete(current)


def activate(current):
    if not supported(current):
        return []
    # The backend already holds current's lifetime lease.
    with runtime.lease(runtime.runtime_base()):
        receipt = runtime.read_marker(current / '.standterm-migration.json')
        if receipt.get('destination') != current.name or receipt.get('complete') is not True:
            raise MigrationError('Migration is incomplete.')
        with verified_predecessor(current, receipt):
            atomic_json(runtime.runtime_base() / 'active-runtime.json', {'version': 1, 'id': current.name})
        return cleanup(current)
