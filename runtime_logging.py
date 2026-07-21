import sys
import threading
from datetime import datetime


_OUTPUT_LOCK = threading.Lock()


def log_message(message, *, file=None):
    output = sys.stdout if file is None else file
    timestamp = datetime.now().astimezone().isoformat(timespec='milliseconds')
    lines = str(message).splitlines() or ['']
    with _OUTPUT_LOCK:
        for line in lines:
            print(f'[{timestamp}] {line}', file=output, flush=True)
