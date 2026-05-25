#!/usr/bin/env python3
import argparse
import json
import sys

import agent_cli as cli


def parse_args():
    parser = argparse.ArgumentParser(description='Persistent JSONL client for StandTerm external agent commands')
    parser.add_argument('--handoff', help='Read url, token, and terminal from a StandTerm external agent handoff JSON file')
    parser.add_argument('--url', help='StandTerm base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with STANDTERM_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Default terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS StandTerm servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')
    args = parser.parse_args()
    cli.apply_handoff(args)
    if not args.url:
        parser.error('--url is required unless --handoff provides url')
    return args


def jsonl_error(error_code, message=None, request_id=None):
    payload = {
        'ok': False,
        'error_code': error_code,
    }
    if request_id is not None:
        payload['id'] = request_id
    if message:
        payload['message'] = message
    return payload


def build_backend_payload(command, token=None, terminal_id='main'):
    if not isinstance(command, dict):
        return None, jsonl_error('invalid_command', 'command must be a JSON object')
    op = command.get('op')
    if not isinstance(op, str) or not op.strip():
        return None, jsonl_error('invalid_command', 'command.op is required', request_id=command.get('id'))

    payload = {
        key: value for key, value in command.items()
        if key != 'id'
    }
    payload.setdefault('terminal_id', terminal_id)
    if token and 'token' not in payload:
        payload['token'] = token
    return payload, None


def handle_command(command, base_url, token=None, terminal_id='main',
                   ca_file=None, insecure=False, post_json=cli.post_json):
    request_id = command.get('id') if isinstance(command, dict) else None
    payload, error = build_backend_payload(command, token=token, terminal_id=terminal_id)
    if error:
        return error
    try:
        status, result = post_json(
            base_url,
            payload,
            dev_mode=not bool(token),
            ca_file=ca_file,
            insecure=insecure,
        )
    except Exception as exc:
        return jsonl_error('transport_error', str(exc), request_id=request_id)

    response = {
        'ok': not (isinstance(result, dict) and result.get('status') == 'failed'),
        'http_status': status,
        'result': result,
    }
    if request_id is not None:
        response['id'] = request_id
    return response


def handle_line(line, base_url, token=None, terminal_id='main',
                ca_file=None, insecure=False, post_json=cli.post_json):
    try:
        command = json.loads(line)
    except json.JSONDecodeError as exc:
        return jsonl_error('invalid_json', str(exc))
    return handle_command(
        command,
        base_url,
        token=token,
        terminal_id=terminal_id,
        ca_file=ca_file,
        insecure=insecure,
        post_json=post_json,
    )


def run_jsonl(input_stream, output_stream, base_url, token=None, terminal_id='main',
              ca_file=None, insecure=False, post_json=cli.post_json):
    for line in input_stream:
        if not line.strip():
            continue
        response = handle_line(
            line,
            base_url,
            token=token,
            terminal_id=terminal_id,
            ca_file=ca_file,
            insecure=insecure,
            post_json=post_json,
        )
        output_stream.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + '\n')
        output_stream.flush()


def main():
    args = parse_args()
    run_jsonl(
        sys.stdin,
        sys.stdout,
        args.url,
        token=args.token,
        terminal_id=args.terminal,
        ca_file=args.ca_file,
        insecure=args.insecure,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
