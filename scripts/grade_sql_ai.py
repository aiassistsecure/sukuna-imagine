#!/usr/bin/env python3
"""AI semantic grading for Imagine SQL evaluation results.

This complements PostgreSQL execution gating instead of replacing it.

The database gate remains authoritative for:
- parse / safety / bind / execute
- invented relations / columns
- exact result-set agreement with the reference

The AI judge answers a different question:
Does the candidate SQL semantically satisfy the natural-language request on
the supplied schema, even if its projection/result shape differs from one
reference query?

Usage:
  python scripts/grade_sql_ai.py \
    --eval eval_sql_v6_protocol.json \
    --judge deepseek-ai/deepseek-coder-6.7b-instruct \
    --out eval_sql_v6_ai.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stealth.evaluate import hf_generator
from stealth.schemas import get

JUDGE_SYSTEM = """You are a strict PostgreSQL semantic evaluator.

You will receive:
- a database schema,
- a natural-language question,
- a candidate SQL query,
- a reference SQL query,
- and the deterministic database-gate verdict.

Judge whether the CANDIDATE SQL semantically answers the user's question on
the supplied schema.

Important:
- Do NOT require exact SQL text matching.
- Do NOT require identical projection to the reference unless the user's
  question itself requires those columns/shape.
- A different but sufficient projection may still be fully correct.
- A join not used by the reference may still be correct if it preserves the
  intended answer.
- Respect requested ordering, top-N limits, filtering, dates, grouping,
  aggregation, and cardinality.
- If the candidate changes the meaning, omits information explicitly requested,
  adds a materially wrong predicate, or otherwise answers a different question,
  it is not fully correct.
- PostgreSQL parse/bind/execute failures are always incorrect.
- Invented relations/columns are always incorrect.

Return EXACTLY one JSON object:
{"score": 0.0, "pass": false, "reason": "short reason"}

score:
1.0 = semantically correct answer
0.5 = partially correct / arguably acceptable but materially incomplete
0.0 = wrong
Set pass=true only for 1.0.
"""

def extract_json(text: str) -> dict:
    s=(text or "").strip()
    try:
        d=json.loads(s)
    except json.JSONDecodeError:
        m=re.search(r"\{.*?\}",s,re.S)
        if not m:
            raise ValueError(f"no JSON in judge response: {s[:240]!r}")
        d=json.loads(m.group(0))
    score=float(d.get("score",0.0))
    if score not in (0.0,0.5,1.0):
        raise ValueError(f"invalid score {score}")
    return {
        "score":score,
        "pass":bool(d.get("pass",score==1.0)) and score==1.0,
        "reason":str(d.get("reason","")).strip(),
    }

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--eval",required=True)
    ap.add_argument("--judge",default="deepseek-ai/deepseek-coder-6.7b-instruct")
    ap.add_argument("--out",default="eval_sql_ai.json")
    ap.add_argument("--max-new",type=int,default=128)
    ap.add_argument("--only-mismatches",action="store_true",
                    help="grade only non-correct deterministic gate cases")
    a=ap.parse_args()

    data=json.load(open(a.eval,encoding="utf-8"))
    judge=hf_generator(a.judge,max_new=a.max_new)

    details=[]
    by_schema=defaultdict(lambda:{"n":0,"passed":0,"score":0.0})

    source_details=data.get("details",[])
    for i,d in enumerate(source_details,1):
        deterministic=d.get("verdict","")
        if a.only_mismatches and deterministic=="correct":
            continue

        sql=d.get("sql") or d.get("raw_sql")
        if not sql:
            verdict={"score":0.0,"pass":False,"reason":"no candidate SQL available"}
            raw=""
        elif deterministic.startswith("stop@") or deterministic in {
            "invented_relation","invented_column","unparseable_block"
        }:
            verdict={"score":0.0,"pass":False,
                     "reason":f"deterministic gate failure: {deterministic}"}
            raw=""
        else:
            sch=get(d["schema_key"])
            prompt=(
                f"SCHEMA:\n{sch.prompt_text('ddl')}\n\n"
                f"QUESTION:\n{d['question']}\n\n"
                f"CANDIDATE SQL:\n{sql}\n\n"
                f"REFERENCE SQL:\n{d['reference_sql']}\n\n"
                f"DATABASE GATE VERDICT:\n{deterministic}\n"
                f"GATE REASON:\n{d.get('reason','')}"
            )
            raw=judge([
                {"role":"system","content":JUDGE_SYSTEM},
                {"role":"user","content":prompt},
            ])
            try:
                verdict=extract_json(raw)
            except Exception as e:
                verdict={"score":0.0,"pass":False,
                         "reason":f"judge_parse_error: {e}"}

        k=d["schema_key"]
        by_schema[k]["n"]+=1
        by_schema[k]["passed"]+=int(verdict["pass"])
        by_schema[k]["score"]+=verdict["score"]

        details.append({
            "schema_key":k,
            "question":d["question"],
            "candidate_sql":sql,
            "reference_sql":d.get("reference_sql"),
            "deterministic_verdict":deterministic,
            "gate_reason":d.get("reason",""),
            "judge":verdict,
            "judge_raw":raw,
        })
        icon="✓" if verdict["pass"] else ("~" if verdict["score"]==0.5 else "✗")
        print(f"{icon} {i:02d} {k:12} AI={verdict['score']:.1f} gate={deterministic}")
        print(f"    {verdict['reason']}")

    n=len(details)
    passed=sum(int(x["judge"]["pass"]) for x in details)
    score=sum(x["judge"]["score"] for x in details)
    per={}
    for k,v in sorted(by_schema.items()):
        per[k]={
            "n":v["n"],
            "passed":v["passed"],
            "pass_rate":round(v["passed"]/max(v["n"],1),4),
            "mean_score":round(v["score"]/max(v["n"],1),4),
        }

    summary={
        "n":n,
        "passed":passed,
        "semantic_pass_rate":round(passed/max(n,1),4),
        "semantic_score":round(score/max(n,1),4),
        "judge":a.judge,
        "per_schema":per,
    }
    json.dump({"summary":summary,"details":details},
              open(a.out,"w",encoding="utf-8"),indent=2,ensure_ascii=False)

    print("\n══════════════════ AI SQL SEMANTIC EVALUATION ══════════════════")
    print(f"  strict semantic passes {passed}/{n}  {summary['semantic_pass_rate']:.1%}")
    print(f"  AI semantic score      {summary['semantic_score']:.1%}")
    print(f"  judge                  {a.judge}")
    print(f"\nwrote {a.out}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
