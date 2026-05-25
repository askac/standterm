#!/usr/bin/env python3
"""Type text into a StandTerm external-agent terminal at a controlled pace.

This helper is a thin CLI wrapper around the shared terminal input pacing
helpers in ``agent_input.py``. Its default profile is generic and does not
assume any application-specific rhythm rules. Use ``--cadence-profile ptt`` for
the optional whole-second cadence guard used by PTT-style editors.
"""
import argparse
import json
import sys

import agent_cli as cli
import agent_input as inputlib


newline_bytes = inputlib.newline_bytes
iter_type_units = inputlib.iter_type_units
base_delay_seconds = inputlib.base_delay_seconds
delay_for_unit = inputlib.delay_for_unit
build_send_payload = inputlib.build_send_payload
summarize_units = inputlib.summarize_units
stderr_json = inputlib.stderr_json
guarded_delay = inputlib.guarded_delay
type_units = inputlib.type_units
plan_delays = inputlib.plan_delays
simulate_ptt_cadence = inputlib.simulate_ptt_cadence


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            'Type text into a StandTerm external agent terminal at a controlled pace. '
            'StandTerm terminal input is one shared stream; do not send cursor-moving '
            'input from another viewer or helper while this command is running.'
        )
    )
    parser.add_argument('--handoff', help='Read url, token, and terminal from a StandTerm external agent handoff JSON file')
    parser.add_argument('--url', help='StandTerm base URL, for example http://127.0.0.1:5010')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with STANDTERM_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS StandTerm servers')
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
    parser.add_argument('--newline-pause-ms', type=float, default=0, help='Extra delay after a newline unit')
    parser.add_argument('--think-pause-prob', type=float, default=0.0, help='Probability [0..1] of inserting a random longer think pause after a unit')
    parser.add_argument('--think-pause-ms-min', type=float, default=2200, help='Minimum think-pause length in ms')
    parser.add_argument('--think-pause-ms-max', type=float, default=3800, help='Maximum think-pause length in ms')
    parser.add_argument('--cadence-profile', choices=('generic', 'ptt'), default='generic',
                        help='Application cadence profile, default: generic')
    parser.add_argument('--max-uniform-seconds', type=float, default=None,
                        help='Optional cadence guard. Defaults to 30 for --cadence-profile ptt and 0 for generic.')
    parser.add_argument('--breaker-ms-min', type=float, default=2200, help='Minimum forced breaker pause length in ms')
    parser.add_argument('--breaker-ms-max', type=float, default=3800, help='Maximum forced breaker pause length in ms')
    parser.add_argument('--dry-run', action='store_true', help='Print the typing plan without sending input')
    parser.add_argument('--progress', action='store_true', help='Print one JSONL progress record per sent unit to stderr')
    args = parser.parse_args(argv)
    normalize_cadence_args(args)
    cli.apply_handoff(args)
    if not args.url and not args.dry_run:
        parser.error('--url is required unless --handoff provides url')
    return args


def normalize_cadence_args(args):
    if args.max_uniform_seconds is None:
        args.max_uniform_seconds = 30 if args.cadence_profile == 'ptt' else 0


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


def dry_run_output(args, units, summary, random_uniform):
    delays = plan_delays(units, args, random_uniform=random_uniform)
    output = {
        'status': 'dry_run',
        'cadence_profile': args.cadence_profile,
        'estimated_total_seconds': round(sum(delays), 1),
        **summary,
    }
    if args.cadence_profile == 'ptt' or (args.max_uniform_seconds or 0) > 0:
        output.update(simulate_ptt_cadence(units, delays))
    return output


def run(args, post_json=cli.post_json, sleep=inputlib.time.sleep, random_uniform=inputlib.random.uniform):
    text = read_input_text(args)
    units = list(iter_type_units(text, newline_mode=args.newline))
    summary = summarize_units(units)
    if args.dry_run:
        print(json.dumps(dry_run_output(args, units, summary, random_uniform), ensure_ascii=False, indent=2, sort_keys=True))
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
