#!/usr/bin/env python3
"""Exercise translation review gates and generated-catalog checks in isolation."""

import csv
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / 'scripts' / 'build_ui_messages.py'
BUILD_CATALOG = runpy.run_path(str(BUILDER))['build_catalog']
FIELDS = ['key', 'current_en', 'en', 'zh-TW', 'context', 'placeholders', 'constraints', 'status', 'source']


def message(key='test.message', **changes):
    row = dict.fromkeys(FIELDS, '')
    row.update(key=key, en='Ready', context='Test fixture', status='source-approved')
    row.update(changes)
    return row


def write_table(path, rows):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)


class UiMessagesSmoke(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='standterm-ui-messages-')
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'messages.tsv'

    def catalog(self, rows):
        write_table(self.source, rows)
        return BUILD_CATALOG(self.source)

    def test_only_approved_sources_and_reviewed_translations_ship(self):
        rows = [message(f'test.{status.replace("-", "_")}', status=status, **{'zh-TW': 'Ready translation'})
                for status in ('proposed', 'retain', 'source-approved', 'translation-reviewed')]
        rows.append(message('test.removed', status='remove', en=''))
        self.assertEqual(self.catalog(rows), {
            'en': {'test.source_approved': 'Ready', 'test.translation_reviewed': 'Ready'},
            'zh-TW': {'test.translation_reviewed': 'Ready translation'},
        })
        self.assertEqual(self.catalog([message(status='translation-reviewed')])['zh-TW'], {})

    def test_placeholder_names_and_multiplicity_must_match(self):
        source = message(en='{host} connects to {host} using {port}',
                         placeholders='{host} {host} {port}', status='translation-reviewed',
                         **{'zh-TW': '{port}: {host} -> {host}'})
        self.assertEqual(self.catalog([source])['zh-TW']['test.message'], '{port}: {host} -> {host}')
        for changes in (
            {'en': '{host} using {port}'},
            {'zh-TW': '{host} using {port}'},
            {'zh-TW': '{host} {host} {hostname}'},
            {'zh-TW': '{host} {host} {port} {port}'},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'Placeholder mismatch'):
                self.catalog([{**source, **changes}])

    def test_invalid_rows_fail_instead_of_silently_shipping(self):
        cases = [
            ([message('Invalid.Key')], 'Invalid or duplicate key'),
            ([message(), message()], 'Invalid or duplicate key'),
            ([message(status='approved')], 'Unknown status'),
            ([message(en=' ')], 'Missing English source'),
            ([message(status='remove')], 'Removal row'),
            ([message(status='remove', en='', **{'zh-TW': 'Still present'})], 'Removal row'),
            ([message(context='First line\nSecond line')], 'Use escaped line breaks'),
            ([message(context='First\tSecond')], 'Use escaped line breaks'),
            ([message(status='proposed', en='Hello {name}')], 'Placeholder mismatch'),
        ]
        for rows, error in cases:
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, error):
                self.catalog(rows)

    def test_header_and_row_width_are_validated(self):
        self.source.write_text('key\ten\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unexpected translation table columns'):
            BUILD_CATALOG(self.source)
        for values in (list(message().values())[:-1], [*message().values(), 'extra']):
            self.source.write_text('\t'.join(FIELDS) + '\n' + '\t'.join(values) + '\n', encoding='utf-8')
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, 'Invalid columns'):
                BUILD_CATALOG(self.source)

    def test_escaped_line_breaks_and_utf8_bom(self):
        row = message(en='First\\nSecond')
        write_table(self.source, [row])
        self.source.write_bytes(b'\xef\xbb\xbf' + self.source.read_bytes())
        self.assertEqual(BUILD_CATALOG(self.source)['en']['test.message'], 'First\nSecond')

    def test_cli_check_detects_missing_and_stale_output_without_writing(self):
        root = Path(self.directory.name)
        script = root / 'scripts' / BUILDER.name
        source = root / 'docs' / 'ui_copy_review.tsv'
        output = root / 'static' / 'js' / 'standterm-messages.js'
        for path in (script, source, output):
            path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUILDER, script)
        write_table(source, [message()])

        def invoke(*args):
            return subprocess.run([sys.executable, str(script), *args], capture_output=True,
                                  text=True, timeout=10, check=False)

        missing = invoke('--check')
        self.assertEqual(missing.returncode, 1)
        self.assertIn('catalog is stale', missing.stderr)
        self.assertFalse(output.exists())
        generated = invoke()
        self.assertEqual(generated.returncode, 0, generated.stderr)
        original = output.read_bytes()
        original_mtime = output.stat().st_mtime_ns
        fresh = invoke('--check')
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(output.stat().st_mtime_ns, original_mtime)
        write_table(source, [message(en='Updated')])
        stale = invoke('--check')
        self.assertEqual(stale.returncode, 1)
        self.assertIn('catalog is stale', stale.stderr)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(output.stat().st_mtime_ns, original_mtime)
        regenerated = invoke()
        self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
        self.assertNotEqual(output.read_bytes(), original)
        self.assertEqual(invoke('--check').returncode, 0)


if __name__ == '__main__':
    unittest.main()
