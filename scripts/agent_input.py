import json
import random
import sys
import time


KEY_INPUTS = {
    'Enter': '\r',
    'Return': '\r',
    'Tab': '\t',
    'Escape': '\x1b',
    'Esc': '\x1b',
    'Backspace': '\x7f',
    'Delete': '\x1b[3~',
    'Up': '\x1b[A',
    'Down': '\x1b[B',
    'Right': '\x1b[C',
    'Left': '\x1b[D',
    'Home': '\x1b[H',
    'End': '\x1b[F',
    'PageUp': '\x1b[5~',
    'PageDown': '\x1b[6~',
}

PUNCTUATION_PAUSE_CHARS = set('.!?;:,，。！？；：、')
NEWLINE_UNITS = ('\r', '\n', '\r\n')
# A pause is a reliable cadence breaker only if it advances a whole-second
# server clock by at least two seconds.
BREAKER_THRESHOLD_SECONDS = 2.0


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


def guarded_delay(delay, secs_since_breaker, args, random_uniform=random.uniform):
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
        'contains_newline_translation': any(unit in NEWLINE_UNITS for unit in units),
    }


def stderr_json(payload):
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
    sys.stderr.flush()


def stderr_compact(message):
    sys.stderr.write(message + '\n')
    sys.stderr.flush()


def emit_type_progress(args, index, sent_units, sent_bytes, total_units):
    mode = getattr(args, 'progress_mode', None)
    if mode is None:
        mode = 'jsonl' if getattr(args, 'progress', False) else 'none'
    if mode == 'none':
        return
    if mode == 'jsonl':
        stderr_json({
            'event': 'typed_unit',
            'unit_index': index,
            'sent_units': sent_units,
            'sent_bytes': sent_bytes,
            'total_units': total_units,
        })
        return
    if mode == 'compact':
        interval = max(getattr(args, 'progress_interval_units', 20) or 20, 1)
        if sent_units == 1 or sent_units == total_units or sent_units % interval == 0:
            stderr_compact(
                f"[external-agent] type progress units={sent_units}/{total_units} "
                f"bytes={sent_bytes}"
            )


def type_units(args, units, post_json, sleep=time.sleep, random_uniform=random.uniform):
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
        emit_type_progress(args, index, sent_units, sent_bytes, len(units))
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
