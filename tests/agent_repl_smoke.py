import queue
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import webssh_agent_repl as repl


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def request(self, op, **fields):
        self.requests.append((op, fields))
        if self.responses:
            return 200, self.responses.pop(0)
        return 200, {'status': 'completed'}


def run_send_worker(items, responses=None, coalesce_seconds=1):
    input_queue = queue.Queue()
    stop_event = threading.Event()
    client = FakeClient(responses=responses)
    for item in items:
        input_queue.put(item)
    input_queue.put(repl.STOP)
    repl.send_worker(client, input_queue, stop_event, coalesce_seconds=coalesce_seconds, debug=False)
    return client, stop_event


def test_normalize_key_modes():
    assert repl.normalize_key('\n', 'cr', 'del') == '\r'
    assert repl.normalize_key('\r', 'lf', 'del') == '\n'
    assert repl.normalize_key('\n', 'crlf', 'del') == '\r\n'
    assert repl.normalize_key('\b', 'cr', 'del') == '\x7f'
    assert repl.normalize_key('\x7f', 'cr', 'bs') == '\b'
    assert repl.normalize_key('x', 'cr', 'del') == 'x'


def test_send_worker_coalesces_pending_input_on_stop():
    client, stop_event = run_send_worker(['p', 'w', 'd', '\r'])
    assert stop_event.is_set() is False
    assert client.requests == [
        ('send', {'data': 'pwd\r'}),
    ]


def test_send_worker_stops_on_fatal_error():
    client, stop_event = run_send_worker(
        ['x'],
        responses=[{'status': 'failed', 'error_code': 'agent_external_revoked'}],
    )
    assert client.requests == [
        ('send', {'data': 'x'}),
    ]
    assert stop_event.is_set() is True


def test_send_worker_stops_on_not_attached_error():
    client, stop_event = run_send_worker(
        ['x'],
        responses=[{'status': 'failed', 'error_code': 'agent_not_attached'}],
    )
    assert client.requests == [
        ('send', {'data': 'x'}),
    ]
    assert stop_event.is_set() is True


def test_send_worker_drops_queued_input_after_fatal_error():
    client, stop_event = run_send_worker(
        ['x', 'y'],
        responses=[{'status': 'failed', 'error_code': 'agent_external_revoked'}],
        coalesce_seconds=0,
    )
    assert client.requests == [
        ('send', {'data': 'x'}),
    ]
    assert stop_event.is_set() is True


def test_send_worker_keeps_running_on_transient_human_lease():
    client, stop_event = run_send_worker(
        ['x'],
        responses=[{'status': 'failed', 'error_code': 'agent_human_input_active'}],
    )
    assert client.requests == [
        ('send', {'data': 'x'}),
    ]
    assert stop_event.is_set() is False


def test_tail_worker_stops_on_not_attached_error():
    client = FakeClient(responses=[
        {'status': 'failed', 'error_code': 'agent_not_attached'},
    ])
    stop_event = threading.Event()
    repl.tail_worker(client, 0, stop_event, poll_seconds=0, limit=10)
    assert client.requests == [
        ('tail', {'since_output_seq': 0, 'limit': 10}),
    ]
    assert stop_event.is_set() is True


def main():
    tests = [
        test_normalize_key_modes,
        test_send_worker_coalesces_pending_input_on_stop,
        test_send_worker_stops_on_fatal_error,
        test_send_worker_stops_on_not_attached_error,
        test_send_worker_drops_queued_input_after_fatal_error,
        test_send_worker_keeps_running_on_transient_human_lease,
        test_tail_worker_stops_on_not_attached_error,
    ]
    for test in tests:
        test()
        print(f'{test.__name__}: ok')


if __name__ == '__main__':
    main()
