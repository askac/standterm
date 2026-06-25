import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

COMPILE_TARGETS = [
    'app.py',
    'scripts/agent_cli.py',
    'scripts/agent_jsonl.py',
    'scripts/agent_repl.py',
    'scripts/agent_shcmd.py',
    'scripts/agent_rsfile.py',
    'scripts/agent_type.py',
    'tests/agent_backend_smoke.py',
    'tests/external_agent_boundary_smoke.py',
    'tests/agent_repl_smoke.py',
    'tests/agent_rsfile_smoke.py',
]

HEADLESS_SMOKE_TESTS = [
    'tests/external_agent_boundary_smoke.py',
    'tests/agent_repl_smoke.py',
    'tests/agent_backend_smoke.py',
    'tests/agent_rsfile_smoke.py',
]


def run_step(label, args):
    print(f'==> {label}', flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Run StandTerm headless smoke checks with the active Python interpreter.',
    )
    parser.add_argument(
        '--skip-compile',
        action='store_true',
        help='Skip py_compile before running smoke tests.',
    )
    args = parser.parse_args(argv)

    if not args.skip_compile:
        run_step(
            'Compile Python entry points and smoke tests',
            [sys.executable, '-m', 'py_compile', *COMPILE_TARGETS],
        )

    for test_path in HEADLESS_SMOKE_TESTS:
        run_step(test_path, [sys.executable, test_path])

    print('==> Headless smoke checks passed', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
