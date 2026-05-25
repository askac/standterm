#!/usr/bin/env python3
"""Compatibility wrapper for the renamed agent CLI."""
import sys

from agent_cli import main


if __name__ == '__main__':
    print(
        'warning: scripts/webssh_agent_cli.py is deprecated; use scripts/agent_cli.py',
        file=sys.stderr,
    )
    raise SystemExit(main())
