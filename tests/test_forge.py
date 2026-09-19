#!/usr/bin/env python3
"""stealth :: forge fixtures — is the gate actually biting?

The first forge run admitted 124 of 124 candidates. That is the expected
result for template-generated pairs, which are correct by construction... and
it is ALSO exactly what a silently-disabled gate looks like. A 100% pass rate
is not evidence of a working gate; it is evidence of nothing.

So this file feeds the forge deliberate poison and asserts that each kind dies
at the right level. If these ever pass, the corpus is worthless no matter what
the admit rate says.

Also checks the emitted corpus rows are actually trainable: schema present in
the prompt, sentinel-wrapped target, extractable SQL.

Run: python tests/test_forge.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from stealth.forge import Candidate, _init_worker, _process, materialise, build_prompt  # noqa: E402
from stealth.gate import Level                                                          # noqa: E402
from stealth.schemas import CATALOG, get                                                # noqa: E402
from stealth.sentinel import sql_of                                                     # noqa: E402

ADMIN_DSN = os.environ.get(
    "STEALTH_ADMIN_DSN",
    "host=/agent/workspace/pgrun user=stealth dbname=postgres",
)

# (label, sql, reference_sql, must_admit, expected_stop_level)
POISON = [
    # --- these MUST be rejected -------------------------------------------
    ("hallucinated table",
     "SELECT * FROM invoices", None, False, Level.SAFETY),
    ("hallucinated column",
     "SELECT revenue FROM orders", None, False, Level.SAFETY),
    ("reserved word left unquoted",
     "SELECT order FROM loans", None, False, Level.REJECTED),
    ("write disguised as a query",
     "SELECT 1; UPDATE orders SET status='paid'", None, False, Level.PARSE),
    ("plausible but WRONG answer — the whole reason L4 exists",
     "SELECT count(*) FROM orders WHERE status='Paid'",
     "SELECT count(*) FROM orders WHERE status='paid'", False, Level.EXECUTE),
    ("off-by-one operator",
     "SELECT count(*) FROM products WHERE price >= 249",
     "SELECT count(*) FROM products WHERE price > 249", False, Level.EXECUTE),
    ("right shape, wrong column",
     "SELECT sum(stock) FROM products",
     "SELECT sum(price) FROM products", False, Level.EXECUTE),
    ("broken reference poisons the pair, not just the candidate",
     "SELECT count(*) FROM orders",
     "SELECT count(*) FROM nonexistent", False, Level.EXECUTE),

    # --- these MUST be admitted -------------------------------------------
    ("correct, trivially",
     "SELECT count(*) FROM orders", "SELECT count(1) FROM orders", True, None),
    ("correct via a different shape",
     "SELECT DISTINCT c.name FROM customers c JOIN orders o ON o.customer_id=c.id "
     "WHERE o.total > 400 ORDER BY c.name",
     "SELECT name FROM customers WHERE id IN "
     "(SELECT customer_id FROM orders WHERE total > 400) ORDER BY name", True, None),
    ("correct and returns zero rows",
     "SELECT * FROM orders WHERE status='cancelled'",
     "SELECT * FROM orders WHERE status NOT IN ('paid','pending','refunded')", True, None),
]


def main() -> int:
    fails = 0

    print("* materialising shop + library")
    try:
        materialise(get("shop"), ADMIN_DSN)
        materialise(get("library"), ADMIN_DSN)
    except Exception as e:
        print(f"  FATAL: {type(e).__name__}: {e}")
        return 2

    _init_worker(ADMIN_DSN, 5000, 2000)

    print("\n== poison: does the forge gate actually bite? ==")
    for label, sql, ref, must_admit, exp_level in POISON:
        key = "library" if "loans" in sql or "order FROM" in sql else "shop"
        rec = _process(Candidate(key, "poison probe", sql, ref, "poison"))
        ok = (rec["admitted"] == must_admit)
        if ok and not must_admit and exp_level is not None:
            ok = (rec["level"] == int(exp_level))
        fails += not ok
        state = "ADMIT" if rec["admitted"] else f"stop@{Level(rec['level']).name}"
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:52} {state:14} {rec['reason'][:44]}")

    print("\n== emitted corpus rows are trainable ==")
    sch = get("shop")
    msgs = build_prompt(sch, "How many orders are paid?")
    checks = [
        ("system message present", msgs[0]["role"] == "system"),
        ("schema is IN the prompt, not the weights", "CREATE TABLE orders" in msgs[1]["content"]),
        ("question is in the prompt", "How many orders are paid?" in msgs[1]["content"]),
        ("sentinel contract stated", "<<<SQL>>>" in msgs[0]["content"]),
        ("refusal options offered", "<<<UNANSWERABLE>>>" in msgs[0]["content"]),
    ]
    for label, cond in checks:
        fails += not cond
        print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")

    path = os.path.join(os.path.dirname(__file__), "..", "corpus", "train.jsonl")
    if os.path.exists(path):
        rows = [json.loads(l) for l in open(path) if l.strip()]
        sample = rows[:200]
        bad_target = [r for r in sample if sql_of(r["messages"][-1]["content"]) is None]
        bad_schema = [r for r in sample
                      if "CREATE TABLE" not in r["messages"][1]["content"]]
        for label, bad in (("every target extracts to SQL", bad_target),
                           ("every prompt carries a schema", bad_schema)):
            ok = not bad
            fails += not ok
            print(f"  [{'ok  ' if ok else 'FAIL'}] {label:52} ({len(bad)} bad of {len(sample)})")
        keys = {r["meta"]["schema_key"] for r in rows}
        ok = len(keys) >= 3
        fails += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] {'corpus spans multiple schemas':52} {sorted(keys)}")
    else:
        print("  (no corpus yet — run `python -m stealth.forge` first)")

    total = len(POISON) + len(checks) + 3
    print(f"\n{total - fails}/{total} behaved as specified")
    if fails:
        print("FORGE GATE IS NOT BITING — the corpus cannot be trusted")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
