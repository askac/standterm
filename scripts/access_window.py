import argparse
import json
import os
import ssl
import sys
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from tkinter import messagebox, ttk

UI_STATE_STARTING = 'starting'
UI_STATE_CURRENT = 'current'
UI_STATE_STALE = 'stale'
UI_STATE_OFFLINE = 'offline'
UI_STATE_SHUTTING_DOWN = 'shutting_down'

ERROR_UNAVAILABLE = 'unavailable'
ERROR_UNAUTHORIZED = 'unauthorized'
ERROR_LOCAL_ONLY = 'local_only'
ERROR_UNREACHABLE = 'unreachable'
ERROR_INSTANCE_MISMATCH = 'instance_mismatch'
ERROR_HTTP = 'http'


def load_handoff_file(path):
    if not path:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return data if isinstance(data, dict) else {}


def load_handoff_stdin():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def urls_from_value(value):
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def apply_handoff(args, handoff):
    control = handoff.get('control') if isinstance(handoff.get('control'), dict) else {}
    args.url = handoff.get('url', '')
    args.token = handoff.get('token', '')
    args.instance_id = control.get('instance_id') or handoff.get('instance_id', '')
    args.status_urls = urls_from_value(control.get('status_urls')) or urls_from_value(handoff.get('status_urls'))
    args.shutdown_urls = urls_from_value(control.get('shutdown_urls')) or urls_from_value(handoff.get('shutdown_urls'))
    args.status_url = args.status_urls[0] if args.status_urls else handoff.get('status_url', '')
    args.shutdown_url = args.shutdown_urls[0] if args.shutdown_urls else handoff.get('shutdown_url', '')
    if not args.status_urls:
        args.status_urls = urls_from_value(args.status_url)
    if not args.shutdown_urls:
        args.shutdown_urls = urls_from_value(args.shutdown_url)
    args.shutdown_token = control.get('token') or handoff.get('shutdown_token', '')
    args.title = handoff.get('title', args.title)


def copy_to_clipboard(root, text, status_var, label, is_current=None):
    if is_current is not None and not is_current():
        status_var.set(f'{label} is unavailable until StandTerm status is current.')
        return
    if not text:
        status_var.set(f'{label} is unavailable.')
        return
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update_idletasks()
    status_var.set(f'Copied {label}.')


def open_browser(url, status_var, is_current=None):
    if is_current is not None and not is_current():
        status_var.set('Access URL is unavailable until StandTerm status is current.')
        return
    if not url:
        status_var.set('Access URL is unavailable.')
        return
    webbrowser.open(url, new=2)
    status_var.set('Opened browser.')


def request_standterm_shutdown(url, token):
    if not url or not token:
        return False, 'Shutdown is unavailable.'
    request = urllib.request.Request(
        url,
        data=b'',
        method='POST',
        headers={'X-StandTerm-Shutdown-Token': token},
    )
    context = ssl._create_unverified_context() if url.lower().startswith('https://') else None
    try:
        with urllib.request.urlopen(request, timeout=5, context=context) as response:
            if response.status < 200 or response.status >= 300:
                return False, f'Shutdown failed: HTTP {response.status}'
    except urllib.error.HTTPError as exc:
        return False, f'Shutdown failed: HTTP {exc.code}'
    except Exception as exc:
        return False, f'Shutdown failed: {exc}'
    return True, 'StandTerm shutdown requested.'


def request_standterm_shutdown_any(urls, token):
    last_message = 'Shutdown is unavailable.'
    for url in urls_from_value(urls):
        ok, message = request_standterm_shutdown(url, token)
        if ok:
            return True, message
        last_message = message
    return False, last_message


def make_launcher_error(kind, message, url='', http_status=None, error_code=''):
    return {
        'kind': kind,
        'message': message,
        'url': url,
        'http_status': http_status,
        'error_code': error_code,
    }


def launcher_error_kind(error):
    if isinstance(error, dict):
        return error.get('kind') or ERROR_UNAVAILABLE
    if error:
        return ERROR_UNREACHABLE
    return ''


def launcher_error_message(error):
    if isinstance(error, dict):
        message = error.get('message') or error.get('kind') or ERROR_UNAVAILABLE
        url = error.get('url') or ''
        return f'{url}: {message}' if url else message
    return str(error) if error else ''


def access_window_state_name(state):
    return state.get('name', UI_STATE_STARTING)


def access_window_is_current(state):
    return access_window_state_name(state) == UI_STATE_CURRENT


def access_window_can_shutdown(state):
    return access_window_state_name(state) == UI_STATE_CURRENT


def launcher_poll_state(error):
    if error is None:
        return UI_STATE_CURRENT
    if launcher_error_kind(error) in {ERROR_UNAUTHORIZED, ERROR_INSTANCE_MISMATCH}:
        return UI_STATE_STALE
    return UI_STATE_OFFLINE


def shutdown_standterm(root, urls, token, status_var, state=None, refresh_buttons=None):
    if state is not None and not access_window_can_shutdown(state):
        status_var.set(f'Shutdown is unavailable while StandTerm is {access_window_state_name(state)}.')
        return
    if not messagebox.askyesno('Shutdown StandTerm', 'Shutdown the StandTerm server?'):
        return
    if state is not None:
        state['name'] = UI_STATE_SHUTTING_DOWN
    if refresh_buttons is not None:
        refresh_buttons()
    ok, message = request_standterm_shutdown_any(urls, token)
    if not ok:
        if state is not None:
            state['name'] = UI_STATE_OFFLINE
        if refresh_buttons is not None:
            refresh_buttons()
        status_var.set(message)
        return
    status_var.set(message)
    root.after(750, root.destroy)


def fetch_launcher_json(url, token):
    if not url or not token:
        return None, make_launcher_error(ERROR_UNAVAILABLE, 'unavailable', url=url)
    request = urllib.request.Request(
        url,
        method='GET',
        headers={'X-StandTerm-Launcher-Token': token},
    )
    context = ssl._create_unverified_context() if url.lower().startswith('https://') else None
    try:
        with urllib.request.urlopen(request, timeout=3, context=context) as response:
            data = json.loads(response.read().decode('utf-8', errors='replace'))
            return data, None
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode('utf-8', errors='replace'))
        except Exception:
            payload = {}
        error_code = payload.get('error_code', '') if isinstance(payload, dict) else ''
        if error_code.endswith('_unauthorized'):
            kind = ERROR_UNAUTHORIZED
        elif error_code.endswith('_local_only'):
            kind = ERROR_LOCAL_ONLY
        else:
            kind = ERROR_HTTP
        message = f'HTTP {exc.code}'
        if error_code:
            message = f'{message} {error_code}'
        return None, make_launcher_error(kind, message, url=url, http_status=exc.code, error_code=error_code)
    except Exception as exc:
        return None, make_launcher_error(ERROR_UNREACHABLE, str(exc), url=url)


def fetch_launcher_json_any(urls, token, expected_instance_id=''):
    last_error = make_launcher_error(ERROR_UNAVAILABLE, 'unavailable')
    for url in urls_from_value(urls):
        data, error = fetch_launcher_json(url, token)
        if error:
            if launcher_error_kind(error) == ERROR_UNAUTHORIZED:
                return None, error
            last_error = error
            continue
        if expected_instance_id and data.get('instance_id') != expected_instance_id:
            return None, make_launcher_error(ERROR_INSTANCE_MISMATCH, 'instance mismatch', url=url)
        return data, None
    return None, last_error


def format_uptime(seconds):
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return 'unknown'
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}'
    return f'{minutes:02d}:{seconds:02d}'


def format_launcher_status(data, error=None, state=None):
    state_name = access_window_state_name(state or {})
    if state_name == UI_STATE_STARTING:
        return '\n'.join([
            'STANDTERM  checking',
            'status     waiting for launcher reply',
            'action     wait',
        ])
    if state_name == UI_STATE_SHUTTING_DOWN:
        return '\n'.join([
            'STANDTERM  shutting down',
            'status     shutdown requested',
            'action     wait',
        ])
    if state_name == UI_STATE_STALE:
        return '\n'.join([
            'STANDTERM  stale',
            f'error      {launcher_error_message(error) or "launcher token rejected"}',
            'action     close this old window',
        ])
    if error:
        return '\n'.join([
            'STANDTERM  offline',
            f'error      {launcher_error_message(error)}',
            'action     close this window and run the launcher',
        ])
    return '\n'.join([
        f'STANDTERM  {data.get("status", "unknown")}',
        f'runtime    {data.get("runtime", "unknown")}',
        f'uptime     {format_uptime(data.get("uptime_seconds"))}',
        f'sessions   {data.get("sessions", 0)}',
        f'sockets    {data.get("sockets", 0)}',
        f'terminals  {data.get("terminals", 0)}',
        f'listen     {data.get("bind_host", "?")}:{data.get("port", "?")}',
        f'https      {"on" if data.get("https") else "off"}',
    ])


def set_button_enabled(button, enabled):
    if enabled:
        button.state(['!disabled'])
    else:
        button.state(['disabled'])


def update_button_states(args, launcher_state, access_buttons, shutdown_button):
    current = access_window_is_current(launcher_state)
    for button in access_buttons:
        set_button_enabled(button, current)
    if shutdown_button is not None:
        set_button_enabled(shutdown_button, bool(args.shutdown_urls and args.shutdown_token and access_window_can_shutdown(launcher_state)))


def poll_launcher_status(root, args, status_text_var, launcher_state, access_buttons, shutdown_button):
    if access_window_state_name(launcher_state) == UI_STATE_SHUTTING_DOWN:
        return
    data, error = fetch_launcher_json_any(args.status_urls, args.shutdown_token, args.instance_id)
    launcher_state['name'] = launcher_poll_state(error)
    status_text_var.set(format_launcher_status(data or {}, error=error, state=launcher_state))
    update_button_states(args, launcher_state, access_buttons, shutdown_button)
    root.after(2000, lambda: poll_launcher_status(root, args, status_text_var, launcher_state, access_buttons, shutdown_button))


def build_window(args):
    root = tk.Tk()
    root.title(args.title)
    root.resizable(False, False)
    root.protocol('WM_DELETE_WINDOW', root.iconify)

    status_var = tk.StringVar(value='Ready.')
    launcher_state = {'name': UI_STATE_STARTING if args.status_urls and args.shutdown_token else UI_STATE_CURRENT}

    outer = ttk.Frame(root, padding=14)
    outer.grid(row=0, column=0, sticky='nsew')
    outer.columnconfigure(1, weight=1)

    ttk.Label(outer, text='StandTerm access', font=('', 12, 'bold')).grid(
        row=0,
        column=0,
        columnspan=3,
        sticky='w',
        pady=(0, 10),
    )

    ttk.Label(outer, text='Access URL and token are hidden. Use the buttons below.').grid(
        row=1,
        column=0,
        columnspan=3,
        sticky='w',
        pady=(0, 10),
    )

    status_text_var = tk.StringVar(value=format_launcher_status({}, state=launcher_state))
    status_box = tk.Label(
        outer,
        textvariable=status_text_var,
        anchor='w',
        justify='left',
        font=('TkFixedFont', 10),
        bg='#101214',
        fg='#9be58f',
        padx=10,
        pady=8,
        width=44,
    )
    status_box.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(0, 10))

    buttons = ttk.Frame(outer)
    buttons.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(0, 10))

    open_button = ttk.Button(
        buttons,
        text='Open Browser',
        command=lambda: open_browser(args.url, status_var, lambda: access_window_is_current(launcher_state)),
    )
    open_button.grid(row=0, column=0, padx=(0, 8))
    url_button = ttk.Button(
        buttons,
        text='Copy URL',
        command=lambda: copy_to_clipboard(root, args.url, status_var, 'Access URL', lambda: access_window_is_current(launcher_state)),
    )
    url_button.grid(row=0, column=1, padx=(0, 8))
    token_button = ttk.Button(
        buttons,
        text='Copy Token',
        command=lambda: copy_to_clipboard(root, args.token, status_var, 'Access Token', lambda: access_window_is_current(launcher_state)),
    )
    token_button.grid(row=0, column=2, padx=(0, 8))
    access_buttons = [open_button, url_button, token_button]

    shutdown_button = ttk.Button(
        buttons,
        text='Shutdown',
        command=lambda: shutdown_standterm(root, args.shutdown_urls, args.shutdown_token, status_var, launcher_state, refresh_buttons),
    )
    shutdown_button.grid(row=0, column=3, padx=(0, 8))
    if not args.shutdown_urls or not args.shutdown_token:
        shutdown_button.state(['disabled'])

    def refresh_buttons():
        update_button_states(args, launcher_state, access_buttons, shutdown_button)

    refresh_buttons()

    ttk.Label(outer, textvariable=status_var).grid(row=4, column=0, columnspan=3, sticky='w')

    root.after(100, root.lift)
    if args.status_urls and args.shutdown_token:
        root.after(
            100,
            lambda: poll_launcher_status(
                root,
                args,
                status_text_var,
                launcher_state,
                access_buttons,
                shutdown_button,
            ),
        )
    return root


def parse_args(argv):
    parser = argparse.ArgumentParser(description='Show StandTerm access helpers.')
    parser.add_argument('--handoff', default='')
    parser.add_argument('--stdin', action='store_true')
    parser.add_argument('--from-env', action='store_true')
    parser.add_argument('--url', default='')
    parser.add_argument('--token', default='')
    parser.add_argument('--instance-id', default='')
    parser.add_argument('--status-url', default='')
    parser.add_argument('--shutdown-url', default='')
    parser.add_argument('--shutdown-token', default='')
    parser.add_argument('--title', default='StandTerm Access')
    args = parser.parse_args(argv)
    args.status_urls = urls_from_value(args.status_url)
    args.shutdown_urls = urls_from_value(args.shutdown_url)
    if args.handoff:
        apply_handoff(args, load_handoff_file(args.handoff))
    if args.stdin:
        apply_handoff(args, load_handoff_stdin())
    if args.from_env:
        args.url = os.getenv('STANDTERM_ACCESS_WINDOW_URL', '')
        args.token = os.getenv('STANDTERM_ACCESS_WINDOW_TOKEN', '')
        args.instance_id = os.getenv('STANDTERM_ACCESS_WINDOW_INSTANCE_ID', '')
        args.status_url = os.getenv('STANDTERM_ACCESS_WINDOW_STATUS_URL', '')
        args.shutdown_url = os.getenv('STANDTERM_ACCESS_WINDOW_SHUTDOWN_URL', '')
        args.status_urls = urls_from_value(args.status_url)
        args.shutdown_urls = urls_from_value(args.shutdown_url)
        args.shutdown_token = os.getenv('STANDTERM_ACCESS_WINDOW_SHUTDOWN_TOKEN', '')
        args.title = os.getenv('STANDTERM_ACCESS_WINDOW_TITLE', args.title)
    if not args.url:
        parser.error('--url is required')
    if not args.token:
        parser.error('--token is required')
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = build_window(args)
    root.mainloop()


if __name__ == '__main__':
    main()
