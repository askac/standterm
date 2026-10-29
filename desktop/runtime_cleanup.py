"""Retain managed runtime payloads and recoverably detach venvs at uninstall."""

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


def prepared_at(root):
    ready = root / '.desktop-ready.json'
    runtime.check_path(ready)
    runtime.check_path(runtime.venv_path(root))
    if not ready.exists():
        return 0
    return (ready.stat().st_mtime_ns if runtime.read_marker(ready) == {'id': root.name}
            and runtime.venv_path(root).is_dir() else 0)


def prune_obsolete(current):
    """Reclaim reproducible files after a bundled Windows/WSL backend starts."""
    base = runtime.runtime_base()
    roots = base / 'runtimes'
    if sys.platform == 'darwin' or current.parent != roots or not re.fullmatch('[a-f0-9]{64}', current.name):
        return []
    runtime.check_path(current)
    if runtime.read_marker(current / '.desktop-ready.json') != {'id': current.name}:
        return []
    candidates = []
    for root in roots.iterdir():
        if root == current or not re.fullmatch('[a-f0-9]{64}', root.name):
            continue
        try:
            runtime.check_path(root)
            if (runtime.read_marker(root / '.standterm-bundle.json') != {'id': root.name}
                    or runtime.read_marker(root / '.standterm-venv.json') != {'id': root.name, 'lease': 1}):
                continue
            candidates.append((prepared_at(root), root))
        except (OSError, ValueError, runtime.UnsafeRuntime):
            continue
    # Preserve one prepared predecessor as well as every runtime still in use.
    previous = max(candidates, default=(0, None), key=lambda item: (item[0], str(item[1])))
    keep = previous[1] if previous[0] else None
    results = []
    for prepared, root in candidates:
        if root == keep:
            continue
        item = {'id': root.name, 'status': 'retained'}
        results.append(item)
        try:
            with runtime.lease(root, create=False):
                if (runtime.read_marker(root / '.standterm-bundle.json') != {'id': root.name}
                        or runtime.read_marker(root / '.standterm-venv.json') != {'id': root.name, 'lease': 1}):
                    continue
                if prepared_at(root) != prepared:
                    continue
                venv = runtime.venv_path(root)
                runtime.check_path(venv)
                if Path(sys.prefix).resolve().is_relative_to(venv.resolve()):
                    continue
                paths = obsolete_core_files(root)
                # A failed or interrupted deletion must never be chosen as the
                # prepared predecessor on the next launch.
                (root / '.desktop-ready.json').unlink(missing_ok=True)
                # Keep the root and lease inode: an older launcher may already
                # be waiting to acquire this exact lock before rebuilding it.
                if venv.is_dir():
                    shutil.rmtree(venv)
                for target, digest in paths:
                    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                        target.unlink()
                item['status'] = 'pruned'
        except runtime.RuntimeBusy:
            item['reason'] = 'in_use'
        except (OSError, ValueError, runtime.UnsafeRuntime):
            item['reason'] = 'unverified_or_unavailable'
    return results


def obsolete_core_files(root):
    # Older installations have no manifest. Retain their Core and user files;
    # their owned venv accounts for most of the accumulated disk usage.
    manifest = root / '.standterm-files.json'
    runtime.check_path(manifest)
    if not manifest.exists():
        return []
    if manifest.stat().st_size > 1024 * 1024:
        raise runtime.UnsafeRuntime()
    data = json.loads(manifest.read_text(encoding='utf-8'))
    files = data.get('files') if isinstance(data, dict) else None
    if (not isinstance(files, dict) or len(files) > 4096 or data.get('version') != 1 or data.get('id') != root.name
            or hashlib.sha256(json.dumps(files, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest() != root.name):
        raise runtime.UnsafeRuntime()
    paths = []
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or not relative.parts or '..' in relative.parts or '\\' in name or ':' in name
                or relative.parts[0].startswith('.') or relative.parts[0] in ['authorized', 'certs', 'tools']
                or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)):
            raise runtime.UnsafeRuntime()
        target = root / name
        runtime.check_path(target)
        paths.append((target, digest))
    # Unknown or modified files, credentials, certificates and logs stay put.
    # Empty asset directories and Python caches are harmless and retained too.
    return paths


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
                    or runtime.read_marker(root / '.standterm-venv.json') != {'id': root.name, 'lease': 1}):
                continue
            with runtime.lease(root, create=False):
                # Revalidate after acquiring the same lease used by setup/backend.
                if runtime.read_marker(root / '.standterm-venv.json') != {'id': root.name, 'lease': 1}:
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
                        or runtime.read_marker(root / '.standterm-venv.json') != {'id': root.name, 'lease': 1}):
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
