"""stealth :: the corpus forge

Materialises every schema into a real PostgreSQL database, generates candidate
(question, SQL) pairs, and runs every single one through the gate. Only pairs
the database agrees with are written out.

PARALLELISM. Each worker process owns one database connection and one slice of
the work. Connections are not shareable across processes and psycopg2 is not
thread-safe per connection, so process-per-worker is the correct shape --
threads would serialise on the connection anyway. Default worker count is
cpu_count(), which on the corpus box means the gate saturates the machine
rather than the GPU.

Two sources of candidate SQL, and they are complementary:

  TEMPLATE   deterministic generators over each schema. Perfect labels by
             construction, near-infinite volume, zero inference cost. This is
             how nedb-cast-slm got 200k pairs in 16.5 seconds.

  TEACHER    a larger model proposing SQL for harder/naturally-phrased
             questions. Higher ceiling, but the gate still has final say --
             a teacher that hallucinates a column simply gets rejected.

The teacher path is deliberately OPTIONAL. With execution gating in place, a
weaker teacher costs throughput, not correctness, so the pipeline must work
with no teacher at all.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, asdict
from multiprocessing import Pool, cpu_count

import psycopg2

from .gate import Gate, Level
from .schemas import Schema, CATALOG, TRAIN_KEYS, HELDOUT_KEYS
from .sentinel import wrap


# --------------------------------------------------------------- materialise --

def materialise(schema: Schema, admin_dsn: str, dbname: str | None = None) -> str:
    """Create and seed a real database for `schema`. Returns its dbname."""
    dbname = dbname or f"stealth_{schema.key}"
    con = psycopg2.connect(admin_dsn)
    con.autocommit = True
    try:
        with con.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        con.close()

    target = _swap_db(admin_dsn, dbname)
    con = psycopg2.connect(target)
    con.autocommit = True
    try:
        with con.cursor() as cur:
            for stmt in schema.ddl():
                cur.execute(stmt)
            for tmpl, rows in schema.seed():
                cur.executemany(tmpl, rows)
    finally:
        con.close()
    return dbname


def _swap_db(dsn: str, dbname: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    parts.append(f"dbname={dbname}")
    return " ".join(parts)


# ------------------------------------------------------------------ candidates --

@dataclass
class Candidate:
    schema_key: str
    question: str
    sql: str
    reference_sql: str | None   # None -> gate stops at L3 EXECUTE
    kind: str                   # template family, or "teacher"
    difficulty: int = 1


def template_candidates(schema: Schema, rng: random.Random) -> list[Candidate]:
    """Deterministic generators. Perfect labels by construction.

    Every generator emits BOTH a candidate and an equivalent-but-differently-
    written reference, so L4 has something to agree with. Where the reference
    is literally identical, L4 degenerates to L3 -- still useful, just weaker.
    """
    out: list[Candidate] = []
    S = schema

    for t in S.tables:
        tq = t.quoted
        cols = t.columns
        numeric = [c for c in cols if any(k in c.type for k in ("int", "numeric", "serial"))
                   and c.name != "id"]
        textual = [c for c in cols if c.type.startswith("text")]
        dated = [c for c in cols if "date" in c.type or "timestamp" in c.type]

        # --- count all -----------------------------------------------------
        out.append(Candidate(S.key, f"How many rows are in {t.name}?",
                             f"SELECT count(*) FROM {tq}",
                             f"SELECT count(1) FROM {tq}", "count_all"))

        # --- count with an equality filter ---------------------------------
        for c in textual[:2]:
            out.append(Candidate(
                S.key,
                f"How many {t.name} have {c.name} set?",
                f"SELECT count({c.quoted}) FROM {tq}",
                f"SELECT count(*) FROM {tq} WHERE {c.quoted} IS NOT NULL",
                "count_non_null", difficulty=2))

        # --- select with a numeric predicate -------------------------------
        for c in numeric[:2]:
            out.append(Candidate(
                S.key,
                f"Show {t.name} where {c.name} is greater than zero.",
                f"SELECT * FROM {tq} WHERE {c.quoted} > 0",
                f"SELECT * FROM {tq} WHERE NOT ({c.quoted} <= 0) AND {c.quoted} IS NOT NULL",
                "numeric_filter", difficulty=2))
            out.append(Candidate(
                S.key,
                f"What is the total {c.name} across all {t.name}?",
                f"SELECT sum({c.quoted}) FROM {tq}",
                f"SELECT sum({c.quoted}) FROM {tq}", "sum", difficulty=1))
            out.append(Candidate(
                S.key,
                f"What is the average {c.name} in {t.name}?",
                f"SELECT avg({c.quoted}) FROM {tq}",
                f"SELECT sum({c.quoted})::numeric / nullif(count({c.quoted}),0) FROM {tq}",
                "avg", difficulty=3))

        # --- group by ------------------------------------------------------
        for g in textual[:2]:
            out.append(Candidate(
                S.key,
                f"How many {t.name} are there for each {g.name}?",
                f"SELECT {g.quoted}, count(*) FROM {tq} GROUP BY {g.quoted}",
                f"SELECT {g.quoted}, count(1) FROM {tq} GROUP BY 1", "group_count",
                difficulty=2))

        # --- top-N ----------------------------------------------------------
        for c in numeric[:1]:
            for n in (3, 5):
                out.append(Candidate(
                    S.key,
                    f"What are the top {n} {t.name} by {c.name}?",
                    f"SELECT * FROM {tq} ORDER BY {c.quoted} DESC NULLS LAST LIMIT {n}",
                    f"SELECT * FROM {tq} ORDER BY {c.quoted} DESC NULLS LAST LIMIT {n}",
                    "top_n", difficulty=2))

        # --- date range -----------------------------------------------------
        for d in dated[:1]:
            out.append(Candidate(
                S.key,
                f"Which {t.name} happened in 2026?",
                f"SELECT * FROM {tq} WHERE {d.quoted} >= '2026-01-01' "
                f"AND {d.quoted} < '2027-01-01'",
                f"SELECT * FROM {tq} WHERE extract(year from {d.quoted}) = 2026",
                "date_range", difficulty=3))

    # --- joins across the first two tables that plausibly relate -----------
    if len(S.tables) >= 2:
        left, right = S.tables[0], S.tables[1]
        fk = None
        for c in right.columns:
            n = c.name.lower().replace('"', "")
            if n.endswith("_id") or n.endswith("id") and n != "id":
                fk = c
                break
        if fk is not None:
            out.append(Candidate(
                S.key,
                f"Show each {left.name} row alongside its related {right.name} count.",
                f"SELECT l.id, count(r.*) FROM {left.quoted} l "
                f"LEFT JOIN {right.quoted} r ON r.{fk.quoted} = l.id GROUP BY l.id",
                f"SELECT l.id, (SELECT count(*) FROM {right.quoted} r2 "
                f"WHERE r2.{fk.quoted} = l.id) FROM {left.quoted} l",
                "join_count", difficulty=4))

    rng.shuffle(out)
    return out


# ----------------------------------------------------------------- prompt fmt --

SYSTEM = (
    "You write PostgreSQL. You are given a schema and a question.\n"
    "Reply with exactly one sentinel block and nothing else:\n"
    "  <<<SQL>>> ... <<<END>>>            a single read-only SELECT\n"
    "  <<<UNANSWERABLE>>> ... <<<END>>>   the schema cannot answer this\n"
    "  <<<CLARIFY>>> ... <<<END>>>        the question is ambiguous; say what is missing\n"
    "Use only tables and columns that appear in the schema. Never invent one."
)


def build_prompt(schema: Schema, question: str, style: str = "ddl") -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"Schema:\n{schema.prompt_text(style)}\n\nQuestion: {question}"},
    ]


# --------------------------------------------------------------------- worker --

_WORKER: dict = {}


def _init_worker(admin_dsn: str, timeout_ms: int, max_rows: int):
    _WORKER["admin_dsn"] = admin_dsn
    _WORKER["timeout_ms"] = timeout_ms
    _WORKER["max_rows"] = max_rows
    _WORKER["gates"] = {}


def _gate_for(schema_key: str) -> Gate:
    gates = _WORKER["gates"]
    if schema_key not in gates:
        dsn = _swap_db(_WORKER["admin_dsn"], f"stealth_{schema_key}")
        g = Gate(dsn, statement_timeout_ms=_WORKER["timeout_ms"],
                 max_rows=_WORKER["max_rows"])
        g.connect()
        gates[schema_key] = g
    return gates[schema_key]


def _process(c: Candidate) -> dict:
    g = _gate_for(c.schema_key)
    r = g.run(c.sql, reference_sql=c.reference_sql)
    rec = {
        "schema_key": c.schema_key,
        "question": c.question,
        "sql": c.sql,
        "kind": c.kind,
        "difficulty": c.difficulty,
        "admitted": bool(r.ok),
        "level": int(r.level),
        "reason": r.reason,
        "rowcount": r.rowcount,
        "result_digest": r.result_digest,
        "elapsed_ms": r.elapsed_ms,
    }
    return rec


# ---------------------------------------------------------------------- forge --

def forge(admin_dsn: str, out_path: str, schema_keys=None, seed: int = 1337,
          workers: int | None = None, timeout_ms: int = 5000,
          max_rows: int = 2000, prompt_style: str = "ddl",
          rejects_path: str | None = None) -> dict:
    """Run the full forge. Returns a stats dict.

    Rejects are written too, and that is not an afterthought: the rejected
    pile is where you find out your generator is broken. A forge that only
    reports admissions can be 90% wrong and look perfect.
    """
    keys = list(schema_keys or TRAIN_KEYS)
    rng = random.Random(seed)
    workers = workers or cpu_count()

    print(f"* materialising {len(keys)} schemas")
    for k in keys:
        materialise(CATALOG[k], admin_dsn)
        print(f"  {k:12} ready")

    cands: list[Candidate] = []
    for k in keys:
        c = template_candidates(CATALOG[k], rng)
        cands.extend(c)
        print(f"  {k:12} {len(c):5d} candidates")
    print(f"* {len(cands)} candidates, {workers} workers")

    t0 = time.perf_counter()
    with Pool(workers, initializer=_init_worker,
              initargs=(admin_dsn, timeout_ms, max_rows)) as pool:
        recs = pool.map(_process, cands, chunksize=32)
    elapsed = time.perf_counter() - t0

    admitted = [r for r in recs if r["admitted"]]
    rejected = [r for r in recs if not r["admitted"]]

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        for r in admitted:
            sch = CATALOG[r["schema_key"]]
            f.write(json.dumps({
                "messages": build_prompt(sch, r["question"], prompt_style) +
                            [{"role": "assistant", "content": wrap(r["sql"])}],
                "meta": {k: r[k] for k in
                         ("schema_key", "kind", "difficulty", "rowcount", "result_digest")},
            }) + "\n")

    if rejects_path:
        with open(rejects_path, "w") as f:
            for r in rejected:
                f.write(json.dumps(r) + "\n")

    by_level: dict[str, int] = {}
    for r in rejected:
        by_level[Level(r["level"]).name] = by_level.get(Level(r["level"]).name, 0) + 1

    stats = {
        "candidates": len(cands),
        "admitted": len(admitted),
        "rejected": len(rejected),
        "admit_rate": round(len(admitted) / max(len(cands), 1), 4),
        "elapsed_s": round(elapsed, 2),
        "per_sec": round(len(cands) / max(elapsed, 1e-9), 1),
        "workers": workers,
        "rejected_by_stop_level": by_level,
        "out": out_path,
    }
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="stealth corpus forge")
    ap.add_argument("--dsn", default=os.environ.get(
        "STEALTH_ADMIN_DSN",
        "host=/agent/workspace/pgrun user=stealth dbname=postgres"))
    ap.add_argument("--out", default="corpus/train.jsonl")
    ap.add_argument("--rejects", default="corpus/rejects.jsonl")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--heldout", action="store_true",
                    help="forge the HELD-OUT schemas instead (for eval)")
    ap.add_argument("--style", default="ddl", choices=["ddl", "compact"])
    a = ap.parse_args()

    st = forge(a.dsn, a.out,
               schema_keys=HELDOUT_KEYS if a.heldout else TRAIN_KEYS,
               seed=a.seed, workers=a.workers or None,
               prompt_style=a.style, rejects_path=a.rejects)
    print("\n" + json.dumps(st, indent=2))
