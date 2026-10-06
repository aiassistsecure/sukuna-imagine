"""Corpus regressions. PostgreSQL execution checks run in CI."""
import os
import random
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg2
from pglast import parse_sql
from stealth.forge import template_candidates, materialise, _swap_db
from stealth.schemas import CATALOG, TRAIN_KEYS, HELDOUT_KEYS

FAMILIES = {
    'count_non_null', 'count_null', 'count_distinct', 'date_range',
    'date_count', 'date_filtered', 'join_count', 'join_count_full_row',
    'join_exists', 'join_absent',
}


class CorpusTests(unittest.TestCase):
    def test_training_split_and_determinism(self):
        self.assertFalse(set(TRAIN_KEYS) & set(HELDOUT_KEYS))
        self.assertIn('audit', TRAIN_KEYS)
        self.assertEqual(HELDOUT_KEYS, ('telemetry',))
        for key in TRAIN_KEYS:
            schema = CATALOG[key]
            schema.seed()
            a = template_candidates(schema, random.Random(1337))
            self.assertEqual(a, template_candidates(schema, random.Random(1337)))
            self.assertEqual(len(a), len({c.question for c in a}))
            for c in a:
                self.assertEqual(c.schema_key, key)
                self.assertEqual(len(parse_sql(c.sql)), 1)
                self.assertEqual(len(parse_sql(c.reference_sql)), 1)

    def test_all_declared_relationships_have_training_targets(self):
        for key in TRAIN_KEYS:
            schema = CATALOG[key]
            cands = template_candidates(schema, random.Random(0))
            for parent, child, fk in schema.relationships:
                counts = [c for c in cands if c.kind == 'join_count'
                          and f'each {parent} ID' in c.question
                          and f'related {child}' in c.question]
                self.assertEqual(len(counts), 2, (key, parent, child))
                self.assertTrue(all('(SELECT count(*)' in c.sql for c in counts))
            for c in cands:
                if c.kind == 'join_count_full_row':
                    self.assertIn(' row alongside ', c.question)
                    self.assertNotIn('l.*', c.sql)

    @unittest.skipUnless(os.environ.get('CORPUS_TEST_DSN'), 'CI PostgreSQL required')
    def test_pairs_and_counterexamples_on_postgres(self):
        admin = os.environ['CORPUS_TEST_DSN']
        for key in TRAIN_KEYS:
            schema = CATALOG[key]
            dbname = materialise(schema, admin)
            with psycopg2.connect(_swap_db(admin, dbname)) as con:
                with con.cursor() as cur:
                    for c in template_candidates(schema, random.Random(0)):
                        if c.kind not in FAMILIES:
                            continue
                        with self.subTest(schema=key, question=c.question):
                            cur.execute(c.sql)
                            actual = Counter(cur.fetchall())
                            cur.execute(c.reference_sql)
                            self.assertEqual(actual, Counter(cur.fetchall()))
                            if c.kind == 'join_count_full_row':
                                parent = next(t for t in schema.tables
                                              if f'each {t.name} row' in c.question)
                                self.assertEqual(len(cur.description), len(parent.columns) + 1)
        with psycopg2.connect(_swap_db(admin, 'stealth_audit')) as con:
            with con.cursor() as cur:
                probes = {
                    'SELECT count(*), count(category), count(DISTINCT category) FROM activities': [(8, 7, 2)],
                    "SELECT id FROM activities WHERE occurred_at >= '2025-01-01' AND occurred_at < '2026-01-01' ORDER BY id": [(3,), (4,)],
                    "SELECT id FROM activities WHERE occurred_at >= '2026-03-01' AND occurred_at < '2026-04-01' ORDER BY id": [(6,)],
                    'SELECT p.id, (SELECT count(*) FROM activities a WHERE a.project_id=p.id) FROM projects p ORDER BY p.id': [(1, 4), (2, 3), (3, 0)],
                }
                for sql, expected in probes.items():
                    cur.execute(sql)
                    self.assertEqual(cur.fetchall(), expected)
                cur.execute("SELECT count(*) FROM activities WHERE occurred_at > '2025-01-01'")
                self.assertNotEqual(cur.fetchone()[0], 2)
                cur.execute("SELECT count(*) FROM activities WHERE project_id=1 AND extract(year from occurred_at)=2026")
                filtered = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM activities WHERE extract(year from occurred_at)=2026")
                self.assertNotEqual(filtered, cur.fetchone()[0])


if __name__ == '__main__':
    unittest.main()
