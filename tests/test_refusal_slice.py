#!/usr/bin/env python3
"""stealth :: refusal-slice generator round-trip tests

scripts/make_refusal_slice.py builds UNANSWERABLE/CLARIFY training rows. The
two things that must hold for every row:

  1. ZERO PROMPT DRIFT — the system+user messages must be byte-identical to
     the real build_prompt(schema, question) from stealth/forge.py. The
     generator imports build_prompt (never reimplements it); this test
     re-derives the prompt independently and compares.
  2. PARSE CONTRACT — the assistant target must parse with the real
     stealth/sentinel.py parser (extract_one) to exactly one block of the
     expected kind, with a non-empty payload.

Also asserts: all six catalog schemas and both kinds are covered, generation
is deterministic in the seed, and --count-per-kind larger than a bank cycles
without breaking the contract.

DB-free: forge.py is loaded as a file with the DB-backed gate and psycopg2
stubbed (same trick as ~/workspace/v12-refusal-regression/run_refusal_suite.py).

Run: python tests/test_refusal_slice.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STEALTH = os.path.join(REPO, "stealth")
SCRIPTS = os.path.join(REPO, "scripts")


def _load_stealth():
    pkg = types.ModuleType("stealth")
    pkg.__path__ = [STEALTH]
    sys.modules["stealth"] = pkg

    def load(name, path):
        spec = importlib.util.spec_from_file_location("stealth." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["stealth." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
        return mod

    sentinel = load("sentinel", os.path.join(STEALTH, "sentinel.py"))
    schemas = load("schemas", os.path.join(STEALTH, "schemas.py"))
    gate_stub = types.ModuleType("stealth.gate")
    gate_stub.Gate = type("Gate", (), {})
    gate_stub.Level = type("Level", (), {})
    sys.modules["stealth.gate"] = gate_stub
    pkg.gate = gate_stub
    pg = types.ModuleType("psycopg2")
    pg.__path__ = []
    sys.modules["psycopg2"] = pg
    forge = load("forge", os.path.join(STEALTH, "forge.py"))
    return forge, sentinel, schemas


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "make_refusal_slice", os.path.join(SCRIPTS, "make_refusal_slice.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_refusal_slice"] = mod
    spec.loader.exec_module(mod)
    return mod


forge, sentinel, schemas = _load_stealth()
gen = _load_generator()

EXPECTED_SCHEMAS = ("shop", "clinic", "library", "fleet", "audit", "telemetry")
EXPECTED_KINDS = ("UNANSWERABLE", "CLARIFY")

failures = []


def check(label, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def test_zero_prompt_drift():
    """messages[0:2] must equal a fresh real build_prompt() call."""
    rows = gen.generate_rows(list(EXPECTED_SCHEMAS), 3, seed=1337)
    bad = 0
    for r in rows:
        key = r["meta"]["schema_key"]
        question = r["meta"]["question"]
        expected = forge.build_prompt(schemas.get(key), question)
        if r["messages"][:2] != expected:
            bad += 1
            check("zero-prompt-drift", False, f"{key}: {question[:60]!r}")
            return
    check("zero-prompt-drift", True)


def test_parse_contract():
    """assistant target must be exactly one block of the expected kind."""
    rows = gen.generate_rows(list(EXPECTED_SCHEMAS), 3, seed=1337)
    bad = 0
    for r in rows:
        expected_kind = r["meta"]["refusal_kind"]
        assistant = r["messages"][-1]["content"]
        blk = sentinel.extract_one(assistant)
        if blk is None:
            bad += 1
            check("parse-contract", False, f"no single block for {r['meta']['question'][:50]!r}")
            return
        if blk.kind != expected_kind or not blk.payload.strip():
            bad += 1
            check("parse-contract", False,
                  f"got {blk.kind!r} (expected {expected_kind!r}) for {r['meta']['question'][:50]!r}")
            return
    check("parse-contract", True)


def test_coverage():
    rows = gen.generate_rows(list(EXPECTED_SCHEMAS), 3, seed=1337)
    got_schemas = {r["meta"]["schema_key"] for r in rows}
    got_kinds = {r["meta"]["refusal_kind"] for r in rows}
    check("all-six-schemas", got_schemas == set(EXPECTED_SCHEMAS),
          f"missing {set(EXPECTED_SCHEMAS) - got_schemas}")
    check("both-kinds", got_kinds == set(EXPECTED_KINDS),
          f"missing {set(EXPECTED_KINDS) - got_kinds}")
    check("row-count", len(rows) == 6 * 2 * 3, f"got {len(rows)}")


def test_determinism():
    a = gen.generate_rows(list(EXPECTED_SCHEMAS), 3, seed=42)
    b = gen.generate_rows(list(EXPECTED_SCHEMAS), 3, seed=42)
    check("deterministic-seed",
          [json.dumps(r, sort_keys=True) for r in a]
          == [json.dumps(r, sort_keys=True) for r in b])


def test_cycle_contract():
    """count-per-kind bigger than a bank must still round-trip."""
    rows = gen.generate_rows(["shop"], 30, seed=7)
    check("cycle-count", len(rows) == 60, f"got {len(rows)}")
    for r in rows:
        blk = sentinel.extract_one(r["messages"][-1]["content"])
        if blk is None or blk.kind != r["meta"]["refusal_kind"]:
            check("cycle-contract", False, r["meta"]["question"][:50])
            return
        expected = forge.build_prompt(schemas.get("shop"), r["meta"]["question"])
        if r["messages"][:2] != expected:
            check("cycle-contract", False, "prompt drift on cycle")
            return
    check("cycle-contract", True)


def test_row_format():
    rows = gen.generate_rows(["telemetry"], 1, seed=1)
    r = rows[0]
    roles = [m["role"] for m in r["messages"]]
    check("row-roles", roles == ["system", "user", "assistant"], str(roles))
    check("meta-kind", r["meta"]["kind"] == "refusal")
    check("system-contract-mentions-refusal",
          "UNANSWERABLE" in r["messages"][0]["content"]
          and "CLARIFY" in r["messages"][0]["content"])


def test_schema_filter():
    keys = gen.parse_schema_keys("shop,library")
    check("schema-filter", keys == ["shop", "library"], str(keys))
    check("schema-all", gen.parse_schema_keys("all") == list(schemas.CATALOG.keys()))
    try:
        gen.parse_schema_keys("shop,bogus")
        check("schema-rejects-unknown", False, "no error raised")
    except ValueError:
        check("schema-rejects-unknown", True)


def main():
    test_zero_prompt_drift()
    test_parse_contract()
    test_coverage()
    test_determinism()
    test_cycle_contract()
    test_row_format()
    test_schema_filter()
    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        return 1
    print("all refusal-slice tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
