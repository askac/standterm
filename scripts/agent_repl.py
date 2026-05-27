#!/usr/bin/env python3
import argparse
import json
import os
import queue
import select
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

import agent_input as inputlib

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


STOP = object()
FATAL_AGENT_ERRORS = {
    'agent_not_attached',
    'agent_paused',
    'agent_privacy_blocked',
    'agent_mode_not_writable',
    'agent_external_unauthorized',
    'agent_external_expired',
    'agent_external_revoked',
    'agent_external_disconnected',
    'agent_external_origin_blocked',
    'agent_external_disabled',
    'terminal_not_found',
}
HEARTBEAT_UNSUPPORTED_ERRORS = {
    'agent_action_not_allowed',
}


def build_ssl_context(ca_file=None, insecure=False):
    if insecure:
        return ssl._create_unverified_context()
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return None


class AgentHttpClient:
    def __init__(self, base_url, token=None, terminal_id='main', timeout=30, debug=False,
                 ca_file=None, insecure=False):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.terminal_id = terminal_id
        self.timeout = timeout
        self.debug = debug
        self.ssl_context = build_ssl_context(ca_file=ca_file, insecure=insecure)

    def command_url(self):
        if self.token:
            return self.base_url + '/agent/external/command'
        return self.base_url + '/agent/external/dev-command'

    def request(self, op, **fields):
        payload = {
            'op': op,
            'terminal_id': self.terminal_id,
        }
        if self.token:
            payload['token'] = self.token
        payload.update(fields)
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            self.command_url(),
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {
                    'status': 'failed',
                    'error_code': f'http_{exc.code}',
                    'message': body,
                }
            return exc.code, payload


def parse_args():
    parser = argparse.ArgumentParser(description='Interactive REPL for a StandTerm external agent terminal')
    parser.add_argument('--handoff', help='Read url, token, and terminal from a StandTerm external agent handoff JSON file')
    parser.add_argument('--url', help='StandTerm base URL, for example http://127.0.0.1:5012')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with STANDTERM_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--ca-file', help='CA certificate bundle used to verify HTTPS StandTerm servers')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')
    parser.add_argument('--poll-ms', type=int, default=150, help='Tail polling interval in milliseconds')
    parser.add_argument('--tail-wait-ms', type=int, default=25000, help='Server-side long-poll wait for tail output in milliseconds')
    parser.add_argument('--keepalive-ms', type=int, default=60000, help='Background heartbeat interval in milliseconds')
    parser.add_argument('--no-keepalive', action='store_true', help='Disable the background heartbeat')
    parser.add_argument('--limit', type=int, default=200, help='Maximum tail events per poll')
    parser.add_argument('--coalesce-ms', type=int, default=20, help='Input coalescing window in milliseconds')
    parser.add_argument('--enter', choices=('cr', 'lf', 'crlf'), default='cr', help='Bytes sent when local Enter is pressed')
    parser.add_argument('--backspace', choices=('del', 'bs'), default='del', help='Bytes sent when local Backspace is pressed')
    parser.add_argument('--no-initial-screen', action='store_true', help='Skip initial provisional screen dump')
    type_group = parser.add_mutually_exclusive_group()
    type_group.add_argument('--type-text', help='Type this text at a controlled pace after attaching, then continue the REPL')
    type_group.add_argument('--type-file', help='Type this UTF-8 file at a controlled pace after attaching, then continue the REPL')
    type_pace_group = parser.add_mutually_exclusive_group()
    type_pace_group.add_argument('--type-cps', type=float, default=3.0, help='Paced typing characters per second, default: 3')
    type_pace_group.add_argument('--type-delay-ms', type=float, help='Paced typing delay between characters in milliseconds')
    parser.add_argument('--type-newline', choices=('cr', 'lf', 'crlf'), default='cr', help='Bytes sent for paced input newlines, default: cr')
    parser.add_argument('--type-jitter-ms', type=float, default=0, help='Random +/- jitter added to each paced typing delay in milliseconds')
    parser.add_argument('--type-punctuation-pause-ms', type=float, default=0, help='Extra paced typing delay after punctuation characters')
    parser.add_argument('--type-newline-pause-ms', type=float, default=0, help='Extra paced typing delay after a newline unit')
    parser.add_argument('--type-think-pause-prob', type=float, default=0.0, help='Probability [0..1] of inserting a random longer paced typing pause')
    parser.add_argument('--type-think-pause-ms-min', type=float, default=2200, help='Minimum paced typing think-pause length in ms')
    parser.add_argument('--type-think-pause-ms-max', type=float, default=3800, help='Maximum paced typing think-pause length in ms')
    parser.add_argument('--type-cadence-profile', choices=('generic', 'ptt'), default='generic', help='Paced typing cadence profile, default: generic')
    parser.add_argument('--type-max-uniform-seconds', type=float, default=None, help='Optional paced typing cadence guard; defaults to 30 for ptt and 0 for generic')
    parser.add_argument('--type-breaker-ms-min', type=float, default=2200, help='Minimum paced typing forced breaker pause length in ms')
    parser.add_argument('--type-breaker-ms-max', type=float, default=3800, help='Maximum paced typing forced breaker pause length in ms')
    parser.add_argument('--type-dry-run', action='store_true', help='Print a paced typing summary and exit without sending input')
    parser.add_argument('--type-progress', choices=('none', 'compact', 'jsonl'), default='compact', help='Paced typing progress format on stderr')
    parser.add_argument('--type-progress-interval-units', type=int, default=20, help='Compact paced typing progress interval in units')
    parser.add_argument('--type-wait-ms', type=int, default=3000, help='Wait up to this long after paced typing when --type-wait-quiet-ms is set')
    parser.add_argument('--type-wait-quiet-ms', type=int, help='After paced typing, wait until terminal output has been quiet this long')
    parser.add_argument('--allow-non-direct', action='store_true', help='Allow running when Agent mode is not direct_active')
    parser.add_argument('--debug', action='store_true', help='Print command acknowledgements to stderr')
    parser.add_argument('--escape', default='ctrl-]', help='Local detach key. Only ctrl-] is supported for now.')
    args = parser.parse_args()
    apply_handoff(args)
    normalize_type_args(args)
    if not args.url:
        parser.error('--url is required unless --handoff provides url')
    return args


def normalize_type_args(args):
    if args.type_max_uniform_seconds is None:
        args.type_max_uniform_seconds = 30 if args.type_cadence_profile == 'ptt' else 0


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


def stderr_line(message):
    sys.stderr.write(message + '\n')
    sys.stderr.flush()


def write_stdout(text):
    if not text:
        return
    sys.stdout.write(text)
    sys.stdout.flush()


def normalize_key(data, enter_mode, backspace_mode):
    if data in ('\r', '\n'):
        if enter_mode == 'cr':
            return '\r'
        if enter_mode == 'crlf':
            return '\r\n'
        return '\n'
    if data in ('\x7f', '\b'):
        return '\x7f' if backspace_mode == 'del' else '\b'
    return data


def print_initial_screen(client):
    _status, result = client.request('screen')
    if result.get('status') == 'failed':
        stderr_line(f"[external-agent] initial screen failed: {result.get('error_code')}")
        return current_output_seq(client)
    screen = result.get('screen')
    if isinstance(screen, dict):
        print_screen_payload(result)
    return int(result.get('output_seq') or 0)


def print_screen_payload(result):
    screen = result.get('screen') if isinstance(result, dict) else None
    if not isinstance(screen, dict):
        return
    lines = screen.get('lines')
    if isinstance(lines, list) and lines:
        write_stdout('\n'.join(str(line) for line in lines))
        write_stdout('\n')


def current_output_seq(client):
    _status, result = client.request('tail', since_output_seq=0, limit=1)
    if result.get('status') == 'failed':
        stderr_line(f"[external-agent] initial tail failed: {result.get('error_code')}")
        return 0
    output_seq = result.get('output_seq')
    return output_seq if isinstance(output_seq, int) else 0


def assert_repl_ready(client, allow_non_direct):
    _status, result = client.request('state')
    if result.get('status') == 'failed':
        raise RuntimeError(result.get('error_code') or 'state failed')
    mode = result.get('mode')
    if mode != 'direct_active' and not allow_non_direct:
        raise RuntimeError(f"terminal is not direct_active: {mode}")
    return result


def format_token_status(state):
    token_state = state.get('external_agent_token') if isinstance(state, dict) else None
    if not isinstance(token_state, dict):
        return ''
    remaining_idle_ms = token_state.get('remaining_idle_ms')
    if isinstance(remaining_idle_ms, int):
        return f" token_idle_s={max(0, remaining_idle_ms // 1000)}"
    token_lifetime = token_state.get('token_lifetime')
    if token_lifetime == 'session':
        return ' token_lifetime=session'
    return ''


def warn_tail_gap(result, last_seq):
    gap = result.get('gap')
    if isinstance(gap, dict) and gap.get('detected'):
        gap_from = gap.get('from_output_seq')
        gap_to = gap.get('to_output_seq')
        missing_count = gap.get('missing_count')
        stderr_line(
            f"[external-agent] warning: output gap {gap_from}..{gap_to} "
            f"({missing_count} events no longer available)"
        )
        return
    events = result.get('events')
    if not isinstance(events, list) or not events:
        return
    first_seq = events[0].get('output_seq')
    if isinstance(first_seq, int) and last_seq and first_seq > last_seq + 1:
        stderr_line(f"[external-agent] warning: output gap {last_seq + 1}..{first_seq - 1}")


def tail_worker(client, last_seq, stop_event, poll_seconds, limit, tail_wait_ms):
    while not stop_event.is_set():
        _status, result = client.request(
            'tail',
            since_output_seq=last_seq,
            limit=limit,
            wait_ms=tail_wait_ms,
        )
        if result.get('status') == 'failed':
            error_code = result.get('error_code') or 'tail failed'
            stderr_line(f"[external-agent] tail failed: {error_code}")
            if error_code in FATAL_AGENT_ERRORS:
                stop_event.set()
                return
            time.sleep(max(poll_seconds, 0.1))
            continue
        events = result.get('events')
        warn_tail_gap(result, last_seq)
        if isinstance(events, list) and events:
            for event in events:
                seq = event.get('output_seq')
                if isinstance(seq, int):
                    last_seq = max(last_seq, seq)
                if event.get('message_type') == 'terminal':
                    write_stdout(str(event.get('data') or ''))
        output_seq = result.get('output_seq')
        if isinstance(output_seq, int):
            last_seq = max(last_seq, output_seq if not events else last_seq)
        if tail_wait_ms <= 0:
            time.sleep(poll_seconds)


def keepalive_worker(client, stop_event, interval_seconds, debug, wait_func=None):
    if interval_seconds <= 0:
        return
    wait_func = wait_func or stop_event.wait
    use_heartbeat = True
    while not stop_event.is_set():
        if wait_func(interval_seconds):
            return
        op = 'heartbeat' if use_heartbeat else 'state'
        _status, result = client.request(op)
        if result.get('status') == 'failed':
            error_code = result.get('error_code') or 'keepalive failed'
            if use_heartbeat and error_code in HEARTBEAT_UNSUPPORTED_ERRORS:
                use_heartbeat = False
                if debug:
                    stderr_line('[external-agent] heartbeat unsupported; falling back to state keepalive')
                _status, result = client.request('state')
                if result.get('status') != 'failed':
                    if debug:
                        stderr_line('[external-agent] keepalive: ' + json.dumps(result, sort_keys=True))
                    continue
                error_code = result.get('error_code') or 'keepalive failed'
            stderr_line(f"[external-agent] keepalive failed: {error_code}")
            if error_code in FATAL_AGENT_ERRORS:
                stop_event.set()
                return
            continue
        if debug:
            stderr_line('[external-agent] keepalive: ' + json.dumps(result, sort_keys=True))


class ReplPostJson:
    def __init__(self, client):
        self.client = client

    def __call__(self, _base_url, payload, dev_mode=False, ca_file=None, insecure=False):
        payload = dict(payload)
        op = payload.pop('op')
        payload.pop('terminal_id', None)
        payload.pop('token', None)
        return self.client.request(op, **payload)


def has_startup_type_request(args):
    return bool(args.type_text is not None or args.type_file)


def read_startup_type_text(args):
    if args.type_text is not None:
        return args.type_text
    if args.type_file:
        try:
            with open(args.type_file, 'r', encoding='utf-8') as handle:
                return handle.read()
        except OSError as exc:
            raise RuntimeError(f'failed to read type file: {exc}') from exc
    return ''


def build_type_args(args):
    return argparse.Namespace(
        url=args.url,
        terminal=args.terminal,
        token=args.token,
        ca_file=args.ca_file,
        insecure=args.insecure,
        cps=args.type_cps,
        delay_ms=args.type_delay_ms,
        jitter_ms=args.type_jitter_ms,
        punctuation_pause_ms=args.type_punctuation_pause_ms,
        newline_pause_ms=args.type_newline_pause_ms,
        think_pause_prob=args.type_think_pause_prob,
        think_pause_ms_min=args.type_think_pause_ms_min,
        think_pause_ms_max=args.type_think_pause_ms_max,
        max_uniform_seconds=args.type_max_uniform_seconds,
        breaker_ms_min=args.type_breaker_ms_min,
        breaker_ms_max=args.type_breaker_ms_max,
        progress_mode=args.type_progress,
        progress_interval_units=args.type_progress_interval_units,
        progress=False,
    )


def run_startup_type(client, args, stop_event):
    text = read_startup_type_text(args)
    units = list(inputlib.iter_type_units(text, newline_mode=args.type_newline))
    summary = inputlib.summarize_units(units)
    type_args = build_type_args(args)
    if args.type_dry_run:
        delays = inputlib.plan_delays(units, type_args)
        output = {
            'status': 'dry_run',
            'cadence_profile': args.type_cadence_profile,
            'estimated_total_seconds': round(sum(delays), 1),
            **summary,
        }
        if args.type_cadence_profile == 'ptt' or (args.type_max_uniform_seconds or 0) > 0:
            output.update(inputlib.simulate_ptt_cadence(units, delays))
        stderr_line('[external-agent] type dry-run: ' + json.dumps(output, ensure_ascii=False, sort_keys=True))
        stop_event.set()
        return output

    stderr_line(
        f"[external-agent] type start units={summary['unit_count']} "
        f"bytes={summary['byte_count']} cadence={args.type_cadence_profile}"
    )
    result = inputlib.type_units(
        type_args,
        units,
        post_json=ReplPostJson(client),
    )
    output = {
        **summary,
        **result,
    }
    status = output.get('status')
    if status == 'failed':
        error_code = output.get('error_code') or 'type failed'
        stderr_line(f"[external-agent] type failed: {error_code}")
        if error_code in FATAL_AGENT_ERRORS:
            stop_event.set()
        return output
    stderr_line(
        f"[external-agent] type completed units={output.get('sent_units', 0)} "
        f"bytes={output.get('sent_bytes', 0)}"
    )
    if args.type_wait_quiet_ms is not None:
        _status, screen = client.request(
            'screen',
            wait_ms=args.type_wait_ms,
            quiet_ms=args.type_wait_quiet_ms,
        )
        if screen.get('status') == 'failed':
            error_code = screen.get('error_code') or 'screen failed'
            stderr_line(f"[external-agent] type wait-quiet failed: {error_code}")
            if error_code in FATAL_AGENT_ERRORS:
                stop_event.set()
        else:
            screen_wait = screen.get('screen_wait')
            if isinstance(screen_wait, dict):
                stderr_line('[external-agent] type wait-quiet: ' + json.dumps(screen_wait, sort_keys=True))
            print_screen_payload(screen)
    return output


def handle_send_result(result, stop_event):
    if result.get('status') != 'failed':
        return False
    error_code = result.get('error_code') or 'send failed'
    stderr_line(f"[external-agent] send failed: {error_code}")
    if error_code in FATAL_AGENT_ERRORS:
        stop_event.set()
        return True
    return False


def send_worker(client, input_queue, stop_event, coalesce_seconds, debug):
    pending = []
    deadline = None
    while True:
        timeout = 0.1
        if pending and deadline is not None:
            timeout = max(0, deadline - time.monotonic())
        try:
            item = input_queue.get(timeout=timeout)
        except queue.Empty:
            item = None
        if item is STOP:
            break
        if isinstance(item, str):
            pending.append(item)
            if item in ('\r', '\n', '\r\n', '\x03', '\x7f', '\b'):
                deadline = time.monotonic()
            elif deadline is None:
                deadline = time.monotonic() + coalesce_seconds
        if pending and (item is None or deadline is not None and time.monotonic() >= deadline):
            data = ''.join(pending)
            pending.clear()
            deadline = None
            _status, result = client.request('send', data=data)
            if debug:
                stderr_line('[external-agent] send: ' + json.dumps(result, sort_keys=True))
            if handle_send_result(result, stop_event):
                return
    if pending:
        _status, result = client.request('send', data=''.join(pending))
        if debug:
            stderr_line('[external-agent] send: ' + json.dumps(result, sort_keys=True))
        handle_send_result(result, stop_event)


def is_local_escape(ch, escape):
    return escape == 'ctrl-]' and ch == '\x1d'


def posix_input_loop(input_queue, stop_event, enter_mode, backspace_mode, escape):
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while not stop_event.is_set():
            readable, _writable, _error = select.select([sys.stdin], [], [], 0.1)
            if not readable:
                continue
            data = os.read(fd, 1024).decode('utf-8', errors='surrogateescape')
            if not data:
                break
            for ch in data:
                if is_local_escape(ch, escape):
                    stderr_line('[external-agent] detached')
                    stop_event.set()
                    return
                input_queue.put(normalize_key(ch, enter_mode, backspace_mode))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def windows_input_loop(input_queue, stop_event, enter_mode, backspace_mode, escape):
    while not stop_event.is_set():
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        data = msvcrt.getwch()
        if data in ('\x00', '\xe0'):
            data += msvcrt.getwch()
        if is_local_escape(data, escape):
            stderr_line('[external-agent] detached')
            stop_event.set()
            return
        input_queue.put(normalize_key(data, enter_mode, backspace_mode))


def pipe_input_loop(input_queue, enter_mode, backspace_mode):
    while True:
        data = sys.stdin.read(4096)
        if not data:
            break
        for ch in data:
            input_queue.put(normalize_key(ch, enter_mode, backspace_mode))


def run_repl(args):
    client = AgentHttpClient(
        args.url,
        token=args.token,
        terminal_id=args.terminal,
        debug=args.debug,
        ca_file=args.ca_file,
        insecure=args.insecure,
    )
    state = assert_repl_ready(client, args.allow_non_direct)
    stderr_line(
        f"[external-agent] attached to {args.terminal} "
        f"mode={state.get('mode')}{format_token_status(state)}"
    )
    last_seq = current_output_seq(client) if args.no_initial_screen else print_initial_screen(client)

    stop_event = threading.Event()
    input_queue = queue.Queue()
    poll_seconds = max(args.poll_ms, 20) / 1000.0
    coalesce_seconds = max(args.coalesce_ms, 0) / 1000.0
    tail_wait_ms = max(args.tail_wait_ms, 0)
    keepalive_ms = 0 if args.no_keepalive else max(args.keepalive_ms, 0)
    keepalive_seconds = keepalive_ms / 1000.0
    type_only = has_startup_type_request(args) and args.type_dry_run

    tail_thread = threading.Thread(
        target=tail_worker,
        args=(client, last_seq, stop_event, poll_seconds, args.limit, tail_wait_ms),
        daemon=True,
    )
    sender_thread = threading.Thread(
        target=send_worker,
        args=(client, input_queue, stop_event, coalesce_seconds, args.debug),
        daemon=True,
    )
    keepalive_thread = None
    if keepalive_seconds > 0:
        keepalive_thread = threading.Thread(
            target=keepalive_worker,
            args=(client, stop_event, keepalive_seconds, args.debug),
            daemon=True,
        )
    tail_thread.start()
    sender_thread.start()
    if keepalive_thread:
        keepalive_thread.start()

    try:
        if has_startup_type_request(args):
            run_startup_type(client, args, stop_event)
        if type_only:
            return
        if stop_event.is_set():
            return
        if sys.stdin.isatty() and termios and tty:
            posix_input_loop(input_queue, stop_event, args.enter, args.backspace, args.escape)
        elif sys.stdin.isatty() and msvcrt:
            windows_input_loop(input_queue, stop_event, args.enter, args.backspace, args.escape)
        else:
            pipe_input_loop(input_queue, args.enter, args.backspace)
    except KeyboardInterrupt:
        input_queue.put('\x03')
    finally:
        stop_event.set()
        input_queue.put(STOP)
        sender_thread.join(timeout=2)
        tail_thread.join(timeout=2)
        if keepalive_thread:
            keepalive_thread.join(timeout=2)


def main():
    args = parse_args()
    try:
        run_repl(args)
        return 0
    except RuntimeError as exc:
        stderr_line(f"[external-agent] {exc}")
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
