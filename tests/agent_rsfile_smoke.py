import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import agent_rsfile


def test_b64url_json_roundtrip():
    payload = {
        'cmd': 'read_chunk',
        'offset': 0,
        'data_b64url': agent_rsfile.b64url_encode(b'\x00hello\xff'),
    }
    encoded = agent_rsfile.b64url_json(payload)
    decoded = agent_rsfile.json_from_b64url(encoded)
    assert decoded == payload


def test_parse_frame_line_accepts_strict_marker():
    payload = agent_rsfile.b64url_json({'cmd': 'probe'})
    frame = agent_rsfile.parse_frame_line(f'STFT1:abc_123:7:OK:{payload}')
    assert frame['status'] == 'ok'
    assert frame['nonce'] == 'abc_123'
    assert frame['seq'] == 7
    assert frame['kind'] == 'OK'
    assert frame['payload'] == {'cmd': 'probe'}


def test_parse_frame_line_rejects_non_frame_and_oversize():
    assert agent_rsfile.parse_frame_line('echo STFT1:abc:1:OK:bad') is None
    assert agent_rsfile.parse_frame_line('x' * 4000, max_marker_bytes=100) is None


def test_render_command_template_rejects_unknown_placeholder():
    try:
        agent_rsfile.render_command_template('echo {unknown}', {'nonce': 'n'})
    except SystemExit as exc:
        assert 'unknown command template placeholder' in str(exc)
    else:
        raise AssertionError('unknown placeholder was accepted')


def test_render_command_template_rejects_unsafe_value():
    try:
        agent_rsfile.render_command_template('echo {nonce}', {'nonce': 'bad value'})
    except SystemExit as exc:
        assert 'unsafe template value' in str(exc)
    else:
        raise AssertionError('unsafe value was accepted')


def test_builtin_methods_cover_common_targets():
    names = set(agent_rsfile.BUILTIN_METHODS)
    assert 'builtin:macos-zsh-python3' in names
    assert 'builtin:linux-sh-python3' in names
    assert 'builtin:windows-powershell' in names
    assert 'builtin:freebsd-tcsh-python3' in names
    assert 'builtin:freebsd-tcsh-python3.11' in names
    assert 'builtin:freebsd-tcsh-python-auto' in names
    assert agent_rsfile.resolve_method('py3-tcsh')[0]['name'] == 'builtin:freebsd-tcsh-python3'
    assert agent_rsfile.resolve_method('py3-sh')[0]['name'] == 'builtin:linux-sh-python3'


def test_builtin_commands_use_encoded_remote_path():
    remote_path = '/tmp/path with spaces/quote\'"file.bin'
    for name in agent_rsfile.BUILTIN_METHODS:
        method, _pack_hash = agent_rsfile.resolve_method(name)
        if method.get('auto_probe'):
            continue
        values = agent_rsfile.base_values(
            'nonce123',
            1,
            remote_path,
            offset=0,
            length=32,
            chunk_b64url='QUJD',
            expected_size=3,
            expected_sha256='a' * 64,
            overwrite_flag='0',
        )
        command = agent_rsfile.rendered_command(method, 'write_chunk', values)
        assert remote_path not in command
        assert agent_rsfile.path_to_b64url(remote_path) in command
        assert len(command.encode('utf-8')) <= method['max_command_bytes']


def test_builtin_command_templates_render_under_byte_caps():
    remote_path = '/tmp/rsfile-test.bin'
    for name in agent_rsfile.BUILTIN_METHODS:
        method, _pack_hash = agent_rsfile.resolve_method(name)
        if method.get('auto_probe'):
            continue
        values = agent_rsfile.base_values(
            'nonce123',
            1,
            remote_path,
            offset=0,
            length=32,
            chunk_b64url='QUJD',
            expected_size=3,
            expected_sha256='a' * 64,
            overwrite_flag='0',
        )
        for command_name in method['commands']:
            command = agent_rsfile.rendered_command(method, command_name, values)
            assert len(command.encode('utf-8')) <= method['max_command_bytes']
        assert agent_rsfile.max_put_chunk_chars(method, remote_path, 'nonce123', 1, '0') >= 512


def test_method_pack_requires_trust():
    with tempfile.TemporaryDirectory() as temp_dir:
        pack = Path(temp_dir) / 'method.json'
        pack.write_text(json.dumps(agent_rsfile.BUILTIN_METHODS['builtin:linux-sh-python3']), encoding='utf-8')
        try:
            agent_rsfile.resolve_method('builtin:linux-sh-python3', method_pack=str(pack), trust_pack=False)
        except SystemExit as exc:
            assert '--trust-pack' in str(exc)
        else:
            raise AssertionError('method pack loaded without trust')


def main():
    tests = [
        test_b64url_json_roundtrip,
        test_parse_frame_line_accepts_strict_marker,
        test_parse_frame_line_rejects_non_frame_and_oversize,
        test_render_command_template_rejects_unknown_placeholder,
        test_render_command_template_rejects_unsafe_value,
        test_builtin_methods_cover_common_targets,
        test_builtin_commands_use_encoded_remote_path,
        test_builtin_command_templates_render_under_byte_caps,
        test_method_pack_requires_trust,
    ]
    for test in tests:
        test()
        print(f'{test.__name__}: ok')


if __name__ == '__main__':
    main()
