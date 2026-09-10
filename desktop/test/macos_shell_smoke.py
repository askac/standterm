"""Verify Finder-style PATH handling with synthetic zsh profiles and commands."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Import Core with isolated state before constructing any shell configuration.
STATE = tempfile.TemporaryDirectory(prefix='standterm-macos-shell-')
os.environ.update({
    'STANDTERM_AGENT_RUNTIME_DIR': str(Path(STATE.name) / 'agent'),
    'STANDTERM_SESSION_RECOVERY_STORE': str(Path(STATE.name) / 'recovery.json'),
    'STANDTERM_DISABLE_AGENTINFO_CURRENT': '1',
})
import app as standterm


class MacShellTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'Native macOS zsh integration')
    def test_desktop_loads_login_profile_with_minimal_finder_path(self):
        with tempfile.TemporaryDirectory(prefix='standterm-zprofile-') as directory:
            root = Path(directory).resolve()
            binaries = root / 'bin'
            binaries.mkdir()
            tmux = binaries / 'tmux'
            tmux.write_text('#!/bin/sh\nprintf "standterm-synthetic-tmux\\n"\n')
            tmux.chmod(0o700)
            # Quoted to cover package/user paths with spaces without using the
            # operator's actual startup files or spawning a real tmux server.
            (root / '.zprofile').write_text(f'export PATH="{binaries}:$PATH"\n')
            (root / '.zshrc').write_text('export STANDTERM_TEST_INTERACTIVE=loaded\n')
            env = {**os.environ, 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                   'SHELL': '/bin/zsh', 'ZDOTDIR': str(root), 'STANDTERM_TEST_TMUX': str(tmux)}
            for desktop in [False, True]:
                with patch.dict(os.environ, env), patch.dict(standterm.app.config, {'DESKTOP_LOGIN_SHELL': desktop}):
                    config, error = standterm.get_default_local_shell_config()
                self.assertIsNone(error)
                self.assertEqual(config['shell_command'], ['/bin/zsh', '-l'] if desktop else ['/bin/zsh'])
                result = subprocess.run([*config['shell_command'], '-i', '-c',
                    'test "$STANDTERM_TEST_INTERACTIVE" = loaded && '
                    'test "$(command -v tmux)" = "$STANDTERM_TEST_TMUX" && command -v tmux && tmux'],
                    env=env, capture_output=True, text=True, timeout=10)
                if desktop:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), [str(tmux), 'standterm-synthetic-tmux'])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(str(tmux), result.stdout)
                    self.assertNotIn('standterm-synthetic-tmux', result.stdout)

    def test_login_flag_only_changes_known_macos_desktop_shells(self):
        for platform, shell, desktop, expected in [
            ('darwin', '/bin/zsh', True, ['/bin/zsh', '-l']),
            ('darwin', '/bin/zsh', False, ['/bin/zsh']),
            ('darwin', '/custom/shell-wrapper', True, ['/custom/shell-wrapper']),
            ('linux', '/bin/zsh', True, ['/bin/zsh']),
        ]:
            with patch.object(sys, 'platform', platform), patch.object(standterm, 'is_wsl', return_value=False), \
                    patch.dict(os.environ, {'SHELL': shell}), \
                    patch.dict(standterm.app.config, {'DESKTOP_LOGIN_SHELL': desktop}):
                config, error = standterm.get_default_local_shell_config()
                self.assertIsNone(error)
                self.assertEqual(config['shell_command'], expected)


if __name__ == '__main__':
    unittest.main()
