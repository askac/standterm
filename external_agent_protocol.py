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


def parse_external_agent_screen_options(
    command,
    invalid_data_error_code=DEFAULT_INVALID_DATA_ERROR_CODE,
):
    has_tail_lines = command.get('tail_lines') is not None
    has_region = command.get('region') is not None
    if has_tail_lines and has_region:
        return None, invalid_data_error_code

    if has_tail_lines:
        try:
            tail_lines = int(command.get('tail_lines'))
        except (TypeError, ValueError):
            return None, invalid_data_error_code
        if tail_lines < 0:
            return None, invalid_data_error_code
        return {'mode': 'tail_lines', 'tail_lines': tail_lines}, None

    if has_region:
        region = command.get('region')
        if not isinstance(region, dict):
            return None, invalid_data_error_code
        try:
            top = int(region.get('top'))
            bottom = int(region.get('bottom'))
        except (TypeError, ValueError):
            return None, invalid_data_error_code
        if top < 0 or bottom < top:
            return None, invalid_data_error_code
        return {'mode': 'region', 'top': top, 'bottom': bottom}, None

    return {'mode': 'full'}, None


def apply_external_agent_screen_options(screen, options):
    if not isinstance(screen, dict) or options.get('mode') == 'full':
        return screen
    lines = list(screen.get('lines') or [])
    original_line_count = len(lines)
    if options.get('mode') == 'tail_lines':
        tail_lines = options['tail_lines']
        start = max(0, original_line_count - tail_lines)
        end = original_line_count
        selected = lines[start:end]
        region = {
            'top': start,
            'bottom': end,
            'tail_lines': tail_lines,
        }
    else:
        start = min(options['top'], original_line_count)
        end = min(options['bottom'], original_line_count)
        selected = lines[start:end]
        region = {
            'top': start,
            'bottom': end,
        }
    sliced = dict(screen)
    sliced['lines'] = selected
    sliced['line_count'] = len(selected)
    sliced['original_line_count'] = original_line_count
    sliced['region'] = region
    sliced['truncated'] = len(selected) != original_line_count
    return sliced


def parse_external_agent_tail_wait_ms(value, max_wait_ms):
    try:
        wait_ms = int(value if value is not None else 0)
    except (TypeError, ValueError):
        wait_ms = 0
    return max(0, min(wait_ms, max_wait_ms))


def parse_external_agent_screen_quiet_ms(value, max_quiet_ms):
    try:
        quiet_ms = int(value if value is not None else 0)
    except (TypeError, ValueError):
        quiet_ms = 0
    return max(0, min(quiet_ms, max_quiet_ms))


def parse_external_agent_send_capture_wait_ms(
    value,
    default_wait_ms,
    max_wait_ms,
):
    try:
        wait_ms = int(value if value is not None else default_wait_ms)
    except (TypeError, ValueError):
        wait_ms = default_wait_ms
    return max(0, min(wait_ms, max_wait_ms))


def parse_external_agent_send_capture_settle_ms(
    value,
    default_settle_ms,
    max_settle_ms,
):
    try:
        settle_ms = int(value if value is not None else default_settle_ms)
    except (TypeError, ValueError):
        settle_ms = default_settle_ms
    return max(0, min(settle_ms, max_settle_ms))


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


def parse_external_agent_sequence_steps(
    command,
    max_steps,
    allowed_ops,
    invalid_data_error_code=DEFAULT_INVALID_DATA_ERROR_CODE,
):
    steps = command.get('steps')
    if not isinstance(steps, list) or not steps or len(steps) > max_steps:
        return None, invalid_data_error_code
    parsed_steps = []
    for step in steps:
        if not isinstance(step, dict):
            return None, invalid_data_error_code
        step_op = step.get('op')
        if not isinstance(step_op, str):
            return None, invalid_data_error_code
        step_op = step_op.strip().lower()
        if step_op not in allowed_ops:
            return None, invalid_data_error_code
        if 'token' in step or 'terminal_id' in step:
            return None, invalid_data_error_code
        parsed_step = dict(step)
        parsed_step['op'] = step_op
        parsed_steps.append(parsed_step)
    return parsed_steps, None
