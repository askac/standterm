import io
import json
import queue
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import agent_cli as cli
import agent_jsonl as jsonl
import agent_repl as repl
import agent_type as typer


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def request(self, op, **fields):
        self.requests.append((op, fields))
        if self.responses:
            return 200, self.responses.pop(0)
        return 200, {'status': 'completed'}


class FakePostJson:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, base_url, payload, dev_mode=False, ca_file=None, insecure=False):
        self.calls.append({
            'base_url': base_url,
            'payload': payload,
            'dev_mode': dev_mode,
            'ca_file': ca_file,
            'insecure': insecure,
        })
        if self.responses:
            return self.responses.pop(0)
        return 200, {'status': 'ok'}


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
    repl.tail_worker(client, 0, stop_event, poll_seconds=0, limit=10, tail_wait_ms=25000)
    assert client.requests == [
        ('tail', {'since_output_seq': 0, 'limit': 10, 'wait_ms': 25000}),
    ]
    assert stop_event.is_set() is True


def test_keepalive_worker_sends_hidden_state_heartbeat():
    client = FakeClient(responses=[
        {'status': 'ok'},
    ])
    stop_event = threading.Event()
    waits = []

    def wait_func(interval):
        waits.append(interval)
        if len(waits) == 1:
            return False
        return True

    repl.keepalive_worker(
        client,
        stop_event,
        interval_seconds=12.5,
        debug=False,
        wait_func=wait_func,
    )
    assert client.requests == [
        ('state', {}),
    ]
    assert waits == [12.5, 12.5]
    assert stop_event.is_set() is False


def test_keepalive_worker_stops_on_fatal_error():
    client = FakeClient(responses=[
        {'status': 'failed', 'error_code': 'agent_external_expired'},
    ])
    stop_event = threading.Event()

    def wait_func(_interval):
        return False

    repl.keepalive_worker(
        client,
        stop_event,
        interval_seconds=1,
        debug=False,
        wait_func=wait_func,
    )
    assert client.requests == [
        ('state', {}),
    ]
    assert stop_event.is_set() is True


def make_repl_type_args(**overrides):
    values = {
        'url': 'https://127.0.0.1:5010',
        'terminal': 'term-2',
        'token': 'agt_secret',
        'ca_file': '/tmp/ca.crt',
        'insecure': False,
        'type_text': 'a\nb',
        'type_file': None,
        'type_newline': 'cr',
        'type_cps': 10.0,
        'type_delay_ms': 0,
        'type_jitter_ms': 0,
        'type_punctuation_pause_ms': 0,
        'type_newline_pause_ms': 0,
        'type_think_pause_prob': 0,
        'type_think_pause_ms_min': 2200,
        'type_think_pause_ms_max': 3800,
        'type_cadence_profile': 'generic',
        'type_max_uniform_seconds': 0,
        'type_breaker_ms_min': 2200,
        'type_breaker_ms_max': 3800,
        'type_dry_run': False,
        'type_progress': 'none',
        'type_progress_interval_units': 20,
        'type_wait_ms': 3000,
        'type_wait_quiet_ms': None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_repl_startup_type_sends_units_through_shared_pacing():
    client = FakeClient()
    stop_event = threading.Event()
    result = repl.run_startup_type(
        client,
        make_repl_type_args(),
        stop_event,
    )
    assert result['status'] == 'completed'
    assert result['sent_units'] == 3
    assert client.requests == [
        ('send', {'data': 'a'}),
        ('send', {'data': '\r'}),
        ('send', {'data': 'b'}),
    ]
    assert stop_event.is_set() is False


def test_repl_startup_type_waits_for_quiet_screen_after_typing():
    client = FakeClient(responses=[
        {'status': 'ok'},
        {'status': 'ok'},
        {'status': 'ok'},
        {'status': 'ok', 'screen_wait': {'settled': True}, 'screen': {'lines': []}},
    ])
    stop_event = threading.Event()
    result = repl.run_startup_type(
        client,
        make_repl_type_args(type_wait_quiet_ms=500, type_wait_ms=2500),
        stop_event,
    )
    assert result['status'] == 'completed'
    assert client.requests[-1] == (
        'screen',
        {'wait_ms': 2500, 'quiet_ms': 500},
    )
    assert stop_event.is_set() is False


def test_repl_startup_type_stops_on_fatal_error():
    client = FakeClient(responses=[
        {'status': 'failed', 'error_code': 'agent_external_revoked'},
    ])
    stop_event = threading.Event()
    result = repl.run_startup_type(
        client,
        make_repl_type_args(type_text='abc'),
        stop_event,
    )
    assert result['status'] == 'failed'
    assert result['error_code'] == 'agent_external_revoked'
    assert client.requests == [
        ('send', {'data': 'a'}),
    ]
    assert stop_event.is_set() is True


def test_format_token_status_reports_idle_countdown():
    assert repl.format_token_status({
        'external_agent_token': {
            'remaining_idle_ms': 124000,
        },
    }) == ' token_idle_s=124'
    assert repl.format_token_status({
        'external_agent_token': {
            'token_lifetime': 'session',
        },
    }) == ' token_lifetime=session'
    assert repl.format_token_status({}) == ''


def test_cli_and_repl_apply_handoff_defaults(tmp_path=None):
    handoff_path = Path('/tmp/standterm_agent_handoff_unit.json') if tmp_path is None else tmp_path / 'handoff.json'
    handoff_path.write_text(
        (
            '{"url":"http://127.0.0.1:5012","token":"agt_unit","terminal_id":"term-2",'
            '"transport":{"tls_ca_cert_path":"/tmp/standterm-test-ca.crt"}}\n'
        ),
        encoding='utf-8',
    )
    cli_args = SimpleNamespace(
        handoff=str(handoff_path),
        url=None,
        token=None,
        terminal='main',
        ca_file=None,
    )
    cli.apply_handoff(cli_args)
    assert cli_args.url == 'http://127.0.0.1:5012'
    assert cli_args.token == 'agt_unit'
    assert cli_args.terminal == 'term-2'
    assert cli_args.ca_file == '/tmp/standterm-test-ca.crt'

    repl_args = SimpleNamespace(
        handoff=str(handoff_path),
        url='http://override',
        token=None,
        terminal='main',
        ca_file=None,
    )
    repl.apply_handoff(repl_args)
    assert repl_args.url == 'http://override'
    assert repl_args.token == 'agt_unit'
    assert repl_args.terminal == 'term-2'
    assert repl_args.ca_file == '/tmp/standterm-test-ca.crt'
    if tmp_path is None:
        handoff_path.unlink(missing_ok=True)


def test_cli_send_capture_payload():
    args = SimpleNamespace(
        command='send',
        terminal='main',
        token='agt_unit',
        text='pwd\n',
        stdin=False,
        capture=True,
        wait_ms=2000,
        settle_ms=150,
        limit=5,
    )
    assert cli.command_payload(args) == {
        'op': 'send',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'kind': 'text',
        'text': 'pwd\n',
        'capture': True,
        'wait_ms': 2000,
        'settle_ms': 150,
        'limit': 5,
    }


def test_cli_plain_send_payload_uses_structured_text():
    args = SimpleNamespace(
        command='send',
        terminal='main',
        token='agt_unit',
        text='pwd\n',
        stdin=False,
        capture=False,
        wait_ms=None,
        settle_ms=None,
        limit=None,
    )
    assert cli.command_payload(args) == {
        'op': 'send',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'kind': 'text',
        'text': 'pwd\n',
    }


def test_cli_screen_tail_lines_payload():
    args = SimpleNamespace(
        command='screen',
        terminal='main',
        token='agt_unit',
        tail_lines=12,
        region=None,
    )
    assert cli.command_payload(args) == {
        'op': 'screen',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'tail_lines': 12,
    }


def test_cli_screen_region_payload():
    args = SimpleNamespace(
        command='screen',
        terminal='main',
        token='agt_unit',
        tail_lines=None,
        region='2:8',
    )
    assert cli.command_payload(args) == {
        'op': 'screen',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'region': {
            'top': 2,
            'bottom': 8,
        },
    }


def test_cli_screen_wait_payload():
    args = SimpleNamespace(
        command='screen',
        terminal='main',
        token='agt_unit',
        tail_lines=4,
        region=None,
        wait_ms=3000,
        quiet_ms=500,
    )
    assert cli.command_payload(args) == {
        'op': 'screen',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'wait_ms': 3000,
        'quiet_ms': 500,
        'tail_lines': 4,
    }


def test_cli_tail_strip_ansi_payload():
    args = SimpleNamespace(
        command='tail',
        terminal='main',
        token='agt_unit',
        since=0,
        limit=50,
        wait_ms=None,
        strip_ansi=True,
    )
    assert cli.command_payload(args) == {
        'op': 'tail',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'since_output_seq': 0,
        'limit': 50,
        'strip_ansi': True,
    }

    args.wait_ms = 2500
    assert cli.command_payload(args)['wait_ms'] == 2500


def test_cli_wait_output_alias_maps_to_tail_payload():
    args = SimpleNamespace(
        command='wait-output',
        terminal='main',
        token='agt_unit',
        since=7,
        limit=12,
        wait_ms=3000,
        strip_ansi=True,
    )
    assert cli.command_payload(args) == {
        'op': 'tail',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'since_output_seq': 7,
        'limit': 12,
        'wait_ms': 3000,
        'strip_ansi': True,
    }


def test_cli_wait_quiet_alias_maps_to_screen_payload():
    args = SimpleNamespace(
        command='wait-quiet',
        terminal='main',
        token='agt_unit',
        tail_lines=8,
        region=None,
        wait_ms=3000,
        quiet_ms=500,
    )
    assert cli.command_payload(args) == {
        'op': 'screen',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'wait_ms': 3000,
        'quiet_ms': 500,
        'tail_lines': 8,
    }


def test_cli_send_wait_payload_requests_capture():
    args = SimpleNamespace(
        command='send-wait',
        terminal='main',
        token='agt_unit',
        text='pwd\n',
        stdin=False,
        wait_ms=None,
        settle_ms=None,
        limit=None,
        submit=False,
    )
    assert cli.command_payload(args) == {
        'op': 'send-wait',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'kind': 'text',
        'text': 'pwd\n',
        'capture': True,
    }


def test_cli_send_wait_strip_ansi_payload_requests_plain_capture():
    args = SimpleNamespace(
        command='send-wait',
        terminal='main',
        token='agt_unit',
        text='pwd\n',
        stdin=False,
        wait_ms=None,
        settle_ms=None,
        limit=None,
        submit=False,
        strip_ansi=True,
    )
    assert cli.command_payload(args) == {
        'op': 'send-wait',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'kind': 'text',
        'text': 'pwd\n',
        'capture': True,
        'strip_ansi': True,
    }

def test_cli_send_submit_after_payload_is_structured():
    args = SimpleNamespace(
        command='send',
        terminal='main',
        token='agt_unit',
        text='codex prompt',
        stdin=False,
        key=None,
        capture=False,
        submit=True,
        wait_ms=None,
        settle_ms=None,
        limit=None,
    )
    assert cli.command_payload(args) == {
        'op': 'send',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'kind': 'text',
        'text': 'codex prompt',
        'submit_after': True,
    }


def test_cli_send_named_keys_payload_uses_structured_keys():
    args = SimpleNamespace(
        command='send',
        terminal='main',
        token='agt_unit',
        text=None,
        stdin=False,
        key=['Down', 'Enter'],
        capture=False,
        submit=False,
        wait_ms=None,
        settle_ms=None,
        limit=None,
    )
    assert cli.command_payload(args) == {
        'op': 'send',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'kind': 'keys',
        'keys': ['Down', 'Enter'],
    }


def test_cli_key_alias_maps_to_send_payload():
    args = SimpleNamespace(
        command='key',
        terminal='main',
        token='agt_unit',
        key=['Up', 'Enter'],
        capture=True,
        wait_ms=1000,
        settle_ms=100,
        limit=3,
        strip_ansi=True,
    )
    assert cli.command_payload(args) == {
        'op': 'send',
        'terminal_id': 'main',
        'token': 'agt_unit',
        'kind': 'keys',
        'keys': ['Up', 'Enter'],
        'capture': True,
        'wait_ms': 1000,
        'settle_ms': 100,
        'limit': 3,
        'strip_ansi': True,
    }


def test_cli_render_save_writes_png_and_redacts_base64():
    one_pixel_png = 'iVBORw0KGgo='
    result = {
        'status': 'ok',
        'render': {
            'mime_type': 'image/png',
            'image_base64': one_pixel_png,
            'image_byte_length': 8,
        },
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        path = str(Path(temp_dir) / 'viewport.png')
        output = cli.save_render_image(result, path)
        assert Path(path).read_bytes() == b'\x89PNG\r\n\x1a\n'
    assert result['render']['image_base64'] == one_pixel_png
    assert 'image_base64' not in output['render']
    assert output['render']['saved_path'].endswith('viewport.png')
    assert output['render']['image_byte_length'] == 8


def test_jsonl_client_reuses_defaults_and_preserves_ids():
    fake_post = FakePostJson(responses=[
        (200, {'status': 'completed', 'bytes_written': 4}),
        (200, {'status': 'ok', 'screen': {'lines': ['x']}}),
    ])
    input_stream = io.StringIO(
        '{"id":"1","op":"send-wait","data":"pwd\\n","wait_ms":2000}\n'
        '{"id":"2","op":"screen","tail_lines":12}\n'
    )
    output_stream = io.StringIO()

    jsonl.run_jsonl(
        input_stream,
        output_stream,
        'https://127.0.0.1:5010',
        token='agt_secret',
        terminal_id='term-2',
        ca_file='/tmp/ca.crt',
        post_json=fake_post,
    )

    outputs = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert outputs[0]['id'] == '1'
    assert outputs[0]['ok'] is True
    assert outputs[0]['result']['bytes_written'] == 4
    assert outputs[1]['id'] == '2'
    assert outputs[1]['ok'] is True
    assert fake_post.calls == [
        {
            'base_url': 'https://127.0.0.1:5010',
            'payload': {
                'op': 'send-wait',
                'data': 'pwd\n',
                'wait_ms': 2000,
                'terminal_id': 'term-2',
                'token': 'agt_secret',
            },
            'dev_mode': False,
            'ca_file': '/tmp/ca.crt',
            'insecure': False,
        },
        {
            'base_url': 'https://127.0.0.1:5010',
            'payload': {
                'op': 'screen',
                'tail_lines': 12,
                'terminal_id': 'term-2',
                'token': 'agt_secret',
            },
            'dev_mode': False,
            'ca_file': '/tmp/ca.crt',
            'insecure': False,
        },
    ]
    assert 'agt_secret' not in output_stream.getvalue()


def test_jsonl_client_reports_invalid_json_as_jsonl_error():
    response = jsonl.handle_line('{not json', 'http://127.0.0.1:5010', token='agt_secret')
    assert response['ok'] is False
    assert response['error_code'] == 'invalid_json'
    assert 'agt_secret' not in json.dumps(response)


def test_jsonl_client_preserves_backend_failed_result():
    fake_post = FakePostJson(responses=[
        (403, {'status': 'failed', 'error_code': 'agent_external_revoked'}),
    ])
    response = jsonl.handle_line(
        '{"id":"fail-1","op":"tail","since_output_seq":0}',
        'http://127.0.0.1:5010',
        token='agt_secret',
        post_json=fake_post,
    )
    assert response == {
        'id': 'fail-1',
        'ok': False,
        'http_status': 403,
        'result': {
            'status': 'failed',
            'error_code': 'agent_external_revoked',
        },
    }


def test_type_units_translate_newlines_and_preserve_unicode_characters():
    assert list(typer.iter_type_units('a\n測b', newline_mode='cr')) == ['a', '\r', '測', 'b']
    assert list(typer.iter_type_units('a\nb', newline_mode='lf')) == ['a', '\n', 'b']
    assert list(typer.iter_type_units('a\nb', newline_mode='crlf')) == ['a', '\r\n', 'b']


def test_type_helper_defaults_to_generic_cadence_profile():
    args = typer.parse_args(['--text', 'abc', '--dry-run'])
    assert args.cadence_profile == 'generic'
    assert args.max_uniform_seconds == 0
    ptt_args = typer.parse_args(['--text', 'abc', '--dry-run', '--cadence-profile', 'ptt'])
    assert ptt_args.max_uniform_seconds == 30


def test_type_helper_sends_one_unit_per_plain_send_without_capture():
    fake_post = FakePostJson()
    sleeps = []
    args = SimpleNamespace(
        url='https://127.0.0.1:5010',
        terminal='term-2',
        token='agt_secret',
        ca_file='/tmp/ca.crt',
        insecure=False,
        cps=2.0,
        delay_ms=None,
        jitter_ms=0,
        punctuation_pause_ms=0,
        progress=False,
    )
    result = typer.type_units(
        args,
        ['a', 'b', '測'],
        post_json=fake_post,
        sleep=sleeps.append,
    )
    assert result['status'] == 'completed'
    assert result['sent_units'] == 3
    assert result['sent_bytes'] == len('ab測'.encode('utf-8'))
    assert sleeps == [0.5, 0.5]
    assert fake_post.calls == [
        {
            'base_url': 'https://127.0.0.1:5010',
            'payload': {
                'op': 'send',
                'terminal_id': 'term-2',
                'token': 'agt_secret',
                'data': 'a',
            },
            'dev_mode': False,
            'ca_file': '/tmp/ca.crt',
            'insecure': False,
        },
        {
            'base_url': 'https://127.0.0.1:5010',
            'payload': {
                'op': 'send',
                'terminal_id': 'term-2',
                'token': 'agt_secret',
                'data': 'b',
            },
            'dev_mode': False,
            'ca_file': '/tmp/ca.crt',
            'insecure': False,
        },
        {
            'base_url': 'https://127.0.0.1:5010',
            'payload': {
                'op': 'send',
                'terminal_id': 'term-2',
                'token': 'agt_secret',
                'data': '測',
            },
            'dev_mode': False,
            'ca_file': '/tmp/ca.crt',
            'insecure': False,
        },
    ]


def test_type_helper_stops_on_failed_send_without_replaying_remaining_units():
    fake_post = FakePostJson(responses=[
        (200, {'status': 'ok'}),
        (409, {'status': 'failed', 'error_code': 'agent_human_input_active'}),
        (200, {'status': 'ok'}),
    ])
    sleeps = []
    args = SimpleNamespace(
        url='http://127.0.0.1:5010',
        terminal='main',
        token='agt_secret',
        ca_file=None,
        insecure=False,
        cps=10.0,
        delay_ms=None,
        jitter_ms=0,
        punctuation_pause_ms=0,
        progress=False,
    )
    result = typer.type_units(
        args,
        ['a', 'b', 'c'],
        post_json=fake_post,
        sleep=sleeps.append,
    )
    assert result['status'] == 'failed'
    assert result['error_code'] == 'agent_human_input_active'
    assert result['stopped_at_unit'] == 1
    assert result['sent_units'] == 1
    assert [call['payload']['data'] for call in fake_post.calls] == ['a', 'b']
    assert sleeps == [0.1]


def main():
    tests = [
        test_normalize_key_modes,
        test_send_worker_coalesces_pending_input_on_stop,
        test_send_worker_stops_on_fatal_error,
        test_send_worker_stops_on_not_attached_error,
        test_send_worker_drops_queued_input_after_fatal_error,
        test_send_worker_keeps_running_on_transient_human_lease,
        test_tail_worker_stops_on_not_attached_error,
        test_keepalive_worker_sends_hidden_state_heartbeat,
        test_keepalive_worker_stops_on_fatal_error,
        test_repl_startup_type_sends_units_through_shared_pacing,
        test_repl_startup_type_waits_for_quiet_screen_after_typing,
        test_repl_startup_type_stops_on_fatal_error,
        test_format_token_status_reports_idle_countdown,
        test_cli_and_repl_apply_handoff_defaults,
        test_cli_plain_send_payload_uses_structured_text,
        test_cli_screen_tail_lines_payload,
        test_cli_screen_region_payload,
        test_cli_screen_wait_payload,
        test_cli_tail_strip_ansi_payload,
        test_cli_wait_output_alias_maps_to_tail_payload,
        test_cli_wait_quiet_alias_maps_to_screen_payload,
        test_cli_send_capture_payload,
        test_cli_send_wait_payload_requests_capture,
        test_cli_send_wait_strip_ansi_payload_requests_plain_capture,
        test_cli_send_submit_after_payload_is_structured,
        test_cli_send_named_keys_payload_uses_structured_keys,
        test_cli_key_alias_maps_to_send_payload,
        test_cli_render_save_writes_png_and_redacts_base64,
        test_jsonl_client_reuses_defaults_and_preserves_ids,
        test_jsonl_client_reports_invalid_json_as_jsonl_error,
        test_jsonl_client_preserves_backend_failed_result,
        test_type_units_translate_newlines_and_preserve_unicode_characters,
        test_type_helper_defaults_to_generic_cadence_profile,
        test_type_helper_sends_one_unit_per_plain_send_without_capture,
        test_type_helper_stops_on_failed_send_without_replaying_remaining_units,
    ]
    for test in tests:
        test()
        print(f'{test.__name__}: ok')


if __name__ == '__main__':
    main()
