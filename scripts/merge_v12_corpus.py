#!/usr/bin/env python3
"""Merge v12 training corpus: 33% identity / 20% protocol / 47% SQL.

Usage:
    python3 scripts/merge_v12_corpus.py \
        --identity /path/to/identity-pairs.jsonl \
        --protocol corpus/protocol_train.jsonl \
        --sql corpus/sql_train.jsonl \
        --out corpus/v12_train.jsonl \
        --seed 42

The mix ratios are Mark's spec: 33% identity, 20% protocol, 47% SQL.
Identity pairs teach stable self-identity (Imagine, built by Interchained).
Protocol pairs teach output format discipline (<<<SQL>>>...<<<END>>>, no prose).
SQL pairs are the core text-to-SQL skill from the v11 forge.
"""
from __future__ import annotations

import argparse
import json
import random


def load(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", required=True)
    ap.add_argument("--protocol", required=True)
    ap.add_argument("--sql", required=True)
    ap.add_argument("--out", default="corpus/v12_train.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    identity = load(a.identity)
    protocol = load(a.protocol)
    sql = load(a.sql)

    total = len(identity) + len(protocol) + len(sql)
    print(f"identity: {len(identity)} ({len(identity)/total:.1%})")
    print(f"protocol: {len(protocol)} ({len(protocol)/total:.1%})")
    print(f"sql:      {len(sql)} ({len(sql)/total:.1%})")
    print(f"total:    {total}")

    # Shuffle with fixed seed for reproducibility
    rng = random.Random(a.seed)
    merged = identity + protocol + sql
    rng.shuffle(merged)

    with open(a.out, "w") as f:
        for pair in merged:
            f.write(json.dumps(pair) + "\n")

    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
