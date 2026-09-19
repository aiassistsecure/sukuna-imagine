#!/usr/bin/env python3
"""stealth :: gate fixtures — BOTH directions, always.

The imagine harness produced seven false-positive classes, every one of them
the same mistake: asserting an easy proxy instead of the property. The rule
that came out of it is R2 -- a check is not trusted until it is proven to fire
on bad input AND stay silent on good input.

So this file is half must-REJECT and half must-ACCEPT, and the must-ACCEPT half
matters more. A gate that rejects correct SQL would quietly starve the corpus
of exactly the examples worth learning from, and nothing downstream would ever
report it.

Run: python tests/test_gate.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from stealth.gate import Gate, Level, check_parse, check_safety  # noqa: E402
from stealth.sentinel import wrap, extract, extract_one, sql_of   # noqa: E402

DSN = os.environ.get(
    "STEALTH_DSN",
    "host=/agent/workspace/pgrun user=stealth dbname=shop",
)

# (label, sql, must_pass, expected_level_if_failing)
CASES = [
    # ---------- must ACCEPT: real, correct, answerable SQL ----------------
    ("simple count",
     "SELECT count(*) FROM orders WHERE status = 'paid'", True, None),
    ("join + group",
     "SELECT c.city, count(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
     "WHERE o.status = 'paid' GROUP BY c.city", True, None),
    ("order + limit",
     "SELECT title, price FROM products ORDER BY price DESC LIMIT 3", True, None),
    ("subquery",
     "SELECT name FROM customers WHERE id IN (SELECT customer_id FROM orders "
     "WHERE total > 400)", True, None),
    ("CTE",
     "WITH paid AS (SELECT * FROM orders WHERE status='paid') "
     "SELECT count(*) FROM paid", True, None),
    ("aggregate with having",
     "SELECT category, sum(stock) FROM products GROUP BY category HAVING sum(stock) > 10",
     True, None),
    ("quotes inside a literal",
     "SELECT * FROM customers WHERE name = 'O''Brien'", True, None),
    ("zero rows is still a valid answer",
     "SELECT * FROM orders WHERE status = 'cancelled'", True, None),

    # ---------- must REJECT ----------------------------------------------
    ("not SQL at all",
     "show me the paid orders please", False, Level.REJECTED),
    ("syntax error",
     "SELECT FROM WHERE orders", False, Level.REJECTED),
    ("NQL, not SQL — the old DSL must not sneak through",
     'FROM orders WHERE status = "paid"', False, Level.REJECTED),
    ("hallucinated table",
     "SELECT * FROM invoices", False, Level.SAFETY),
    ("hallucinated column",
     "SELECT customer_name FROM orders", False, Level.SAFETY),
    ("ambiguous column across joined tables",
     "SELECT id FROM orders o JOIN customers c ON c.id = o.customer_id", False, Level.SAFETY),
    ("type error",
     "SELECT * FROM orders WHERE placed_at > 'not-a-date'", False, Level.SAFETY),
    ("write attempt — DELETE",
     "DELETE FROM orders", False, Level.PARSE),
    ("write attempt — UPDATE",
     "UPDATE orders SET status='paid'", False, Level.PARSE),
    ("DDL",
     "DROP TABLE orders", False, Level.PARSE),
    ("two statements",
     "SELECT 1; SELECT 2", False, Level.PARSE),
    ("stacked write behind a read",
     "SELECT 1; DROP TABLE orders", False, Level.PARSE),
    ("sleep / resource abuse",
     "SELECT pg_sleep(30)", False, Level.PARSE),
    ("system catalog snooping",
     "SELECT * FROM pg_catalog.pg_user", False, Level.PARSE),
    ("empty",
     "", False, Level.REJECTED),
]

# (label, candidate, reference, should_agree)
AGREE_CASES = [
    ("count(*) vs count(id) — same answer, different text",
     "SELECT count(*) FROM orders WHERE status='paid'",
     "SELECT count(id) FROM orders WHERE status='paid'", True),
    # Neither side specifies an order, so PostgreSQL promises none and the
    # row sequence is not part of the answer -> compare as a set.
    ("same rows, no ORDER BY on either side, different phrasing",
     "SELECT c.name FROM customers c WHERE c.tier = 'pro'",
     "SELECT name FROM customers WHERE tier='pro'", True),
    # The inverse, and the gate found it before I pinned it: when the
    # REFERENCE asks for a specific order, sequence IS the answer. Returning
    # the right rows in the wrong order is a wrong answer.
    ("conflicting explicit ORDER BY — sequence is part of the answer",
     "SELECT name FROM customers WHERE tier='pro' ORDER BY name",
     "SELECT name FROM customers WHERE tier='pro' ORDER BY name DESC", False),
    ("wrong literal case — nearly identical text, DIFFERENT answer",
     "SELECT count(*) FROM orders WHERE status='Paid'",
     "SELECT count(*) FROM orders WHERE status='paid'", False),
    ("wrong operator",
     "SELECT count(*) FROM products WHERE price >= 249",
     "SELECT count(*) FROM products WHERE price > 249", False),
    ("wrong column entirely",
     "SELECT sum(stock) FROM products",
     "SELECT sum(price) FROM products", False),
    ("equivalent via join vs subquery",
     "SELECT DISTINCT c.name FROM customers c JOIN orders o ON o.customer_id=c.id "
     "WHERE o.total > 400 ORDER BY c.name",
     "SELECT name FROM customers WHERE id IN "
     "(SELECT customer_id FROM orders WHERE total > 400) ORDER BY name", True),
]

SENTINEL_CASES = [
    ("plain sql block", wrap("SELECT 1"), "SELECT 1"),
    ("sql containing semicolons and quotes",
     wrap("SELECT 'a;b', \"weird col\" FROM t WHERE x = 'it''s'"),
     "SELECT 'a;b', \"weird col\" FROM t WHERE x = 'it''s'"),
    ("multi-line sql",
     wrap("SELECT a,\n       b\nFROM t\nWHERE c = 1"),
     "SELECT a,\n       b\nFROM t\nWHERE c = 1"),
    ("prose around the block is ignored",
     "Sure, here you go:\n" + wrap("SELECT 2") + "\nHope that helps!", "SELECT 2"),
    ("unterminated block yields nothing", "<<<SQL>>>\nSELECT 3", None),
    ("two blocks is not one answer", wrap("SELECT 1") + wrap("SELECT 2"), None),
    ("no block at all", "SELECT 4", None),
    ("unanswerable is not sql", wrap("no revenue column exists", "UNANSWERABLE"), None),
]


def main() -> int:
    fails = 0

    print("== sentinel envelope ==")
    for label, text, expect in SENTINEL_CASES:
        got = sql_of(text)
        ok = (got == expect)
        fails += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:46} -> {got!r}")

    b = extract_one(wrap("nothing here", "UNANSWERABLE"))
    ok = b is not None and b.kind == "UNANSWERABLE"
    fails += not ok
    print(f"  [{'ok  ' if ok else 'FAIL'}] {'UNANSWERABLE block is recognised':46} -> "
          f"{b.kind if b else None}")

    print("\n== gate levels (live PostgreSQL) ==")
    g = Gate(DSN)
    try:
        g.connect()
    except Exception as e:
        print(f"  FATAL: cannot reach PostgreSQL at {DSN!r}: {e}")
        return 2

    for label, sql, must_pass, exp_level in CASES:
        r = g.run(sql)
        ok = (r.ok == must_pass)
        if ok and not must_pass and exp_level is not None:
            ok = (r.level == exp_level)
        fails += not ok
        verdict = "PASS" if r.ok else f"stop@{r.level.name}"
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:46} {verdict:14} {r.reason[:52]}")

    print("\n== L4 result agreement ==")
    for label, cand, ref, should in AGREE_CASES:
        r = g.run(cand, reference_sql=ref)
        agreed = r.ok and r.level == Level.AGREE
        ok = (agreed == should)
        fails += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:46} "
              f"{'agree' if agreed else 'differ':6} {r.reason[:44]}")

    g.close()
    total = len(SENTINEL_CASES) + 1 + len(CASES) + len(AGREE_CASES)
    print(f"\n{total - fails}/{total} behaved as specified")
    if fails:
        print("GATE IS WRONG — fix it before forging any corpus")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
