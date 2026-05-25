#!/usr/bin/env python3
"""Compatibility wrapper for the renamed agent REPL."""
import sys

from agent_repl import main


if __name__ == '__main__':
    print(
        'warning: scripts/webssh_agent_repl.py is deprecated; use scripts/agent_repl.py',
        file=sys.stderr,
    )
    raise SystemExit(main())
