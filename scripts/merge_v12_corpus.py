#!/usr/bin/env python3
"""Merge v12 training corpus: enforce 33% identity / 20% protocol / 47% SQL row shares.

Unlike naive concatenation, this enforces exact row shares deterministically:
- Chooses the smallest total divisible by 100 that retains every input row
  in its component quota (33/20/47).
- Smaller pools repeat in shuffled complete cycles to fill their quota.
- Emits a .mix.json report with repetitions, omissions, per-kind counts,
  and system-free identity coverage.
- Validates chat structure: every record must have nonempty messages ending
  in an assistant target.
- Refuses to overwrite any input file.

Usage:
    python3 scripts/merge_v12_corpus.py \
        --identity corpus/identity_train.jsonl \
        --protocol corpus/protocol_train.jsonl \
        --sql corpus/sql_train.jsonl \
        --out corpus/v12_train.jsonl \
        --seed 42
    # --total N forces a specific total (must be a positive multiple of 100);
    # may omit rows from larger pools.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys

IDENTITY_SHARE = 0.33
PROTOCOL_SHARE = 0.20
SQL_SHARE = 0.47


def load(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def get_messages(rec: dict) -> list[dict] | None:
    """Return the chat messages for a record, normalizing prompt/response format."""
    if "messages" in rec:
        return rec["messages"]
    if "prompt" in rec and "response" in rec:
        # prompt/response pairs have no system message (unprompted identity)
        return [
            {"role": "user", "content": rec["prompt"]},
            {"role": "assistant", "content": rec["response"]},
        ]
    return None


def validate_record(rec: dict, path: str, idx: int) -> None:
    msgs = get_messages(rec)
    if not msgs:
        raise ValueError(f"{path}:{idx}: no messages or prompt/response found")
    if not msgs:
        raise ValueError(f"{path}:{idx}: empty messages")
    for m in msgs:
        if not m.get("content", "").strip():
            raise ValueError(f"{path}:{idx}: empty content in {m.get('role')} message")
    if msgs[-1].get("role") != "assistant":
        raise ValueError(f"{path}:{idx}: last message is not assistant")
    if not msgs[-1].get("content", "").strip():
        raise ValueError(f"{path}:{idx}: empty assistant target")


def has_system(rec: dict) -> bool:
    msgs = get_messages(rec) or []
    return any(m.get("role") == "system" for m in msgs)


def smallest_total(n_identity: int, n_protocol: int, n_sql: int) -> int:
    """Smallest total divisible by 100 retaining every input row in its quota."""
    # Each pool's quota must be >= its row count:
    #   total * share >= n  →  total >= n / share
    needed = max(
        math.ceil(n_identity / IDENTITY_SHARE),
        math.ceil(n_protocol / PROTOCOL_SHARE),
        math.ceil(n_sql / SQL_SHARE),
    )
    # Round up to next multiple of 100
    return math.ceil(needed / 100) * 100


def fill_pool(rows: list[dict], quota: int, rng: random.Random) -> tuple[list[dict], int]:
    """Fill quota from rows, repeating in shuffled complete cycles. Returns (filled, repetitions)."""
    if not rows:
        return [], 0
    filled: list[dict] = []
    repetitions = 0
    pool = rows[:]
    while len(filled) < quota:
        rng.shuffle(pool)
        take = min(len(pool), quota - len(filled))
        filled.extend(pool[:take])
        if len(filled) < quota:
            repetitions += 1  # completed a full cycle, starting another
            pool = rows[:]
    return filled, repetitions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", required=True)
    ap.add_argument("--protocol", required=True)
    ap.add_argument("--sql", required=True)
    ap.add_argument("--out", default="corpus/v12_train.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--total", type=int, default=0,
                    help="Force total rows (positive multiple of 100); may omit rows.")
    a = ap.parse_args()

    # Prevent input overwrite
    out_abs = os.path.abspath(a.out)
    for p in (a.identity, a.protocol, a.sql):
        if os.path.abspath(p) == out_abs:
            print(f"refusing to overwrite input: {p}", file=sys.stderr)
            return 2

    identity = load(a.identity)
    protocol = load(a.protocol)
    sql = load(a.sql)

    # Validate chat structure
    for idx, rec in enumerate(identity):
        validate_record(rec, a.identity, idx)
    for idx, rec in enumerate(protocol):
        validate_record(rec, a.protocol, idx)
    for idx, rec in enumerate(sql):
        validate_record(rec, a.sql, idx)

    if a.total:
        if a.total <= 0 or a.total % 100 != 0:
            print("--total must be a positive multiple of 100", file=sys.stderr)
            return 2
        total = a.total
    else:
        total = smallest_total(len(identity), len(protocol), len(sql))

    q_identity = round(total * IDENTITY_SHARE)
    q_protocol = round(total * PROTOCOL_SHARE)
    q_sql = total - q_identity - q_protocol  # remainder to SQL, keeps exact total

    rng = random.Random(a.seed)
    rng.shuffle(identity)
    rng.shuffle(protocol)
    rng.shuffle(sql)

    # Fill quotas; smaller pools cycle, larger pools may be truncated if --total forced
    filled_id, rep_id = fill_pool(identity, q_identity, rng)
    filled_pr, rep_pr = fill_pool(protocol, q_protocol, rng)
    filled_sq, rep_sq = fill_pool(sql, q_sql, rng)

    # Track omissions (only possible with explicit --total)
    used_id = set(id(r) for r in filled_id)
    used_pr = set(id(r) for r in filled_pr)
    used_sq = set(id(r) for r in filled_sq)
    omit_id = len(identity) - len(used_id)
    omit_pr = len(protocol) - len(used_pr)
    omit_sq = len(sql) - len(used_sq)

    # System-free identity coverage (unprompted identity = no system message)
    sysfree_id = sum(1 for r in filled_id if not has_system(r))

    merged = filled_id + filled_pr + filled_sq
    rng.shuffle(merged)

    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    with open(out_abs, "w") as f:
        for rec in merged:
            f.write(json.dumps(rec) + "\n")

    report = {
        "total": len(merged),
        "seed": a.seed,
        "quotas": {"identity": q_identity, "protocol": q_protocol, "sql": q_sql},
        "shares": {
            "identity": len(filled_id) / len(merged),
            "protocol": len(filled_pr) / len(merged),
            "sql": len(filled_sq) / len(merged),
        },
        "input_rows": {
            "identity": len(identity),
            "protocol": len(protocol),
            "sql": len(sql),
        },
        "repetitions": {"identity": rep_id, "protocol": rep_pr, "sql": rep_sq},
        "omissions": {"identity": omit_id, "protocol": omit_pr, "sql": omit_sq},
        "system_free_identity": sysfree_id,
        "system_free_identity_share": sysfree_id / len(filled_id) if filled_id else 0,
    }
    report_path = out_abs + ".mix.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"identity: {len(filled_id)} ({len(filled_id)/len(merged):.1%}) "
          f"[input {len(identity)}, cycles +{rep_id}, omitted {omit_id}]")
    print(f"protocol: {len(filled_pr)} ({len(filled_pr)/len(merged):.1%}) "
          f"[input {len(protocol)}, cycles +{rep_pr}, omitted {omit_pr}]")
    print(f"sql:      {len(filled_sq)} ({len(filled_sq)/len(merged):.1%}) "
          f"[input {len(sql)}, cycles +{rep_sq}, omitted {omit_sq}]")
    print(f"total:    {len(merged)}")
    print(f"system-free identity: {sysfree_id}/{len(filled_id)} "
          f"({sysfree_id/len(filled_id):.1%})" if filled_id else "no identity rows")
    print(f"wrote {a.out} + {os.path.basename(report_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
