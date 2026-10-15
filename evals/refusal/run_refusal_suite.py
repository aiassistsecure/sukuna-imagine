#!/usr/bin/env python3
"""Imagine standing refusal/clarification regression suite.

Measures the gap disclosed in the 2026-10-13 CTO brief: the 40/40 telemetry
eval covers valid questions only, and a manual out-of-schema probe ("capital
of France" on a shop schema) hallucinated a column instead of emitting
<<<UNANSWERABLE>>>. v12 scored 2/18 on this suite; this file exists so v13
cannot regress it silently.

Design notes:
- Prompts are built with the REAL build_prompt() from stealth/forge.py
  (loaded as a file from the repo root; the DB-backed gate and psycopg2 are
  stubbed because this suite needs no database).
- Blocks are parsed with the REAL sentinel parser (stealth/sentinel.py).
- Scoring is DB-free: refusal verdicts are behavioral (which block kind was
  emitted), and any SQL emitted on a refusal probe is checked for invented
  identifiers against the schema catalog.
- Expected kinds come from probes.jsonl; the model's system prompt itself
  defines the contract ("If the schema cannot answer the question, use
  UNANSWERABLE." / "If the question has multiple materially different
  interpretations, use CLARIFY.").
- All paths are resolved relative to this file: the repo root is the parent
  of evals/, and stealth/forge.py + stealth/sentinel.py are loaded from
  there. Run it from anywhere.

Usage:
  python3 evals/refusal/run_refusal_suite.py --selftest   # harness check, no model
  python3 evals/refusal/run_refusal_suite.py              # needs llama-server on :8081
  python3 evals/refusal/run_refusal_suite.py --endpoint URL
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import types
import urllib.request
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNNER_DIR.parent.parent          # evals/refusal -> evals -> repo root
STEALTH = REPO_ROOT / "stealth"
PROBES = RUNNER_DIR / "probes.jsonl"
DEFAULT_RESULTS = RUNNER_DIR / "results.json"
DEFAULT_ENDPOINT = "http://localhost:8081/v1/chat/completions"


def _load_stealth():
    """Load sentinel/schemas/forge as files, bypassing the DB-backed __init__."""
    pkg = types.ModuleType("stealth")
    pkg.__path__ = [str(STEALTH)]
    sys.modules["stealth"] = pkg

    def load(name, path):
        spec = importlib.util.spec_from_file_location(
            "stealth." + name, str(path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["stealth." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
        return mod

    sentinel = load("sentinel", STEALTH / "sentinel.py")
    schemas = load("schemas", STEALTH / "schemas.py")
    gate_stub = types.ModuleType("stealth.gate")
    gate_stub.Gate = type("Gate", (), {})
    gate_stub.Level = type("Level", (), {})
    sys.modules["stealth.gate"] = gate_stub
    pkg.gate = gate_stub
    pg = types.ModuleType("psycopg2")
    pg.__path__ = []
    sys.modules["psycopg2"] = pg
    forge = load("forge", STEALTH / "forge.py")
    return forge, sentinel, schemas


forge, sentinel, schemas = _load_stealth()

# ------------------------------------------------------------------ generate

def generate_llamacpp(messages, endpoint=DEFAULT_ENDPOINT,
                       model="imagine-v12", timeout=180) -> str:
    body = json.dumps({
        "messages": messages,
        "model": model,
        "temperature": 0.0,
        "max_tokens": 512,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    return out["choices"][0]["message"]["content"]


# ------------------------------------------------------- identifier checking

SQL_KEYWORDS = frozenset("""
    select from where and or not null as on join inner left right outer full
    cross natural using group by order having limit offset distinct count sum
    avg min max desc asc between in like ilike is case when then else end
    cast true false with union all except intersect exists any some over
    partition rows row range interval date timestamp timestamptz time text
    int integer bigint smallint numeric decimal real double precision boolean
    bool char varchar serial bigserial primary key foreign references unique
    default check constraint index coalesce nullif greatest least now
    current_date current_timestamp extract date_trunc to_char to_date
    generate_series unnest array lateral recursive do instead of values
    returning ilike similar to escape collate
""".split())


def schema_identifiers(schema) -> set[str]:
    ids = set()
    for t in schema.tables:
        ids.add(t.name.lower())
        for c in t.columns:
            ids.add(c.name.lower())
    return ids


def invented_identifiers(payload: str, schema) -> list[str]:
    """Tokens in the SQL that are neither schema identifiers nor SQL keywords."""
    known = schema_identifiers(schema)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", payload)
    bad = []
    for tok in tokens:
        low = tok.lower()
        if low in known or low in SQL_KEYWORDS:
            continue
        if low not in bad:
            bad.append(low)
    return bad


# ------------------------------------------------------------------- scoring

def score_probe(probe: dict, text: str) -> dict:
    schema = schemas.CATALOG[probe["schema_key"]]
    blk = sentinel.extract_one(text)
    strict = blk is not None and text.strip() == blk.raw.strip()
    if blk is None:
        return {"pass": False, "verdict": "unparseable_block",
                "emitted": None, "strict_protocol": False,
                "output": text[:300]}
    emitted = blk.kind
    invented = []
    if emitted == "SQL":
        invented = invented_identifiers(blk.payload, schema)
    expected = probe["expected"]
    if expected == "SQL":
        ok = emitted == "SQL" and not invented
    else:
        ok = emitted == expected
    verdict = (
        "correct_refusal" if ok and expected != "SQL"
        else "correct_answer" if ok
        else f"wrong_kind:emitted_{emitted}"
    )
    if emitted == "SQL" and invented:
        verdict += f":invented_{','.join(invented)}"
    return {"pass": ok, "verdict": verdict, "emitted": emitted,
            "expected": expected, "strict_protocol": strict,
            "invented": invented, "output": blk.payload[:200]}


def run_suite(generate) -> list[dict]:
    probes = [json.loads(l) for l in PROBES.open() if l.strip()]
    results = []
    for p in probes:
        schema = schemas.CATALOG[p["schema_key"]]
        messages = forge.build_prompt(schema, p["question"])
        text = generate(messages)
        r = score_probe(p, text)
        results.append({"id": p["id"], "question": p["question"],
                        "note": p["note"], **r})
    return results


def report(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(1 for r in results if r["pass"])
    by_expected: dict[str, dict] = {}
    for r in results:
        b = by_expected.setdefault(r["expected"], {"n": 0, "pass": 0})
        b["n"] += 1
        b["pass"] += r["pass"]
    hallucinations = [r for r in results
                      if r["emitted"] == "SQL" and r["invented"]]
    print(f"\nRefusal regression: {passed}/{n} passed")
    for exp, b in by_expected.items():
        print(f"  expected {exp:13s} {b['pass']}/{b['n']}")
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"  [{mark}] {r['id']} ({r['expected']}) -> {r['verdict']}")
        if not r["pass"]:
            print(f"        Q: {r['question']}")
            print(f"        out: {r['output'][:160]!r}")
    if hallucinations:
        print("\nHallucinated identifiers on refusal probes:")
        for r in hallucinations:
            print(f"  {r['id']}: {', '.join(r['invented'])}")
    return {"passed": passed, "n": n, "by_expected": by_expected,
            "hallucination_probes": [r["id"] for r in hallucinations]}


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    """Validate the harness against canned model outputs (no model needed)."""
    cases = [
        # (probe_id, canned_output, expect_pass)
        ("R01", "<<<UNANSWERABLE>>>\nno capital column in schema\n<<<END>>>", True),
        ("R01", "<<<SQL>>>\nSELECT capital FROM customers;\n<<<END>>>", False),  # hallucinated column
        ("R01", "The capital of France is Paris.", False),  # no block
        ("R02", "<<<CLARIFY>>>\nDo you mean customers by tier?\n<<<END>>>", False),  # wrong kind
        ("C01", "<<<CLARIFY>>>\nBest by lifetime value or by order volume?\n<<<END>>>", True),
        ("C01", "<<<SQL>>>\nSELECT name FROM customers ORDER BY lifetime_value DESC LIMIT 5;\n<<<END>>>", False),
        ("S01", "<<<SQL>>>\nSELECT count(*) FROM customers;\n<<<END>>>", True),
        ("S01", "<<<UNANSWERABLE>>>\ncannot answer\n<<<END>>>", False),  # over-refusal
        ("S02", "<<<SQL>>>\nSELECT hostname FROM hosts;\n<<<END>>>", True),
        ("R03", "<<<SQL>>>\nSELECT name, password FROM customers;\n<<<END>>>", False),  # invented sensitive col
        # Extra prose outside the block: extract_one still finds the one
        # block and the kind matches, so it passes; strict_protocol=False is
        # reported, not scored.
        ("R01", "Sure! <<<UNANSWERABLE>>>\nnope\n<<<END>>> extra prose", True),
    ]
    probes = {json.loads(l)["id"]: json.loads(l)
              for l in PROBES.open() if l.strip()}
    fails = 0
    for pid, canned, want in cases:
        got = score_probe(probes[pid], canned)
        status = "ok" if got["pass"] == want else "MISMATCH"
        if got["pass"] != want:
            fails += 1
        print(f"[{status}] {pid} want_pass={want} got_pass={got['pass']} "
              f"verdict={got['verdict']} strict={got['strict_protocol']}")
    print(f"\nselftest: {'PASS' if fails == 0 else f'{fails} MISMATCHES'}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="validate the harness with canned outputs (no model)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help="llama-server chat-completions endpoint")
    ap.add_argument("--out", default=str(DEFAULT_RESULTS),
                    help="where to write results.json (live runs only)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    results = run_suite(
        lambda messages: generate_llamacpp(messages, endpoint=args.endpoint))
    summary = report(results)
    json.dump({"summary": summary, "results": results},
              open(args.out, "w"), indent=2)
    print(f"\nwrote {args.out}")
    return 0 if summary["passed"] == summary["n"] else 2


if __name__ == "__main__":
    sys.exit(main())
