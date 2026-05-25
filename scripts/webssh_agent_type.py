#!/usr/bin/env python3
"""Compatibility wrapper for the renamed agent typer."""
import sys

from agent_type import main


if __name__ == '__main__':
    print(
        'warning: scripts/webssh_agent_type.py is deprecated; use scripts/agent_type.py',
        file=sys.stderr,
    )
    raise SystemExit(main())
