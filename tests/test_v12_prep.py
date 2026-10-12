"""Dependency-free v12 preparation regressions; no model or database required."""
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.merge_v12_corpus import balanced, load
from scripts.make_protocol import clean_sql_block, SYSTEM, PREFIXES


def row(tag):
    return {'messages': [{'role': 'user', 'content': tag},
                         {'role': 'assistant', 'content': 'answer'}],
            'meta': {'kind': tag}}


class V12Tests(unittest.TestCase):
    def test_exact_mix_retains_inputs_and_is_reproducible(self):
        pools = {k: [row(f'{k}-{i}') for i in range(n)]
                 for k, n in [('identity', 160), ('protocol', 900), ('sql', 200)]}
        merged, report = balanced(pools, 42)
        counts = Counter(r['meta']['kind'].split('-')[0] for r in merged)
        self.assertEqual(counts, {'identity': 1485, 'protocol': 900, 'sql': 2115})
        for pool in pools.values():
            self.assertTrue(all(r in merged for r in pool))
        self.assertEqual((merged, report), balanced(pools, 42))
        self.assertNotEqual(merged, balanced(pools, 43)[0])

    def test_explicit_total_reports_omissions(self):
        pools = {k: [row(f'{k}-{i}') for i in range(60)]
                 for k in ('identity', 'protocol', 'sql')}
        merged, report = balanced(pools, 42, 100)
        self.assertEqual(len(merged), 100)
        self.assertEqual(report['components']['identity']['omitted_rows'], 27)
        for invalid in (0, -100, 101):
            with self.assertRaises(ValueError):
                balanced(pools, 42, invalid)

    def test_protocol_accepts_writes_but_rejects_bad_envelopes(self):
        for sql in ('SELECT 1;', 'INSERT INTO t VALUES (1);',
                    'UPDATE t SET x=1;', 'DELETE FROM t WHERE x=1;'):
            self.assertTrue(clean_sql_block(f'<<<SQL>>>\n{sql}\n<<<END>>>\n'))
        for bad in ('<<<SQL>>> <<<END>>>', '```sql\n<<<SQL>>>SELECT 1<<<END>>>\n```',
                    '<<<SQL>>>SELECT 1<<<END>>>UNANSWERABLE',
                    '<<<SQL>>>SELECT 1<<<END>>><<<SQL>>>SELECT 2<<<END>>>'):
            self.assertFalse(clean_sql_block(bad))
        self.assertNotIn('read-only', SYSTEM + ' '.join(PREFIXES))

    def test_cli_creates_directory_manifest_and_keeps_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for k in ('identity', 'protocol', 'sql'):
                p = root / f'{k}.jsonl'
                p.write_text(json.dumps(row(k)) + '\n', encoding='utf-8')
                paths.extend(['--' + k, str(p)])
            out = root / 'nested' / 'train.jsonl'
            cmd = [sys.executable, str(ROOT / 'scripts/merge_v12_corpus.py'), *paths]
            result = subprocess.run([*cmd, '--out', str(out)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(load(str(out))), 100)
            self.assertEqual(json.loads(Path(str(out) + '.mix.json').read_text())['basis'], 'rows')
            result = subprocess.run([*cmd, '--out', paths[1]], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(load(paths[1])), 1)

    def test_invalid_and_empty_input_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'bad.jsonl'
            for text in ('', '{}\n', json.dumps({'messages': [{'role': 'user', 'content': 'hi'}]})):
                p.write_text(text)
                with self.assertRaises(ValueError):
                    load(str(p))


if __name__ == '__main__':
    unittest.main()
