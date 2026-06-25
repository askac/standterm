#!/usr/bin/env python3
import argparse
import json
import sys

import agent_cli as cli


NEWLINE_BYTES = {
    'cr': '\r',
    'lf': '\n',
    'crlf': '\r\n',
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Send one shell-style command line to a StandTerm external-agent terminal.',
    )
    parser.add_argument('--handoff', help='Read url, token, and terminal from a StandTerm external agent handoff JSON file')
    parser.add_argument('--agentinfo', help='Read tokenless StandTerm agentinfo JSON from a local path or URL')
    parser.add_argument('--url', help='StandTerm base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with STANDTERM_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS StandTerm servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')
    parser.add_argument('--stdin', action='store_true', help='Read the command line from stdin instead of positional arguments')
    parser.add_argument('--newline', choices=sorted(NEWLINE_BYTES), default='cr', help='Line ending sent after the command')
    parser.add_argument('--wait-ms', type=int, default=3000, help='Maximum capture wait time')
    parser.add_argument('--settle-ms', type=int, default=300, help='Output idle time before capture returns')
    parser.add_argument('--limit', type=int, help='Maximum captured tail events to return')
    parser.add_argument('--raw-ansi', action='store_true', help='Do not strip ANSI/control sequences from captured terminal data')
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument('--json', action='store_true', help='Print a compact JSON response with status and stdout')
    output_group.add_argument('--full-json', action='store_true', help='Print the full external-agent JSON response')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='Command words to join with spaces and send to the terminal shell')
    args = parser.parse_args(argv)
    if args.command and args.command[0] == '--':
        args.command = args.command[1:]
    cli.apply_agentinfo(args)
    cli.apply_handoff(args)
    if not args.url:
        parser.error('--url is required unless --handoff or --agentinfo provides url')
    if args.stdin and args.command:
        parser.error('--stdin cannot be combined with positional command words')
    if not args.stdin and not args.command:
        parser.error('command is required unless --stdin is used')
    return args


def command_text(args, stdin_text=None):
    text = stdin_text if args.stdin else ' '.join(args.command)
    if text.endswith(('\r', '\n')):
        return text
    return text + NEWLINE_BYTES[args.newline]


def command_payload(args, stdin_text=None):
    payload = {
        'op': 'send-wait',
        'terminal_id': args.terminal,
        'kind': 'text',
        'text': command_text(args, stdin_text=stdin_text),
        'capture': True,
        'wait_ms': args.wait_ms,
        'settle_ms': args.settle_ms,
    }
    if args.token:
        payload['token'] = args.token
    if args.limit is not None:
        payload['limit'] = args.limit
    if not args.raw_ansi:
        payload['strip_ansi'] = True
    return payload


def capture_text(result):
    capture = result.get('capture') if isinstance(result, dict) else None
    events = capture.get('events') if isinstance(capture, dict) else None
    if not isinstance(events, list):
        return ''
    return ''.join(
        event.get('data')
        for event in events
        if isinstance(event, dict) and isinstance(event.get('data'), str)
    )


def compact_result(result):
    if not isinstance(result, dict):
        return {
            'status': 'failed',
            'error_code': 'invalid_response',
            'stdout': '',
        }
    capture = result.get('capture') if isinstance(result.get('capture'), dict) else {}
    payload = {
        'status': result.get('status'),
        'stdout': capture_text(result),
    }
    for key in (
        'error_code',
        'bytes_written',
        'input_kind',
        'terminal_id',
    ):
        if key in result:
            payload[key] = result[key]
    if capture:
        payload['capture'] = {
            key: capture.get(key)
            for key in (
                'status',
                'mode',
                'timed_out',
                'settled',
                'wait_ms',
                'settle_ms',
                'returned_event_count',
                'truncated',
            )
            if key in capture
        }
    return payload


def print_json(payload, stream):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def run(args, stdin_text=None, post_json=cli.post_json, stdout=None, stderr=None):
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    _status, result = post_json(
        args.url,
        command_payload(args, stdin_text=stdin_text),
        dev_mode=not bool(args.token),
        ca_file=args.ca_file,
        insecure=args.insecure,
    )
    if args.full_json:
        print_json(result, stdout)
    elif args.json:
        print_json(compact_result(result), stdout)
    elif isinstance(result, dict) and result.get('status') == 'failed':
        print_json(result, stderr)
    else:
        stdout.write(capture_text(result))
        stdout.flush()
    return 0 if isinstance(result, dict) and result.get('status') != 'failed' else 1


def main(argv=None):
    args = parse_args(argv)
    stdin_text = sys.stdin.read() if args.stdin else None
    return run(args, stdin_text=stdin_text)


if __name__ == '__main__':
    raise SystemExit(main())
