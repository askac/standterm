"""A process-scoped RGB hint without changing TERM or installed terminfo."""

import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


TERMINFO_TOOL_TIMEOUT = 2


@functools.lru_cache(maxsize=4)
def _rgb_terminfo(term, search_path, home):
    infocmp = shutil.which('infocmp', path=search_path)
    tic = shutil.which('tic', path=search_path)
    if not infocmp or not tic:
        return None
    environment = dict(os.environ, HOME=home, PATH=search_path)
    environment.pop('TERMINFO', None)
    environment.pop('TERMINFO_DIRS', None)
    directory = None
    try:
        def run(command, **kwargs):
            return subprocess.run(command, env=environment, text=True, capture_output=True,
                                  check=True, timeout=TERMINFO_TOOL_TIMEOUT, **kwargs).stdout

        source = run([infocmp, '-x', '-1', term])
        if re.search(r'^\s+(?:RGB|Tc)[,=#]', source, re.MULTILINE):
            return None
        lines = source.splitlines(keepends=True)
        header = next(index for index, line in enumerate(lines) if line and not line.startswith('#'))
        lines.insert(header + 1, '\tRGB, Tc,\n')
        # Keep this private directory after Core exits: detached children may still use it.
        directory = tempfile.mkdtemp(prefix='standterm-terminfo-')
        source_path = Path(directory) / 'source.ti'
        source_path.write_text(''.join(lines), encoding='utf-8')
        run([tic, '-x', '-o', directory, str(source_path)])
        compiled = run([infocmp, '-A', directory, '-x', '-1', term])
        if not re.search(r'^\s+RGB,', compiled, re.MULTILINE):
            raise ValueError('Compiled terminfo does not advertise RGB.')
        return directory
    except (OSError, subprocess.SubprocessError, ValueError, StopIteration):
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        return None


def add_local_rgb_terminfo(environment):
    if (sys.platform.startswith('win') or environment.get('TERM') != 'xterm-256color'
            or 'TERMINFO' in environment or 'TERMINFO_DIRS' in environment):
        return
    directory = _rgb_terminfo(environment['TERM'], environment.get('PATH', os.defpath),
                             environment.get('HOME', str(Path.home())))
    if directory:
        environment['TERMINFO'] = directory
