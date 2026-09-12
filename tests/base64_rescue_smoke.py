import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECODER = ROOT / 'scripts' / 'base64d.sh'
PROBE = ROOT / 'scripts' / 'base64d_probe.sh'
SHELLS = [[path] for name in ('dash', 'bash', 'ksh') if (path := shutil.which(name))]
if shutil.which('busybox'):
    SHELLS.append([shutil.which('busybox'), 'sh'])


@unittest.skipUnless(SHELLS, 'A supported POSIX shell is required.')
class Base64RescueTests(unittest.TestCase):
    def test_binary_round_trips_across_shells_and_output_batches(self):
        vectors = [b'', b'f', b'fo', b'foo', bytes(range(256))]
        vectors.extend(bytes(index % 256 for index in range(size)) for size in (767, 768, 769, 4096))
        for shell in SHELLS:
            for raw in vectors:
                with self.subTest(shell=shell, size=len(raw)):
                    encoded = base64.encodebytes(raw).decode('ascii').replace('\n', '\r\n\t ')
                    result = subprocess.run(shell + [str(DECODER), encoded], capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, raw)

    def test_invalid_inputs_emit_no_partial_bytes(self):
        invalid = ['A', 'AAA', 'A===', '====', 'AA=A', 'Zg=a', 'Zh==', 'Zm9=', 'Zg==AAAA',
                   'Zm!v', 'AA==\nAA==', 'A' * 4097, 'AAAA\n' * 300 + '!AAA']
        for shell in SHELLS:
            for encoded in invalid:
                with self.subTest(shell=shell, size=len(encoded)):
                    result = subprocess.run(shell + [str(DECODER), encoded], capture_output=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, b'')
                    self.assertIn(b'base64d.sh:', result.stderr)
            result = subprocess.run(shell + [str(DECODER)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 2)

    def test_probe_cleans_up_and_refuses_existing_temp_directory(self):
        for shell in SHELLS:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory(prefix='standterm-probe-test-') as directory:
                env = dict(os.environ, TMPDIR=directory)
                result = subprocess.run(shell + [str(PROBE)], env=env, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b'PASS  printf %b with octal', result.stdout)
                self.assertEqual(list(Path(directory).iterdir()), [])
                wrapper = ('mkdir "$TMPDIR/standterm-base64d-probe-$$"; '
                           'printf sentinel > "$TMPDIR/standterm-base64d-probe-$$/bytes.bin"; '
                           'exec "$@"')
                result = subprocess.run(shell + ['-c', wrapper, 'probe-collision'] + shell + [str(PROBE)],
                                        env=env, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'cannot create a private temporary directory', result.stderr)
                files = list(Path(directory).glob('*/bytes.bin'))
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].read_bytes(), b'sentinel')


if __name__ == '__main__':
    unittest.main()
