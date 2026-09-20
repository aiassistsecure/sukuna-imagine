#!/usr/bin/env python3
"""Build a protocol-focused Imagine curriculum from verified SQL rows.

This does NOT invent new SQL. It reuses already-admitted SQL and varies only
the natural-language framing so the model learns one thing cleanly:
exactly one sentinel block, no prose, no Markdown, no trailing commentary.
"""
from __future__ import annotations

import argparse
import json
import os
import random

SYSTEM = (
    "You are Imagine, a PostgreSQL compiler. "
    "Return exactly one sentinel block and nothing else. "
    "For valid database questions use <<<SQL>>> followed by one read-only "
    "PostgreSQL SELECT/WITH statement and <<<END>>>. "
    "Never emit Markdown fences, explanations, labels, or trailing text."
)

PREFIXES = [
    "{q}",
    "Write the PostgreSQL for this request: {q}",
    "Return only the SQL result for: {q}",
    "Compile this database request: {q}",
    "Using only the supplied schema, answer: {q}",
    "Produce one read-only PostgreSQL query for: {q}",
    "No explanation. SQL only for: {q}",
    "Translate this request to PostgreSQL: {q}",
]

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="corpus/sql_train.jsonl")
    ap.add_argument("--out", default="corpus/protocol_train.jsonl")
    ap.add_argument("--copies", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1337)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    rows = [json.loads(line) for line in open(a.source) if line.strip()]
    out = []

    for rec in rows:
        msgs = rec["messages"]
        user = next(m["content"] for m in msgs if m["role"] == "user")
        assistant = msgs[-1]["content"]
        if not assistant.startswith("<<<SQL>>>") or not assistant.rstrip().endswith("<<<END>>>"):
            continue

        # Preserve schema verbatim; vary only the question lead-in.
        if "\n\nQuestion:" in user:
            schema, question = user.rsplit("\n\nQuestion:", 1)
            question = question.strip()
        else:
            schema, question = "", user.strip()

        choices = PREFIXES[:]
        rng.shuffle(choices)
        for template in choices[:max(1, min(a.copies, len(choices)))]:
            q = template.format(q=question)
            content = f"{schema}\n\nQuestion: {q}" if schema else q
            out.append({
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": assistant},
                ],
                "meta": {"kind": "protocol", "source_kind": rec.get("meta", {}).get("kind")},
            })

    rng.shuffle(out)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        for rec in out:
            f.write(json.dumps(rec) + "\n")
    print(f"wrote {len(out)} protocol rows -> {a.out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
