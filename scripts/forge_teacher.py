#!/usr/bin/env python3
"""Forge a SQL corpus with an optional large-model teacher.

The teacher proposes alternate SQL for deterministic questions. PostgreSQL L4
remains the authority: a teacher proposal is admitted only if it produces the
same answer as the reference query on real data.
"""
from __future__ import annotations
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stealth.evaluate import hf_generator
from stealth.forge import forge

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teacher", default="mistralai/Devstral-Small-2507")
    ap.add_argument("--dsn", default=os.environ.get(
        "STEALTH_ADMIN_DSN",
        "host=/agent/workspace/pgrun user=stealth dbname=postgres"))
    ap.add_argument("--out", default="corpus/sql_train.jsonl")
    ap.add_argument("--rejects", default="corpus/sql_rejects.jsonl")
    ap.add_argument("--teacher-per-schema", type=int, default=32)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1337)
    a = ap.parse_args()

    print(f"* loading teacher {a.teacher}")
    generate = hf_generator(a.teacher)
    stats = forge(
        a.dsn, a.out, seed=a.seed, workers=a.workers or None,
        rejects_path=a.rejects, teacher_generate=generate,
        teacher_per_schema=a.teacher_per_schema,
    )
    print("\n" + json.dumps(stats, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
