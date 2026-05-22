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
    subparsers.add_parser('screen')

    render_parser = subparsers.add_parser('render')
    render_parser.add_argument('--wait-ms', type=int, default=3000, help='Maximum browser render wait time')

    tail_parser = subparsers.add_parser('tail')
    tail_parser.add_argument('--since', type=int, default=0, help='Only return events after this output_seq')
    tail_parser.add_argument('--limit', type=int, default=50, help='Maximum events to return')

    send_parser = subparsers.add_parser('send')
    input_group = send_parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--text', help='Text to send')
    input_group.add_argument('--stdin', action='store_true', help='Read text from stdin')

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
    elif args.command == 'render':
        payload['wait_ms'] = args.wait_ms
    elif args.command == 'send':
        payload['data'] = sys.stdin.read() if args.stdin else args.text
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
