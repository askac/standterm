#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time

import agent_cli as cli


TERMINAL_ACTION_STATUSES = {'completed', 'failed', 'rejected'}


def parse_args():
    parser = argparse.ArgumentParser(
        description='Copy one file between two StandTerm SSH or Local Shell terminals',
    )
    parser.add_argument('--handoff', help='Source terminal external-agent handoff JSON file')
    parser.add_argument('--agentinfo', help='StandTerm agentinfo JSON path or URL')
    parser.add_argument('--url', help='StandTerm base URL')
    parser.add_argument('--token', help='Source terminal external-agent token')
    parser.add_argument('--terminal', help='Source terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS StandTerm servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification for loopback testing')
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--destination-handoff', help='Destination terminal external-agent handoff JSON file')
    destination.add_argument('--destination-terminal', help='Destination terminal id resolved through --agentinfo')
    parser.add_argument(
        '--conflict-mode',
        choices=('fail', 'keep-both', 'replace'),
        default='fail',
        help='Destination conflict behavior; fail is the safe default',
    )
    parser.add_argument(
        '--wait-seconds',
        type=float,
        default=300,
        help='Maximum time to wait for browser approval and copy completion',
    )
    parser.add_argument(
        '--poll-ms',
        type=int,
        default=500,
        help='Action status polling interval while waiting for approval',
    )
    parser.add_argument('--no-wait', action='store_true', help='Return the pending action immediately')
    parser.add_argument('source_path', help='Source file path interpreted by the source terminal backend')
    parser.add_argument('destination_path', help='Destination file path interpreted by the destination terminal backend')
    args = parser.parse_args()

    if args.wait_seconds <= 0:
        parser.error('--wait-seconds must be positive')
    if args.poll_ms < 100 or args.poll_ms > 10000:
        parser.error('--poll-ms must be between 100 and 10000')
    cli.apply_agentinfo(args)
    cli.apply_handoff(args)
    if not args.url:
        parser.error('--url is required unless --handoff or --agentinfo provides it')
    if not args.token:
        parser.error('the source terminal requires a minted external-agent token')
    apply_destination(args, parser)
    return args


def apply_destination(args, parser):
    destination_handoff_path = args.destination_handoff
    if args.destination_terminal:
        if not args.agentinfo_payload:
            parser.error('--destination-terminal requires --agentinfo')
        destination_handoff_path = cli.resolve_terminal_handoff_path(
            args.agentinfo_payload,
            args.destination_terminal,
        )
        if not destination_handoff_path or not os.path.isfile(destination_handoff_path):
            parser.error(f'no external-agent handoff is available for terminal {args.destination_terminal}')

    destination = cli.load_handoff(destination_handoff_path)
    destination_token = destination.get('token')
    destination_terminal_id = destination.get('terminal_id')
    destination_url = destination.get('url')
    if not isinstance(destination_token, str) or not destination_token.startswith('agt_'):
        parser.error('destination handoff does not contain a valid external-agent token')
    if not isinstance(destination_terminal_id, str) or not destination_terminal_id:
        parser.error('destination handoff does not contain a terminal id')
    if args.destination_terminal and destination_terminal_id != args.destination_terminal:
        parser.error('destination handoff does not match --destination-terminal')
    if destination_terminal_id == args.terminal:
        parser.error('source and destination terminals must be different')
    if cli.normalized_server_url(destination_url) != cli.normalized_server_url(args.url):
        parser.error('source and destination handoffs must belong to the same StandTerm server')

    source = cli.load_handoff(args.handoff) if args.handoff else {}
    source_instance = source.get('launcher_instance_id')
    destination_instance = destination.get('launcher_instance_id')
    if source_instance and destination_instance and source_instance != destination_instance:
        parser.error('source and destination handoffs belong to different StandTerm instances')
    args.destination_token = destination_token
    args.destination_terminal_id = destination_terminal_id


def post_command(args, payload):
    return cli.post_json(
        args.url,
        payload,
        dev_mode=False,
        ca_file=args.ca_file,
        insecure=args.insecure,
    )


def print_result(result):
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get('error_code') == 'file_copy_publish_outcome_unknown':
        print(
            'Warning: the destination may already have changed; inspect it before retrying.',
            file=sys.stderr,
            flush=True,
        )


def wait_for_action(args, pending):
    action_id = pending.get('action_id')
    if not isinstance(action_id, str) or not action_id:
        return {
            'status': 'failed',
            'error_code': 'agent_action_invalid_result',
            'message': 'The backend did not return an action id.',
        }
    deadline = time.monotonic() + args.wait_seconds
    print(
        f'Waiting for browser approval of file copy action {action_id}...',
        file=sys.stderr,
        flush=True,
    )
    while time.monotonic() < deadline:
        time.sleep(args.poll_ms / 1000)
        _status, result = post_command(args, {
            'op': 'action-status',
            'token': args.token,
            'terminal_id': args.terminal,
            'action_id': action_id,
        })
        if not isinstance(result, dict):
            continue
        if result.get('status') in TERMINAL_ACTION_STATUSES:
            return result
        if result.get('status') == 'failed' and result.get('error_code'):
            return result
    return {
        'status': 'failed',
        'error_code': 'agent_action_wait_timeout',
        'action_id': action_id,
        'message': 'Timed out waiting for browser approval or copy completion.',
    }


def main():
    args = parse_args()
    _status, result = post_command(args, {
        'op': 'file-copy',
        'token': args.token,
        'terminal_id': args.terminal,
        'source_path': args.source_path,
        'destination_token': args.destination_token,
        'destination_terminal_id': args.destination_terminal_id,
        'destination_path': args.destination_path,
        'conflict_mode': args.conflict_mode.replace('-', '_'),
    })
    if not isinstance(result, dict):
        result = {
            'status': 'failed',
            'error_code': 'invalid_backend_response',
        }
    if result.get('status') == 'pending_approval' and not args.no_wait:
        result = wait_for_action(args, result)
    print_result(result)
    return 0 if result.get('status') in {'completed', 'pending_approval'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
