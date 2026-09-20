#!/usr/bin/env python3
"""Concatenate JSONL corpora without changing message content."""
from __future__ import annotations
import argparse, json, os

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("inputs", nargs="+")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    n = 0
    with open(a.out, "w") as dst:
        for path in a.inputs:
            for line in open(path):
                if not line.strip():
                    continue
                json.loads(line)
                dst.write(line if line.endswith("\n") else line + "\n")
                n += 1
    print(f"merged {n} rows -> {a.out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
