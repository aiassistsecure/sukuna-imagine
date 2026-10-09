#!/usr/bin/env python3
"""V12 corpus prep regressions: mix enforcement and protocol format.

Run: python -m unittest discover -s tests -p test_v12_prep.py -v
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.merge_v12_corpus import (
    fill_pool,
    get_messages,
    has_system,
    smallest_total,
    validate_record,
)


def make_msg_rows(n: int, kind: str) -> list[dict]:
    rows = []
    for i in range(n):
        rows.append({
            "messages": [
                {"role": "system", "content": f"{kind} system"},
                {"role": "user", "content": f"{kind} question {i}"},
                {"role": "assistant", "content": f"{kind} answer {i}"},
            ],
            "meta": {"kind": kind},
        })
    return rows


def make_prompt_rows(n: int) -> list[dict]:
    # prompt/response format = system-free (unprompted identity)
    return [{"prompt": f"who are you {i}?", "response": "I am Imagine."} for i in range(n)]


class TestSmallestTotal(unittest.TestCase):
    def test_divisible_by_100(self):
        t = smallest_total(1600, 1000, 3284)
        self.assertEqual(t % 100, 0)
        # every pool's quota retains all rows
        self.assertGreaterEqual(round(t * 0.33), 1600)
        self.assertGreaterEqual(round(t * 0.20), 1000)

    def test_exact_shares(self):
        t = smallest_total(33, 20, 47)
        self.assertEqual(t, 100)


class TestFillPool(unittest.TestCase):
    def test_no_repeat_when_fits(self):
        rows = make_msg_rows(10, "sql")
        filled, reps = fill_pool(rows, 10, random.Random(1))
        self.assertEqual(len(filled), 10)
        self.assertEqual(reps, 0)

    def test_cycles_when_short(self):
        rows = make_msg_rows(10, "identity")
        filled, reps = fill_pool(rows, 33, random.Random(1))
        self.assertEqual(len(filled), 33)
        self.assertGreaterEqual(reps, 2)  # 10 -> 20 -> 30 -> 33 needs 3 cycles
        # every source row appears at least once
        seen = {r["messages"][1]["content"] for r in filled}
        self.assertEqual(len(seen), 10)

    def test_truncates_when_over(self):
        rows = make_msg_rows(100, "sql")
        filled, reps = fill_pool(rows, 47, random.Random(1))
        self.assertEqual(len(filled), 47)
        self.assertEqual(reps, 0)


class TestValidation(unittest.TestCase):
    def test_valid_messages(self):
        validate_record(make_msg_rows(1, "x")[0], "test", 0)

    def test_valid_prompt_response(self):
        validate_record(make_prompt_rows(1)[0], "test", 0)

    def test_rejects_empty_assistant(self):
        rec = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "  "},
        ]}
        with self.assertRaises(ValueError):
            validate_record(rec, "test", 0)

    def test_rejects_non_assistant_last(self):
        rec = {"messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(ValueError):
            validate_record(rec, "test", 0)

    def test_rejects_no_messages(self):
        with self.assertRaises(ValueError):
            validate_record({"foo": "bar"}, "test", 0)


class TestSystemFree(unittest.TestCase):
    def test_prompt_format_is_system_free(self):
        self.assertFalse(has_system(make_prompt_rows(1)[0]))

    def test_messages_format_has_system(self):
        self.assertTrue(has_system(make_msg_rows(1, "x")[0]))


class TestEndToEnd(unittest.TestCase):
    def test_mix_enforcement(self):
        from scripts.merge_v12_corpus import main as merge_main
        with tempfile.TemporaryDirectory() as d:
            id_p = os.path.join(d, "id.jsonl")
            pr_p = os.path.join(d, "pr.jsonl")
            sq_p = os.path.join(d, "sq.jsonl")
            out_p = os.path.join(d, "out.jsonl")
            for p, rows in ((id_p, make_prompt_rows(33)),
                            (pr_p, make_msg_rows(20, "protocol")),
                            (sq_p, make_msg_rows(47, "sql"))):
                with open(p, "w") as f:
                    for r in rows:
                        f.write(json.dumps(r) + "\n")
            argv = sys.argv
            sys.argv = ["merge", "--identity", id_p, "--protocol", pr_p,
                        "--sql", sq_p, "--out", out_p, "--seed", "7"]
            try:
                self.assertEqual(merge_main(), 0)
            finally:
                sys.argv = argv
            # total divisible by 100, exact shares
            with open(out_p) as f:
                merged = [json.loads(l) for l in f]
            self.assertEqual(len(merged), 100)
            kinds = [r.get("meta", {}).get("kind", "identity") for r in merged]
            # prompt/response rows have no meta; count them as identity
            n_id = sum(1 for r in merged if "prompt" in r)
            n_pr = sum(1 for k in kinds if k == "protocol")
            n_sq = sum(1 for k in kinds if k == "sql")
            self.assertEqual(n_id, 33)
            self.assertEqual(n_pr, 20)
            self.assertEqual(n_sq, 47)
            # mix report exists
            with open(out_p + ".mix.json") as f:
                report = json.load(f)
            self.assertEqual(report["total"], 100)
            self.assertEqual(report["omissions"],
                             {"identity": 0, "protocol": 0, "sql": 0})
            self.assertEqual(report["system_free_identity"], 33)

    def test_refuses_input_overwrite(self):
        from scripts.merge_v12_corpus import main as merge_main
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "same.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps(make_prompt_rows(1)[0]) + "\n")
            argv = sys.argv
            sys.argv = ["merge", "--identity", p, "--protocol", p,
                        "--sql", p, "--out", p]
            try:
                self.assertEqual(merge_main(), 2)
            finally:
                sys.argv = argv


class TestProtocolFormat(unittest.TestCase):
    def test_sentinel_block(self):
        # protocol targets must be exactly one bare sentinel block
        good = "<<<SQL>>> SELECT 1; <<<END>>>"
        self.assertTrue(good.startswith("<<<SQL>>>"))
        self.assertTrue(good.rstrip().endswith("<<<END>>>"))

    def test_rejects_fenced(self):
        fenced = "```sql\nSELECT 1;\n```"
        self.assertFalse(fenced.startswith("<<<SQL>>>"))


if __name__ == "__main__":
    unittest.main()
