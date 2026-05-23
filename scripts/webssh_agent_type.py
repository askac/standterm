#!/usr/bin/env python3
import argparse
import json
import random
import sys
import time

import webssh_agent_cli as cli


PUNCTUATION_PAUSE_CHARS = set('.!?;:,，。！？；：、')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            'Type text into a WebSSH external agent terminal at a controlled pace. '
            'WebSSH terminal input is one shared stream; do not send cursor-moving '
            'input from another viewer or helper while this command is running.'
        )
    )
    parser.add_argument('--handoff', help='Read url, token, and terminal from a WebSSH external agent handoff JSON file')
    parser.add_argument('--url', help='WebSSH base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with WEBSSH_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS WebSSH servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--text', help='Text to type')
    input_group.add_argument('--stdin', action='store_true', help='Read text to type from stdin')
    input_group.add_argument('--from-file', help='Read text to type from a UTF-8 file')

    pace_group = parser.add_mutually_exclusive_group()
    pace_group.add_argument('--cps', type=float, default=3.0, help='Characters per second, default: 3')
    pace_group.add_argument('--delay-ms', type=float, help='Delay between characters in milliseconds')
    parser.add_argument('--newline', choices=('cr', 'lf', 'crlf'), default='cr', help='Bytes sent for input newlines, default: cr')
    parser.add_argument('--jitter-ms', type=float, default=0, help='Random +/- jitter added to each delay in milliseconds')
    parser.add_argument('--punctuation-pause-ms', type=float, default=0, help='Extra delay after punctuation characters')
    parser.add_argument('--dry-run', action='store_true', help='Print the typing plan without sending input')
    parser.add_argument('--progress', action='store_true', help='Print one JSONL progress record per sent unit to stderr')
    args = parser.parse_args(argv)
    cli.apply_handoff(args)
    if not args.url and not args.dry_run:
        parser.error('--url is required unless --handoff provides url')
    return args


def read_input_text(args):
    if args.stdin:
        return sys.stdin.read()
    if args.from_file:
        try:
            with open(args.from_file, 'r', encoding='utf-8') as handle:
                return handle.read()
        except OSError as exc:
            raise SystemExit(f'failed to read input file: {exc}') from exc
    return args.text


def newline_bytes(newline_mode):
    if newline_mode == 'cr':
        return '\r'
    if newline_mode == 'crlf':
        return '\r\n'
    return '\n'


def iter_type_units(text, newline_mode='cr'):
    replacement = newline_bytes(newline_mode)
    for ch in text:
        if ch == '\n':
            yield replacement
        else:
            yield ch


def base_delay_seconds(args):
    if args.delay_ms is not None:
        return max(args.delay_ms, 0) / 1000.0
    cps = args.cps if args.cps and args.cps > 0 else 3.0
    return 1.0 / cps


def delay_for_unit(unit, args, random_uniform=random.uniform):
    delay = base_delay_seconds(args)
    jitter_ms = max(args.jitter_ms, 0)
    if jitter_ms:
        delay += random_uniform(-jitter_ms, jitter_ms) / 1000.0
    if unit in PUNCTUATION_PAUSE_CHARS and args.punctuation_pause_ms > 0:
        delay += args.punctuation_pause_ms / 1000.0
    return max(delay, 0)


def build_send_payload(unit, terminal_id='main', token=None):
    payload = {
        'op': 'send',
        'terminal_id': terminal_id,
        'data': unit,
    }
    if token:
        payload['token'] = token
    return payload


def summarize_units(units):
    return {
        'unit_count': len(units),
        'byte_count': sum(len(unit.encode('utf-8', errors='ignore')) for unit in units),
        'contains_newline_translation': any(unit in ('\r', '\n', '\r\n') for unit in units),
    }


def stderr_json(payload):
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
    sys.stderr.flush()


def type_units(args, units, post_json=cli.post_json, sleep=time.sleep, random_uniform=random.uniform):
    sent_units = 0
    sent_bytes = 0
    last_result = None
    for index, unit in enumerate(units):
        status, result = post_json(
            args.url,
            build_send_payload(unit, terminal_id=args.terminal, token=args.token),
            dev_mode=not bool(args.token),
            ca_file=args.ca_file,
            insecure=args.insecure,
        )
        last_result = result
        if isinstance(result, dict) and result.get('status') == 'failed':
            error_code = result.get('error_code') or f'http_{status}'
            return {
                'status': 'failed',
                'error_code': error_code,
                'stopped_at_unit': index,
                'sent_units': sent_units,
                'sent_bytes': sent_bytes,
                'result': result,
            }
        sent_units += 1
        sent_bytes += len(unit.encode('utf-8', errors='ignore'))
        if args.progress:
            stderr_json({
                'event': 'typed_unit',
                'unit_index': index,
                'sent_units': sent_units,
                'sent_bytes': sent_bytes,
            })
        if index < len(units) - 1:
            sleep(delay_for_unit(unit, args, random_uniform=random_uniform))

    return {
        'status': 'completed',
        'sent_units': sent_units,
        'sent_bytes': sent_bytes,
        'last_result': last_result,
    }


def run(args, post_json=cli.post_json, sleep=time.sleep, random_uniform=random.uniform):
    text = read_input_text(args)
    units = list(iter_type_units(text, newline_mode=args.newline))
    summary = summarize_units(units)
    if args.dry_run:
        output = {
            'status': 'dry_run',
            **summary,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    result = type_units(
        args,
        units,
        post_json=post_json,
        sleep=sleep,
        random_uniform=random_uniform,
    )
    output = {
        **summary,
        **result,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if output.get('status') == 'failed':
        return 1
    return 0


def main(argv=None):
    return run(parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
