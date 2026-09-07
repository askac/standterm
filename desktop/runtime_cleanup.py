"""Recoverably detach idle, lease-aware venvs; never remove Core or user data."""

import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import uuid

spec = importlib.util.spec_from_file_location('standterm_runtime', Path(__file__).with_name('runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


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
