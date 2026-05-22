#!/usr/bin/env python3
import argparse
import json
import os
import queue
import select
import sys
import threading
import time
import urllib.error
import urllib.request

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
    'agent_external_disabled',
    'terminal_not_found',
}


class AgentHttpClient:
    def __init__(self, base_url, token=None, terminal_id='main', timeout=30, debug=False):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.terminal_id = terminal_id
        self.timeout = timeout
        self.debug = debug

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
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
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
    parser = argparse.ArgumentParser(description='Interactive REPL for a WebSSH external agent terminal')
    parser.add_argument('--url', required=True, help='WebSSH base URL, for example http://127.0.0.1:5012')
    parser.add_argument('--token', help='External agent attach token. Omit only on dev servers with WEBSSH_AGENT_DEV_TOKEN=1.')
    parser.add_argument('--terminal', default='main', help='Terminal id')
    parser.add_argument('--poll-ms', type=int, default=150, help='Tail polling interval in milliseconds')
    parser.add_argument('--limit', type=int, default=200, help='Maximum tail events per poll')
    parser.add_argument('--coalesce-ms', type=int, default=20, help='Input coalescing window in milliseconds')
    parser.add_argument('--enter', choices=('cr', 'lf', 'crlf'), default='cr', help='Bytes sent when local Enter is pressed')
    parser.add_argument('--backspace', choices=('del', 'bs'), default='del', help='Bytes sent when local Backspace is pressed')
    parser.add_argument('--no-initial-screen', action='store_true', help='Skip initial provisional screen dump')
    parser.add_argument('--allow-non-direct', action='store_true', help='Allow running when Agent mode is not direct_active')
    parser.add_argument('--debug', action='store_true', help='Print command acknowledgements to stderr')
    parser.add_argument('--escape', default='ctrl-]', help='Local detach key. Only ctrl-] is supported for now.')
    return parser.parse_args()


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
        stderr_line(f"[webssh-agent] initial screen failed: {result.get('error_code')}")
        return current_output_seq(client)
    screen = result.get('screen')
    if isinstance(screen, dict):
        lines = screen.get('lines')
        if isinstance(lines, list) and lines:
            write_stdout('\n'.join(str(line) for line in lines))
            write_stdout('\n')
    return int(result.get('output_seq') or 0)


def current_output_seq(client):
    _status, result = client.request('tail', since_output_seq=0, limit=1)
    if result.get('status') == 'failed':
        stderr_line(f"[webssh-agent] initial tail failed: {result.get('error_code')}")
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


def warn_tail_gap(result, last_seq):
    gap = result.get('gap')
    if isinstance(gap, dict) and gap.get('detected'):
        gap_from = gap.get('from_output_seq')
        gap_to = gap.get('to_output_seq')
        missing_count = gap.get('missing_count')
        stderr_line(
            f"[webssh-agent] warning: output gap {gap_from}..{gap_to} "
            f"({missing_count} events no longer available)"
        )
        return
    events = result.get('events')
    if not isinstance(events, list) or not events:
        return
    first_seq = events[0].get('output_seq')
    if isinstance(first_seq, int) and last_seq and first_seq > last_seq + 1:
        stderr_line(f"[webssh-agent] warning: output gap {last_seq + 1}..{first_seq - 1}")


def tail_worker(client, last_seq, stop_event, poll_seconds, limit):
    while not stop_event.is_set():
        _status, result = client.request('tail', since_output_seq=last_seq, limit=limit)
        if result.get('status') == 'failed':
            error_code = result.get('error_code') or 'tail failed'
            stderr_line(f"[webssh-agent] tail failed: {error_code}")
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
        time.sleep(poll_seconds)


def handle_send_result(result, stop_event):
    if result.get('status') != 'failed':
        return False
    error_code = result.get('error_code') or 'send failed'
    stderr_line(f"[webssh-agent] send failed: {error_code}")
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
                stderr_line('[webssh-agent] send: ' + json.dumps(result, sort_keys=True))
            if handle_send_result(result, stop_event):
                return
    if pending:
        _status, result = client.request('send', data=''.join(pending))
        if debug:
            stderr_line('[webssh-agent] send: ' + json.dumps(result, sort_keys=True))
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
                    stderr_line('[webssh-agent] detached')
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
            stderr_line('[webssh-agent] detached')
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
    client = AgentHttpClient(args.url, token=args.token, terminal_id=args.terminal, debug=args.debug)
    state = assert_repl_ready(client, args.allow_non_direct)
    stderr_line(f"[webssh-agent] attached to {args.terminal} mode={state.get('mode')}")
    last_seq = current_output_seq(client) if args.no_initial_screen else print_initial_screen(client)

    stop_event = threading.Event()
    input_queue = queue.Queue()
    poll_seconds = max(args.poll_ms, 20) / 1000.0
    coalesce_seconds = max(args.coalesce_ms, 0) / 1000.0

    tail_thread = threading.Thread(
        target=tail_worker,
        args=(client, last_seq, stop_event, poll_seconds, args.limit),
        daemon=True,
    )
    sender_thread = threading.Thread(
        target=send_worker,
        args=(client, input_queue, stop_event, coalesce_seconds, args.debug),
        daemon=True,
    )
    tail_thread.start()
    sender_thread.start()

    try:
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


def main():
    args = parse_args()
    try:
        run_repl(args)
        return 0
    except RuntimeError as exc:
        stderr_line(f"[webssh-agent] {exc}")
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
