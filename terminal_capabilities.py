"""Bounded, read-only replies for the capabilities of the shipped terminal."""

import re

from core_version import CORE_VERSION


CAPABILITY_RESPONSE_WINDOW = 256
MAX_CAPABILITY_QUERIES_PER_OUTPUT = 32
MAX_CAPABILITY_NAMES = 16
MAX_CAPABILITY_REQUEST_LENGTH = 1024


def build_capability_response(kind, names=None):
    if kind == 'version':
        return f'\x1bP>|StandTerm({CORE_VERSION})\x1b\\'
    if kind != 'terminfo' or not isinstance(names, str):
        return None
    if not names or len(names) > MAX_CAPABILITY_REQUEST_LENGTH:
        return None
    encoded_names = names.split(';')
    if len(encoded_names) > MAX_CAPABILITY_NAMES:
        return None
    capabilities = {'TN': 'xterm-256color', 'name': 'xterm-256color',
                    'Co': '256', 'colors': '256', 'RGB': '8', 'Tc': None}
    replies = []
    for encoded_name in encoded_names:
        if not re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,64}', encoded_name):
            return None
        name = bytes.fromhex(encoded_name).decode('ascii', errors='replace')
        if name not in capabilities:
            replies.append('\x1bP0+r\x1b\\')
            break
        value = capabilities[name]
        encoded_value = '' if value is None else '=' + value.encode('ascii').hex()
        replies.append(f'\x1bP1+r{encoded_name}{encoded_value}\x1b\\')
    return ''.join(replies)
