#!/usr/bin/env python3
"""Type text into a StandTerm external-agent terminal at a controlled, human-like
pace.

Why the cadence options exist
-----------------------------
Some terminal applications score input *rhythm* server-side. PTT BBS does this
in its editor (pttbbs `mbbsd/edit.c`): while editing it keeps a per-keystroke
reward counter and, crucially, an anti-bot guard that works at **whole-second**
granularity:

    if ((interval = (now - th))) {        // now/th are seconds (time4_t)
        th = now;
        if ((char)ch != last) { money++; last = (char)ch; }
    }
    if (interval && interval == tin) {     // same integer-second gap again
        count++;
        if (count > 60) { money = 0; count = 0; /* may also flag/kick */ }
    } else if (interval) { count = 0; tin = interval; }

Consequences:
- `money` rises at most once per second (and only on a *different* char), so
  bursting many chars in one second earns nothing extra.
- If the integer-second gap between counted keystrokes stays identical for >60
  in a row, `money` is wiped. A steady ~1-3 cps stream crosses second
  boundaries at a constant 1-second interval and will trip this.
- Sub-second `--jitter-ms` / `--punctuation-pause-ms` are INVISIBLE to this
  check (it only sees whole seconds). To defeat it you must vary the cadence at
  the *second* level: occasionally insert a >=2s pause so the integer-second
  interval changes and the uniform run resets.

`--max-uniform-seconds` is the robust defence: it guarantees a multi-second
breaker pause at least that often, keeping the uniform run well under 60.
`--dry-run` simulates the edit.c counter over the planned schedule and reports
the worst-case uniform run so you can verify before sending.
"""
import argparse
import json
import random
import sys
import time

import agent_cli as cli


PUNCTUATION_PAUSE_CHARS = set('.!?;:,，。！？；：、')
NEWLINE_UNITS = ('\r', '\n', '\r\n')
# A pause is a reliable cadence "breaker" only if it advances the server's
# integer second clock by >= 2, so the breaker pause must comfortably exceed 1s.
BREAKER_THRESHOLD_SECONDS = 2.0


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
    parser.add_argument('--jitter-ms', type=float, default=0, help='Random +/- jitter added to each delay in milliseconds (sub-second; invisible to second-granularity rhythm checks)')
    parser.add_argument('--punctuation-pause-ms', type=float, default=0, help='Extra delay after punctuation characters')
    parser.add_argument('--newline-pause-ms', type=float, default=0, help='Extra delay after a newline unit (human paragraph/line pause)')
    parser.add_argument('--think-pause-prob', type=float, default=0.0, help='Probability [0..1] of inserting a random longer "think" pause after a unit')
    parser.add_argument('--think-pause-ms-min', type=float, default=2200, help='Minimum think-pause length in ms (used when a think pause fires)')
    parser.add_argument('--think-pause-ms-max', type=float, default=3800, help='Maximum think-pause length in ms')
    parser.add_argument('--max-uniform-seconds', type=float, default=30,
                        help='Cadence guard (default 30, ON): force a multi-second breaker pause at least this '
                             'often so the whole-second input interval cannot stay uniform (defeats PTT edit.c '
                             'money=0 after 60 equal intervals). Set 0 to disable.')
    parser.add_argument('--breaker-ms-min', type=float, default=2200, help='Minimum forced breaker pause length in ms (must exceed 2000 to change the integer-second interval)')
    parser.add_argument('--breaker-ms-max', type=float, default=3800, help='Maximum forced breaker pause length in ms')
    parser.add_argument('--dry-run', action='store_true', help='Print the typing plan (and a simulated PTT edit.c cadence/reward check) without sending input')
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
    jitter_ms = max(getattr(args, 'jitter_ms', 0) or 0, 0)
    if jitter_ms:
        delay += random_uniform(-jitter_ms, jitter_ms) / 1000.0
    if unit in PUNCTUATION_PAUSE_CHARS and (getattr(args, 'punctuation_pause_ms', 0) or 0) > 0:
        delay += args.punctuation_pause_ms / 1000.0
    if unit in NEWLINE_UNITS and (getattr(args, 'newline_pause_ms', 0) or 0) > 0:
        delay += args.newline_pause_ms / 1000.0
    think_prob = getattr(args, 'think_pause_prob', 0) or 0
    if think_prob > 0 and random_uniform(0, 1) < think_prob:
        lo = getattr(args, 'think_pause_ms_min', 2200)
        hi = max(getattr(args, 'think_pause_ms_max', 3800), lo)
        delay += random_uniform(lo, hi) / 1000.0
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


def guarded_delay(delay, secs_since_breaker, args, random_uniform=random.uniform):
    """Apply the second-granularity cadence guard to a planned delay.

    Returns (delay, secs_since_breaker). When the time since the last breaker
    would exceed ``--max-uniform-seconds`` and this delay is not already a
    breaker, the delay is replaced by a randomized multi-second breaker so the
    integer-second input interval cannot stay uniform long enough to trip
    PTT edit.c's `count > 60` money wipe. A no-op when the guard is disabled.
    """
    max_uniform = getattr(args, 'max_uniform_seconds', 0) or 0
    if max_uniform <= 0:
        return delay, secs_since_breaker
    if delay >= BREAKER_THRESHOLD_SECONDS:
        return delay, 0.0
    if secs_since_breaker + delay >= max_uniform:
        lo = max(getattr(args, 'breaker_ms_min', 2200), BREAKER_THRESHOLD_SECONDS * 1000.0)
        hi = max(getattr(args, 'breaker_ms_max', 3800), lo)
        return random_uniform(lo, hi) / 1000.0, 0.0
    return delay, secs_since_breaker + delay


def type_units(args, units, post_json=cli.post_json, sleep=time.sleep, random_uniform=random.uniform):
    sent_units = 0
    sent_bytes = 0
    last_result = None
    secs_since_breaker = 0.0
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
            delay = delay_for_unit(unit, args, random_uniform=random_uniform)
            delay, secs_since_breaker = guarded_delay(
                delay, secs_since_breaker, args, random_uniform=random_uniform
            )
            sleep(delay)

    return {
        'status': 'completed',
        'sent_units': sent_units,
        'sent_bytes': sent_bytes,
        'last_result': last_result,
    }


def plan_delays(units, args, random_uniform=random.uniform):
    """Produce a representative per-unit delay schedule (same logic the sender
    uses), including jitter, pauses, and the cadence guard."""
    delays = []
    secs_since_breaker = 0.0
    last_index = len(units) - 1
    for index, unit in enumerate(units):
        if index >= last_index:
            delays.append(0.0)
            break
        delay = delay_for_unit(unit, args, random_uniform=random_uniform)
        delay, secs_since_breaker = guarded_delay(
            delay, secs_since_breaker, args, random_uniform=random_uniform
        )
        delays.append(delay)
    return delays


def simulate_ptt_cadence(units, delays):
    """Replay PTT edit.c's whole-second reward/anti-bot counter over a planned
    schedule. The cadence run is timing-driven and reliable; the money figure is
    an approximation (the server counts per input byte, not per Unicode unit)."""
    th = None
    last = None
    tin = 0
    count = 0
    money = 0
    max_run = 0
    wiped = False
    t = 0.0
    for index, unit in enumerate(units):
        sec = int(t)
        if th is None:
            th = sec
        interval = sec - th
        if interval:
            th = sec
            ch = unit[-1] if unit else ''
            if ch != last:
                money += 1
                last = ch
        if interval and interval == tin:
            count += 1
            if count > max_run:
                max_run = count
            if count > 60:
                money = 0
                count = 0
                wiped = True
        elif interval:
            count = 0
            tin = interval
        t += delays[index]
    return {
        'estimated_total_seconds': round(t, 1),
        'simulated_money_raw': money,
        'simulated_max_uniform_run': max_run,
        'money_would_be_wiped': wiped,
        'cadence_ok': (not wiped) and max_run <= 55,
    }


def run(args, post_json=cli.post_json, sleep=time.sleep, random_uniform=random.uniform):
    text = read_input_text(args)
    units = list(iter_type_units(text, newline_mode=args.newline))
    summary = summarize_units(units)
    if args.dry_run:
        delays = plan_delays(units, args, random_uniform=random_uniform)
        output = {
            'status': 'dry_run',
            **summary,
            **simulate_ptt_cadence(units, delays),
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
