DEFAULT_INVALID_DATA_ERROR_CODE = 'agent_action_invalid_data'

EXTERNAL_AGENT_KEY_INPUTS = {
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

EXTERNAL_AGENT_SEND_INPUT_KINDS = {'legacy_data', 'text', 'keys'}


def external_agent_flag_enabled(value):
    return value is True or value == 1


def should_external_agent_submit_after(command):
    return external_agent_flag_enabled(command.get('submit_after')) \
        or external_agent_flag_enabled(command.get('submit'))


def should_external_agent_capture_send(command):
    return external_agent_flag_enabled(command.get('capture'))


def should_external_agent_strip_ansi(command):
    return external_agent_flag_enabled(command.get('strip_ansi'))


def normalize_external_agent_key_names(value):
    if isinstance(value, str):
        keys = [value]
    elif isinstance(value, list):
        keys = value
    else:
        return None
    if not keys:
        return None
    normalized = []
    for key in keys:
        if not isinstance(key, str) or key not in EXTERNAL_AGENT_KEY_INPUTS:
            return None
        normalized.append(key)
    return normalized


def parse_external_agent_structured_input(
    input_payload,
    invalid_data_error_code=DEFAULT_INVALID_DATA_ERROR_CODE,
):
    if not isinstance(input_payload, dict):
        return None, None, invalid_data_error_code
    kind = input_payload.get('kind')
    if not isinstance(kind, str):
        return None, None, invalid_data_error_code
    kind = kind.strip().lower()
    if kind == 'text':
        text = input_payload.get('text')
        if not isinstance(text, str):
            return None, None, invalid_data_error_code
        return text, {'input_kind': 'text'}, None
    if kind == 'keys':
        keys = normalize_external_agent_key_names(input_payload.get('keys'))
        if not keys:
            return None, None, invalid_data_error_code
        return ''.join(EXTERNAL_AGENT_KEY_INPUTS[key] for key in keys), {
            'input_kind': 'keys',
            'key_names': keys,
            'key_count': len(keys),
        }, None
    return None, None, invalid_data_error_code


def parse_external_agent_send_input(
    command,
    invalid_data_error_code=DEFAULT_INVALID_DATA_ERROR_CODE,
):
    has_input = command.get('input') is not None
    has_kind = command.get('kind') is not None
    has_data = command.get('data') is not None
    if sum(1 for value in (has_input, has_kind, has_data) if value) != 1:
        return None, None, invalid_data_error_code
    if has_input:
        return parse_external_agent_structured_input(
            command.get('input'),
            invalid_data_error_code=invalid_data_error_code,
        )
    if has_kind:
        input_payload = {
            'kind': command.get('kind'),
            'text': command.get('text'),
            'keys': command.get('keys'),
        }
        return parse_external_agent_structured_input(
            input_payload,
            invalid_data_error_code=invalid_data_error_code,
        )
    data = command.get('data')
    if not isinstance(data, str):
        return None, None, invalid_data_error_code
    return data, {'input_kind': 'legacy_data'}, None


def parse_external_agent_wait_condition(command):
    condition = command.get('condition')
    if condition is None:
        condition = command.get('kind')
    if condition is None:
        condition = command.get('wait_for')
    if not isinstance(condition, str):
        return None
    condition = condition.strip().lower().replace('-', '_')
    if condition in {'output', 'quiet'}:
        return condition
    return None
