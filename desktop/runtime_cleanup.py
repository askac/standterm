"""Dispose of migrated runtimes and recoverably detach venvs at uninstall."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import uuid

spec = importlib.util.spec_from_file_location('standterm_runtime', Path(__file__).with_name('runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def owned_venv(root, identity=None):
    identity = identity or root.name
    return runtime.read_marker(root / '.standterm-venv.json') in [
        {'id': identity, 'lease': 1}, {'id': identity, 'lease': 2}]


def validate_files(files, identity):
    if (not isinstance(files, dict) or len(files) > 4096
            or (files and hashlib.sha256(json.dumps(files, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest() != identity)):
        raise runtime.UnsafeRuntime()
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or not relative.parts or '..' in relative.parts or '\\' in name or ':' in name
                or relative.parts[0].startswith('.') or relative.parts[0] in ['authorized', 'certs', 'tools']
                or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
            raise runtime.UnsafeRuntime()
    return files


def core_files(root):
    manifest = root / '.standterm-files.json'
    runtime.check_path(manifest)
    if manifest.exists():
        if manifest.stat().st_size > 1024 * 1024:
            raise runtime.UnsafeRuntime()
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1 or data.get('id') != root.name:
            raise runtime.UnsafeRuntime()
        files = validate_files(data.get('files'), root.name)
        if not files:
            raise runtime.UnsafeRuntime()
    else:
        # Older releases did not save the file map. A complete map matching the
        # bundle digest proves which files are reproducible without guessing.
        files = {}
        for parent, directories, names in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name != '__pycache__'
                and not (Path(parent) == root and (name.startswith('.') or name in ['tools', 'authorized', 'certs']))]
            for name in directories + names:
                target = Path(parent) / name
                if Path(parent) == root and (name.startswith('.') or name == 'setup.log'):
                    continue
                runtime.check_path(target)
                if target.is_file():
                    files[target.relative_to(root).as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        files = dict(sorted(files.items()))
        if hashlib.sha256(json.dumps(files, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest() != root.name:
            files = {}
    for name in files:
        runtime.check_path(root / name)
    return files


def dispose(root, identity, files, archive, *, legacy=False):
    venv = runtime.venv_path(root)
    runtime.check_path(venv)
    (root / '.desktop-ready.json').unlink(missing_ok=True)
    if venv.exists():
        shutil.rmtree(venv)
    for name, digest in files.items():
        target = root / name
        runtime.check_path(target)
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            target.unlink()
    # Never unlock and delete an old Windows lease inode. Its minimal stub is
    # retained; all later runtimes use stable locks outside their directories.
    if legacy:
        return
    controls = {'.setup.lock', '.standterm-bundle.json', '.standterm-venv.json', '.standterm-files.json',
                '.desktop-ready.json', '.standterm-migration.json', '.standterm-data-schema.json',
                '.standterm-retention.json', 'setup.log'}
    for parent, directories, names in os.walk(root, topdown=False, followlinks=False):
        for name in names:
            target = Path(parent) / name
            runtime.check_path(target)
            relative = target.relative_to(root)
            if (len(relative.parts) == 1 and name in controls) or '__pycache__' in relative.parts:
                continue
            # Keep only unique data/modified files, never another full runtime.
            destination = archive / relative
            runtime.check_path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                raise runtime.UnsafeRuntime()
            os.rename(target, destination)
        for name in directories:
            runtime.check_path(Path(parent) / name)
    shutil.rmtree(root)


def prune_obsolete(current):
    """Retire idle predecessors only after verified migration and consent."""
    base = runtime.runtime_base()
    if sys.platform == 'darwin' or runtime.managed_identity(current) != current.name or current.parent != base / 'runtimes':
        return []
    runtime.check_path(current)
    if runtime.read_marker(current / '.desktop-ready.json') != {'id': current.name}:
        return []
    policy = current / '.standterm-retention.json'
    runtime.check_path(policy)
    if not policy.exists() or runtime.read_marker(policy) != {'version': 1, 'cleanArtifacts': True}:
        return []
    receipt = runtime.read_marker(current / '.standterm-migration.json')
    if receipt.get('destination') != current.name or receipt.get('complete') is not True:
        return []
    spec = importlib.util.spec_from_file_location('standterm_upgrade', Path(__file__).with_name('runtime_upgrade.py'))
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    upgrade.read_data(current)
    records = base / 'retirement-records'
    retired = base / 'retired-runtimes'
    retained = base / 'retained-data'
    for folder in [records, retired, retained]:
        runtime.check_path(folder)
        folder.mkdir(mode=0o700, exist_ok=True)
    results = []
    # A durable external record survives interrupted deletion of any child.
    # Recovery never touches a recreated original runtime path.
    for record in sorted(records.glob('*.json')):
        if not re.fullmatch(r'[a-f0-9]{64}-[a-f0-9]{32}\.json', record.name):
            continue
        item = {'id': record.name[:64], 'status': 'retained'}
        results.append(item)
        try:
            runtime.check_path(record)
            if record.stat().st_size > 1024 * 1024:
                continue
            saved = json.loads(record.read_text(encoding='utf-8'))
            if saved.get('version') != 1 or saved.get('id') != item['id'] or saved.get('retired') != record.stem:
                continue
            files = validate_files(saved.get('files'), item['id'])
            root = retired / record.stem
            runtime.check_path(root)
            if root.exists():
                with runtime.lease(root, create=False):
                    dispose(root, item['id'], files, retained / record.stem)
            record.unlink()
            item['status'] = 'removed'
        except (OSError, ValueError, runtime.UnsafeRuntime, runtime.RuntimeBusy):
            item['reason'] = 'unavailable'
    for root in sorted((base / 'runtimes').iterdir()):
        if root == current or not re.fullmatch('[a-f0-9]{64}', root.name):
            continue
        item = {'id': root.name, 'status': 'retained'}
        results.append(item)
        try:
            with runtime.lease(root, create=False):
                if runtime.read_marker(root / '.standterm-bundle.json') != {'id': root.name} or not owned_venv(root):
                    continue
                venv = runtime.venv_path(root)
                runtime.check_path(venv)
                if Path(sys.prefix).resolve().is_relative_to(venv.resolve()):
                    continue
                if receipt.get('source') == root.name and upgrade.fingerprints(upgrade.read_data(root)) != receipt.get('files'):
                    item['reason'] = 'source_data_changed'
                    continue
                files = core_files(root)
                name = root.name + '-' + uuid.uuid4().hex
                record = records / (name + '.json')
                upgrade.atomic_json(record, {'version': 1, 'id': root.name, 'retired': name, 'files': files})
                destination = retired / name
                try:
                    os.rename(root, destination)
                except OSError:
                    record.unlink()
                    if sys.platform != 'win32' or not (root / '.setup.lock').exists():
                        raise
                    dispose(root, root.name, files, retained / name, legacy=True)
                    item['status'] = 'pruned_legacy'
                    continue
                dispose(destination, root.name, files, retained / name)
                record.unlink()
                item['status'] = 'removed'
        except runtime.RuntimeBusy:
            item['reason'] = 'in_use'
        except (OSError, ValueError, runtime.UnsafeRuntime, upgrade.MigrationError):
            item['reason'] = 'unverified_or_unavailable'
    return results


def inventory(base):
    runtime.check_path(base)
    roots = base / 'runtimes'
    runtime.check_path(roots)
    results = []
    if not roots.exists():
        return results
    # No recursive discovery, arbitrary paths, external interpreters or distro scans.
    candidates = [root for root in roots.iterdir() if re.fullmatch('[a-f0-9]{64}', root.name)]
    # Validate cardinality AND serialized response budget before any move.
    if len(candidates) > 32:
        raise runtime.UnsafeRuntime()
    for root in sorted(candidates):
        item = {'id': root.name, 'source': str(runtime.venv_path(root)),
                'status': 'retained', 'reason': 'unverified'}
        results.append(item)
        try:
            runtime.check_path(root)
            if (runtime.read_marker(root / '.standterm-bundle.json') != {'id': root.name}
                    or not owned_venv(root)):
                continue
            with runtime.lease(root, create=False):
                # Revalidate after acquiring the same lease used by setup/backend.
                if not owned_venv(root):
                    continue
                venv = runtime.venv_path(root)
                runtime.check_path(venv)
                if not venv.is_dir():
                    item['reason'] = 'absent'
                    continue
                if Path(sys.prefix).resolve().is_relative_to(venv.resolve()):
                    item['reason'] = 'cleanup_interpreter'
                    continue
                item.update(status='candidate', reason='idle')
        except runtime.RuntimeBusy:
            item['reason'] = 'in_use'
        except (OSError, ValueError, runtime.UnsafeRuntime):
            item['reason'] = 'unverified_or_unavailable'
    # Include a worst-case recovery result in the preflight budget. UTF-8/JSON
    # escaping and long home paths cannot cause a post-mutation oversized frame.
    worst_case = [{**item, 'recovery': str(base / 'venv-recovery' / ('0' * 36))} for item in results]
    if len(json.dumps(worst_case).encode('utf-8')) > 24000:
        raise runtime.UnsafeRuntime()
    return results


def detach(base, identities):
    if (not isinstance(identities, list) or len(identities) > 32
            or any(not isinstance(item, str) or not re.fullmatch('[a-f0-9]{64}', item) for item in identities)
            or len(set(identities)) != len(identities)):
        raise runtime.UnsafeRuntime()
    results = inventory(base)
    for item in results:
        root = base / 'runtimes' / item['id']
        if item['status'] != 'candidate':
            continue
        item.update(status='retained', reason='not_selected')
        if item['id'] not in identities:
            continue
        try:
            with runtime.lease(root, create=False):
                if (runtime.read_marker(root / '.standterm-bundle.json') != {'id': root.name}
                        or not owned_venv(root)):
                    item['reason'] = 'unverified'
                    continue
                venv = runtime.venv_path(root)
                runtime.check_path(venv)
                if not venv.is_dir() or Path(sys.prefix).resolve().is_relative_to(venv.resolve()):
                    item['reason'] = 'unavailable_or_cleanup_interpreter'
                    continue
                recovery = base / 'venv-recovery'
                runtime.check_path(recovery)
                recovery.mkdir(mode=0o700, exist_ok=True)
                destination = recovery / str(uuid.uuid4())
                destination.mkdir(mode=0o700)
                (destination / 'restore.json').write_text(json.dumps({
                    'version': 1, 'runtime_id': root.name,
                    'source': str(venv), 'venv': venv.name,
                }), encoding='utf-8')
                # Same-filesystem rename is recoverable and never follows venv children.
                # EXDEV, open Windows files and permission failures retain the source.
                os.rename(venv, destination / venv.name)
                item.update(status='detached', reason='recoverable', recovery=str(destination))
        except runtime.RuntimeBusy:
            item['reason'] = 'in_use'
        except (OSError, ValueError, runtime.UnsafeRuntime):
            item['reason'] = 'unverified_or_unavailable'
    return results


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--inventory']:
            print(json.dumps({'type': 'cleanup_inventory', 'results': inventory(runtime.runtime_base())}), flush=True)
        elif len(sys.argv) == 3 and sys.argv[1] == '--detach-idle-venvs':
            print(json.dumps({'type': 'cleanup_summary',
                              'results': detach(runtime.runtime_base(), json.loads(sys.argv[2]))}), flush=True)
        else:
            raise ValueError()
    except Exception:
        print(json.dumps({'type': 'error', 'code': 'cleanup_failed'}), flush=True)
        sys.exit(1)
