#!/usr/bin/env python3
"""Emit corpus/eval.jsonl — {schema_key, question, reference_sql} rows.

Defaults to the HELD-OUT schemas, because generalisation is the thing worth
measuring. Pass --train-schemas to also score the ones the model saw; the gap
between the two numbers IS the memorisation gap.
"""
import argparse, json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from stealth.forge import template_candidates
from stealth.schemas import CATALOG, HELDOUT_KEYS, TRAIN_KEYS

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="corpus/eval.jsonl")
ap.add_argument("--train-schemas", action="store_true")
ap.add_argument("--per-schema", type=int, default=40)
ap.add_argument("--seed", type=int, default=99)
a = ap.parse_args()

keys = list(HELDOUT_KEYS) + (list(TRAIN_KEYS) if a.train_schemas else [])
rng = random.Random(a.seed)
rows = []
for k in keys:
    cands = [c for c in template_candidates(CATALOG[k], rng) if c.reference_sql]
    for c in cands[:a.per_schema]:
        rows.append({"schema_key": k, "question": c.question,
                     "reference_sql": c.reference_sql,
                     "kind": c.kind, "difficulty": c.difficulty})
os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
with open(a.out, "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
print(f"wrote {len(rows)} eval rows across {keys} -> {a.out}")
