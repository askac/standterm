#!/usr/bin/env python3
import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser(description='WebSSH external agent CLI')
    parser.add_argument('--handoff', help='Read url, token, and terminal from a WebSSH external agent handoff JSON file')
    parser.add_argument('--url', help='WebSSH base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with WEBSSH_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS WebSSH servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')
    subparsers = parser.add_subparsers(dest='command', required=True)

    subparsers.add_parser('hello')
    subparsers.add_parser('attach')
    subparsers.add_parser('state')
    screen_parser = subparsers.add_parser('screen')
    screen_group = screen_parser.add_mutually_exclusive_group()
    screen_group.add_argument('--tail-lines', type=int, help='Only return the last N viewport lines')
    screen_group.add_argument('--region', help='Only return zero-based line range TOP:BOTTOM, with BOTTOM exclusive')

    render_parser = subparsers.add_parser('render')
    render_parser.add_argument('--wait-ms', type=int, default=3000, help='Maximum browser render wait time')

    tail_parser = subparsers.add_parser('tail')
    tail_parser.add_argument('--since', type=int, default=0, help='Only return events after this output_seq')
    tail_parser.add_argument('--limit', type=int, default=50, help='Maximum events to return')
    tail_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from returned terminal data')

    send_parser = subparsers.add_parser('send')
    input_group = send_parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--text', help='Text to send')
    input_group.add_argument('--stdin', action='store_true', help='Read text from stdin')
    send_parser.add_argument('--capture', action='store_true', help='Wait for terminal output after sending')
    send_parser.add_argument('--wait-ms', type=int, help='Maximum capture wait time')
    send_parser.add_argument('--settle-ms', type=int, help='Output idle time before capture returns')
    send_parser.add_argument('--limit', type=int, help='Maximum captured tail events to return')
    send_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from captured terminal data')

    send_wait_parser = subparsers.add_parser('send-wait')
    send_wait_group = send_wait_parser.add_mutually_exclusive_group(required=True)
    send_wait_group.add_argument('--text', help='Text to send')
    send_wait_group.add_argument('--stdin', action='store_true', help='Read text from stdin')
    send_wait_parser.add_argument('--wait-ms', type=int, help='Maximum capture wait time')
    send_wait_parser.add_argument('--settle-ms', type=int, help='Output idle time before capture returns')
    send_wait_parser.add_argument('--limit', type=int, help='Maximum captured tail events to return')
    send_wait_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from captured terminal data')

    subparsers.add_parser('revoke')
    args = parser.parse_args()
    apply_handoff(args)
    if not args.url:
        parser.error('--url is required unless --handoff provides url')
    return args


def load_handoff(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise SystemExit(f'failed to read handoff: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f'failed to parse handoff JSON: {exc}') from exc
    if not isinstance(payload, dict):
        raise SystemExit('handoff JSON must be an object')
    return payload


def apply_handoff(args):
    if not args.handoff:
        return
    payload = load_handoff(args.handoff)
    if not args.url:
        args.url = payload.get('url')
    if not args.token:
        args.token = payload.get('token')
    if args.terminal == 'main' and isinstance(payload.get('terminal_id'), str):
        args.terminal = payload['terminal_id']
    transport = payload.get('transport')
    if not args.ca_file and isinstance(transport, dict):
        args.ca_file = transport.get('tls_ca_cert_path')
    if not args.ca_file:
        args.ca_file = payload.get('tls_ca_cert_path')


def command_payload(args):
    payload = {
        'op': args.command,
        'terminal_id': args.terminal,
    }
    if args.token:
        payload['token'] = args.token
    if args.command == 'tail':
        payload['since_output_seq'] = args.since
        payload['limit'] = args.limit
        if getattr(args, 'strip_ansi', False):
            payload['strip_ansi'] = True
    elif args.command == 'screen':
        if getattr(args, 'tail_lines', None) is not None:
            payload['tail_lines'] = args.tail_lines
        elif getattr(args, 'region', None):
            try:
                top_text, bottom_text = args.region.split(':', 1)
                payload['region'] = {
                    'top': int(top_text),
                    'bottom': int(bottom_text),
                }
            except ValueError as exc:
                raise SystemExit('screen --region must use TOP:BOTTOM') from exc
    elif args.command == 'render':
        payload['wait_ms'] = args.wait_ms
    elif args.command in {'send', 'send-wait'}:
        payload['data'] = sys.stdin.read() if args.stdin else args.text
        if args.command == 'send-wait':
            payload['capture'] = True
        elif getattr(args, 'capture', False):
            payload['capture'] = True
        if getattr(args, 'wait_ms', None) is not None:
            payload['wait_ms'] = args.wait_ms
        if getattr(args, 'settle_ms', None) is not None:
            payload['settle_ms'] = args.settle_ms
        if getattr(args, 'limit', None) is not None:
            payload['limit'] = args.limit
        if getattr(args, 'strip_ansi', False):
            payload['strip_ansi'] = True
    return payload


def build_ssl_context(ca_file=None, insecure=False):
    if insecure:
        return ssl._create_unverified_context()
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return None


def post_json(base_url, payload, dev_mode=False, ca_file=None, insecure=False):
    path = '/agent/external/dev-command' if dev_mode else '/agent/external/command'
    url = base_url.rstrip('/') + path
    data = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    context = build_ssl_context(ca_file=ca_file, insecure=insecure)
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {'status': 'failed', 'error_code': f'http_{exc.code}', 'message': body}
        return exc.code, payload


def print_result(result):
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def main():
    args = parse_args()
    _status, result = post_json(
        args.url,
        command_payload(args),
        dev_mode=not bool(args.token),
        ca_file=args.ca_file,
        insecure=args.insecure,
    )
    print_result(result)
    return 0 if result.get('status') != 'failed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
