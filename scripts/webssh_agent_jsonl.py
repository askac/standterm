#!/usr/bin/env python3
"""Compatibility wrapper for the renamed agent JSONL client."""
import sys

from agent_jsonl import main


if __name__ == '__main__':
    print(
        'warning: scripts/webssh_agent_jsonl.py is deprecated; use scripts/agent_jsonl.py',
        file=sys.stderr,
    )
    raise SystemExit(main())
