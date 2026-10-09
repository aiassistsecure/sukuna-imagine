#!/usr/bin/env python3
"""Deterministically balance v12 rows at 33% identity / 20% protocol / 47% SQL.

Ratios are row shares, not tokenizer-dependent token shares. Complete shuffled
cycles preserve every input row by default; repeats are intentional and reported.
"""
from __future__ import annotations
import argparse
import json
import math
import random
from collections import Counter
from pathlib import Path

SHARES = {'identity': 33, 'protocol': 20, 'sql': 47}


def load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding='utf-8') as source:
        for lineno, line in enumerate(source, 1):
            if not line.strip():
                continue
            rec = json.loads(line)
            messages = rec.get('messages') if isinstance(rec, dict) else None
            if (not isinstance(messages, list) or not messages or
                any(not isinstance(m, dict) or m.get('role') not in
                    {'system', 'user', 'assistant'} or not isinstance(m.get('content'), str)
                    or not m['content'].strip() for m in messages) or
                messages[-1]['role'] != 'assistant' or
                not any(m['role'] == 'user' for m in messages[:-1])):
                raise ValueError(f'{path}:{lineno}: expected nonempty chat messages ending in assistant')
            rows.append(rec)
    if not rows:
        raise ValueError(f'{path}: empty corpus')
    return rows


def balanced(pools: dict[str, list[dict]], seed: int, total: int | None = None):
    if total is None:
        total = 100 * max(math.ceil(len(pools[k]) / SHARES[k]) for k in SHARES)
    if total <= 0 or total % 100:
        raise ValueError('--total must be a positive multiple of 100')
    rng = random.Random(seed)
    merged, report = [], {'seed': seed, 'total': total, 'basis': 'rows', 'components': {}}
    for kind, share in SHARES.items():
        pool = pools[kind]
        quota = total // 100 * share
        if not pool:
            raise ValueError(f'{kind}: empty corpus')
        selected = []
        while len(selected) < quota:
            cycle = list(pool)
            rng.shuffle(cycle)
            selected.extend(cycle[:quota - len(selected)])
        # Do not overwrite the source metadata (kind, schema_key, provenance).
        merged.extend(selected)
        report['components'][kind] = {
            'input_rows': len(pool), 'output_rows': quota,
            'repeated_rows': max(0, quota - len(pool)),
            'omitted_rows': max(0, len(pool) - quota),
            'kinds': dict(Counter(r.get('meta', {}).get('kind', 'unspecified') for r in selected)),
            'without_system_rows': sum(not any(m['role'] == 'system' for m in r['messages']) for r in selected),
        }
    rng.shuffle(merged)
    return merged, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    for kind in SHARES:
        ap.add_argument('--' + kind, required=True)
    ap.add_argument('--out', default='corpus/v12_train.jsonl')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--total', type=int, help='positive multiple of 100; default retains every input row')
    a = ap.parse_args()
    try:
        paths = {k: Path(getattr(a, k)).resolve() for k in SHARES}
        output = Path(a.out)
        report_path = Path(str(output) + '.mix.json')
        if output.resolve() in paths.values() or report_path.resolve() in paths.values():
            raise ValueError('output must not overwrite an input corpus')
        pools = {k: load(str(p)) for k, p in paths.items()}
        merged, report = balanced(pools, a.seed, a.total)
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    report['sources'] = {k: str(p) for k, p in paths.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8') as target:
        for row in merged:
            target.write(json.dumps(row, ensure_ascii=False) + '\n')
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    print(f'wrote {output}; mix report {report_path}')
    if not report['components']['identity']['without_system_rows']:
        print('WARNING: identity has no system-free rows; unprompted identity is not directly supervised.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
