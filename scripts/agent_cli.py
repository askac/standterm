#!/usr/bin/env python3
import argparse
import base64
import copy
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from agent_input import KEY_INPUTS


def parse_args():
    parser = argparse.ArgumentParser(description='StandTerm external agent CLI')
    parser.add_argument('--handoff', help='Read url, token, and terminal from a StandTerm external agent handoff JSON file')
    parser.add_argument('--agentinfo', help='Read tokenless StandTerm agentinfo JSON from a local path or URL')
    parser.add_argument('--url', help='StandTerm base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with STANDTERM_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS StandTerm servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')
    subparsers = parser.add_subparsers(dest='command', required=True)

    discover_parser = subparsers.add_parser('discover')
    discover_parser.add_argument('--refresh-url', action='store_true', help='Fetch /agentinfo from --url instead of only printing local --agentinfo')

    hello_parser = subparsers.add_parser('hello')
    hello_parser.add_argument('--discover', action='store_true', help='Include tokenless agentinfo in the hello output when available')
    subparsers.add_parser('attach')
    subparsers.add_parser('state')
    screen_parser = subparsers.add_parser('screen')
    screen_group = screen_parser.add_mutually_exclusive_group()
    screen_group.add_argument('--tail-lines', type=int, help='Only return the last N viewport lines')
    screen_group.add_argument('--region', help='Only return zero-based line range TOP:BOTTOM, with BOTTOM exclusive')
    screen_parser.add_argument('--wait-ms', type=int, help='Wait up to this long for a quiet screen')
    screen_parser.add_argument('--quiet-ms', type=int, help='Required terminal quiet time before returning screen')

    render_parser = subparsers.add_parser('render')
    render_parser.add_argument('--mode', choices=('auto', 'visible-xterm-png', 'mirror-screen'), default='auto', help='Render mode')
    render_parser.add_argument('--wait-ms', type=int, default=3000, help='Maximum browser render wait time')
    render_parser.add_argument('--save', help='Save returned PNG image bytes to this path and omit image_base64 from stdout')

    tail_parser = subparsers.add_parser('tail')
    tail_parser.add_argument('--since', type=int, default=0, help='Only return events after this output_seq')
    tail_parser.add_argument('--limit', type=int, default=50, help='Maximum events to return')
    tail_parser.add_argument('--wait-ms', type=int, help='Server-side long-poll wait for output')
    tail_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from returned terminal data')

    wait_output_parser = subparsers.add_parser('wait-output')
    wait_output_parser.add_argument('--since', type=int, default=0, help='Only return events after this output_seq')
    wait_output_parser.add_argument('--limit', type=int, default=50, help='Maximum events to return')
    wait_output_parser.add_argument('--wait-ms', type=int, required=True, help='Server-side long-poll wait for output')
    wait_output_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from returned terminal data')

    wait_quiet_parser = subparsers.add_parser('wait-quiet')
    wait_quiet_group = wait_quiet_parser.add_mutually_exclusive_group()
    wait_quiet_group.add_argument('--tail-lines', type=int, help='Only return the last N viewport lines')
    wait_quiet_group.add_argument('--region', help='Only return zero-based line range TOP:BOTTOM, with BOTTOM exclusive')
    wait_quiet_parser.add_argument('--wait-ms', type=int, required=True, help='Wait up to this long for a quiet screen')
    wait_quiet_parser.add_argument('--quiet-ms', type=int, required=True, help='Required terminal quiet time before returning screen')

    wait_parser = subparsers.add_parser('wait')
    wait_parser.add_argument('--for', dest='condition', choices=('output', 'quiet'), required=True, help='Structured wait condition')
    wait_parser.add_argument('--since', type=int, default=0, help='For output waits, only consider events after this output_seq')
    wait_parser.add_argument('--limit', type=int, default=50, help='Maximum events to include when --include-events is used')
    wait_parser.add_argument('--wait-ms', type=int, required=True, help='Maximum wait time')
    wait_parser.add_argument('--quiet-ms', type=int, help='For quiet waits, required terminal quiet time')
    wait_parser.add_argument('--include-events', action='store_true', help='Include display tail events for output waits')
    wait_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from included events')

    send_parser = subparsers.add_parser('send')
    input_group = send_parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--text', help='Text to send')
    input_group.add_argument('--stdin', action='store_true', help='Read text from stdin')
    input_group.add_argument('--key', action='append', choices=sorted(KEY_INPUTS), help='Named key to send; repeat for multiple keys')
    send_parser.add_argument('--capture', action='store_true', help='Wait for terminal output after sending')
    send_parser.add_argument('--submit', action='store_true', help='Send a discrete Enter keypress after the text/stdin payload')
    send_parser.add_argument('--wait-ms', type=int, help='Maximum capture wait time')
    send_parser.add_argument('--settle-ms', type=int, help='Output idle time before capture returns')
    send_parser.add_argument('--limit', type=int, help='Maximum captured tail events to return')
    send_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from captured terminal data')

    send_wait_parser = subparsers.add_parser('send-wait')
    send_wait_group = send_wait_parser.add_mutually_exclusive_group(required=True)
    send_wait_group.add_argument('--text', help='Text to send')
    send_wait_group.add_argument('--stdin', action='store_true', help='Read text from stdin')
    send_wait_group.add_argument('--key', action='append', choices=sorted(KEY_INPUTS), help='Named key to send; repeat for multiple keys')
    send_wait_parser.add_argument('--submit', action='store_true', help='Send a discrete Enter keypress after the text/stdin payload')
    send_wait_parser.add_argument('--wait-ms', type=int, help='Maximum capture wait time')
    send_wait_parser.add_argument('--settle-ms', type=int, help='Output idle time before capture returns')
    send_wait_parser.add_argument('--limit', type=int, help='Maximum captured tail events to return')
    send_wait_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from captured terminal data')

    key_parser = subparsers.add_parser('key')
    key_parser.add_argument('--key', action='append', required=True, choices=sorted(KEY_INPUTS), help='Named key to send; repeat for multiple keys')
    key_parser.add_argument('--capture', action='store_true', help='Wait for terminal output after sending')
    key_parser.add_argument('--wait-ms', type=int, help='Maximum capture wait time')
    key_parser.add_argument('--settle-ms', type=int, help='Output idle time before capture returns')
    key_parser.add_argument('--limit', type=int, help='Maximum captured tail events to return')
    key_parser.add_argument('--strip-ansi', action='store_true', help='Strip ANSI/control sequences from captured terminal data')

    subparsers.add_parser('revoke')
    args = parser.parse_args()
    apply_agentinfo(args)
    if args.command == 'discover':
        if not args.agentinfo_payload and not args.url:
            parser.error('discover requires --agentinfo or --url')
        return args
    apply_handoff(args)
    if not args.url:
        parser.error('--url is required unless --handoff or --agentinfo provides url')
    return args


def load_json_file(path, label):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise SystemExit(f'failed to read {label}: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f'failed to parse {label} JSON: {exc}') from exc
    if not isinstance(payload, dict):
        raise SystemExit(f'{label} JSON must be an object')
    return payload


def load_handoff(path):
    return load_json_file(path, 'handoff')


def is_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def get_json(url, ca_file=None, insecure=False):
    context = build_ssl_context(ca_file=ca_file, insecure=insecure)
    try:
        with urllib.request.urlopen(url, timeout=30, context=context) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {'status': 'failed', 'error_code': f'http_{exc.code}', 'message': body}
        return exc.code, payload


def load_agentinfo(source, ca_file=None, insecure=False):
    if is_url(source):
        url = source.rstrip('/')
        if not url.endswith('/agentinfo'):
            url += '/agentinfo'
        _status, payload = get_json(url, ca_file=ca_file, insecure=insecure)
        if not isinstance(payload, dict):
            raise SystemExit('agentinfo response must be a JSON object')
        return payload
    return load_json_file(source, 'agentinfo')


def apply_agentinfo(args):
    args.agentinfo_payload = None
    if not getattr(args, 'agentinfo', None):
        return
    payload = load_agentinfo(args.agentinfo, ca_file=args.ca_file, insecure=args.insecure)
    args.agentinfo_payload = payload
    transport = payload.get('transport')
    if not args.url:
        args.url = payload.get('base_url')
    if not args.url and isinstance(transport, dict):
        args.url = transport.get('base_url')
    if not args.ca_file and isinstance(transport, dict):
        args.ca_file = transport.get('tls_ca_cert_path')
    if not args.ca_file:
        args.ca_file = payload.get('tls_ca_cert_path')
    if not args.handoff and isinstance(payload.get('handoff_path'), str) and os.path.isfile(payload['handoff_path']):
        args.handoff = payload['handoff_path']


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
    command = args.command
    op = {
        'key': 'send',
        'wait-output': 'tail',
        'wait-quiet': 'screen',
    }.get(command, command)
    payload = {
        'op': op,
        'terminal_id': args.terminal,
    }
    if args.token:
        payload['token'] = args.token
    if command in {'tail', 'wait-output'}:
        payload['since_output_seq'] = args.since
        payload['limit'] = args.limit
        if getattr(args, 'wait_ms', None) is not None:
            payload['wait_ms'] = args.wait_ms
        if getattr(args, 'strip_ansi', False):
            payload['strip_ansi'] = True
    elif command in {'screen', 'wait-quiet'}:
        if getattr(args, 'wait_ms', None) is not None:
            payload['wait_ms'] = args.wait_ms
        if getattr(args, 'quiet_ms', None) is not None:
            payload['quiet_ms'] = args.quiet_ms
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
                raise SystemExit(f'{command} --region must use TOP:BOTTOM') from exc
    elif command == 'wait':
        payload['condition'] = args.condition
        payload['wait_ms'] = args.wait_ms
        if args.condition == 'output':
            payload['since_output_seq'] = args.since
            payload['limit'] = args.limit
            if args.include_events:
                payload['include_events'] = True
            if args.strip_ansi:
                payload['strip_ansi'] = True
        elif args.condition == 'quiet':
            if args.quiet_ms is None:
                raise SystemExit('wait --for quiet requires --quiet-ms')
            payload['quiet_ms'] = args.quiet_ms
    elif command == 'render':
        payload['render_mode'] = args.mode.replace('-', '_')
        payload['wait_ms'] = args.wait_ms
    elif command in {'send', 'send-wait', 'key'}:
        keys = getattr(args, 'key', None)
        if keys:
            payload['kind'] = 'keys'
            payload['keys'] = list(keys)
        else:
            payload['kind'] = 'text'
            payload['text'] = send_data(args)
        if command == 'send-wait':
            payload['capture'] = True
        elif getattr(args, 'capture', False):
            payload['capture'] = True
        if getattr(args, 'submit', False):
            if getattr(args, 'key', None):
                raise SystemExit('--submit can only be used with --text or --stdin')
            payload['submit_after'] = True
        if getattr(args, 'wait_ms', None) is not None:
            payload['wait_ms'] = args.wait_ms
        if getattr(args, 'settle_ms', None) is not None:
            payload['settle_ms'] = args.settle_ms
        if getattr(args, 'limit', None) is not None:
            payload['limit'] = args.limit
        if getattr(args, 'strip_ansi', False):
            payload['strip_ansi'] = True
    return payload


def send_data(args):
    return sys.stdin.read() if args.stdin else args.text


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


def discover_result(args):
    if args.agentinfo_payload and not getattr(args, 'refresh_url', False):
        return args.agentinfo_payload
    if not args.url:
        raise SystemExit('discover requires --agentinfo or --url')
    _status, result = get_json(args.url.rstrip('/') + '/agentinfo', ca_file=args.ca_file, insecure=args.insecure)
    return result


def print_result(result):
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def save_render_image(result, path):
    render = result.get('render') if isinstance(result, dict) else None
    image_base64 = render.get('image_base64') if isinstance(render, dict) else None
    if not isinstance(image_base64, str) or not image_base64:
        raise SystemExit('render response does not include render.image_base64')
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise SystemExit(f'failed to decode render.image_base64: {exc}') from exc
    try:
        with open(path, 'wb') as handle:
            handle.write(image_bytes)
    except OSError as exc:
        raise SystemExit(f'failed to save render image: {exc}') from exc

    output = copy.deepcopy(result)
    output_render = output.get('render')
    if isinstance(output_render, dict):
        output_render.pop('image_base64', None)
        output_render['saved_path'] = path
    return output


def main():
    args = parse_args()
    if args.command == 'render' and args.save and args.mode != 'visible-xterm-png':
        raise SystemExit('render --save requires --mode visible-xterm-png')
    if args.command == 'discover':
        result = discover_result(args)
        print_result(result)
        return 0 if result.get('status') != 'failed' else 1
    _status, result = post_json(
        args.url,
        command_payload(args),
        dev_mode=not bool(args.token),
        ca_file=args.ca_file,
        insecure=args.insecure,
    )
    if args.command == 'hello' and getattr(args, 'discover', False) and args.agentinfo_payload:
        result = copy.deepcopy(result)
        result['agentinfo'] = args.agentinfo_payload
    if result.get('status') != 'failed' and args.command == 'render' and args.save:
        result = save_render_image(result, args.save)
    print_result(result)
    return 0 if result.get('status') != 'failed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
