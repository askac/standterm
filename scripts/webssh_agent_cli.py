#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.error
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser(description='WebSSH external agent CLI')
    parser.add_argument('--url', required=True, help='WebSSH base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', required=True, help='External agent attach token')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    subparsers = parser.add_subparsers(dest='command', required=True)

    subparsers.add_parser('attach')
    subparsers.add_parser('state')
    subparsers.add_parser('screen')

    tail_parser = subparsers.add_parser('tail')
    tail_parser.add_argument('--since', type=int, default=0, help='Only return events after this output_seq')
    tail_parser.add_argument('--limit', type=int, default=50, help='Maximum events to return')

    send_parser = subparsers.add_parser('send')
    input_group = send_parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--text', help='Text to send')
    input_group.add_argument('--stdin', action='store_true', help='Read text from stdin')

    subparsers.add_parser('revoke')
    return parser.parse_args()


def command_payload(args):
    payload = {
        'op': args.command,
        'token': args.token,
        'terminal_id': args.terminal,
    }
    if args.command == 'tail':
        payload['since_output_seq'] = args.since
        payload['limit'] = args.limit
    elif args.command == 'send':
        payload['data'] = sys.stdin.read() if args.stdin else args.text
    return payload


def post_json(base_url, payload):
    url = base_url.rstrip('/') + '/agent/external/command'
    data = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
    _status, result = post_json(args.url, command_payload(args))
    print_result(result)
    return 0 if result.get('status') != 'failed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
