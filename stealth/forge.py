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
from .sentinel import wrap, extract_one


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

        # --- richer numeric semantics ---------------------------------------
        # The held-out telemetry failures exposed an important curriculum gap:
        # the model could write valid SQL but sometimes changed projection,
        # ordering, or predicate semantics. Generate many equivalent forms so
        # L4 teaches the *result*, not one memorised query string.
        for c in numeric:
            out.extend([
                Candidate(
                    S.key,
                    f"What is the minimum {c.name} in {t.name}?",
                    f"SELECT min({c.quoted}) FROM {tq}",
                    f"SELECT {c.quoted} FROM {tq} WHERE {c.quoted} IS NOT NULL "
                    f"ORDER BY {c.quoted} ASC LIMIT 1",
                    "min", difficulty=2),
                Candidate(
                    S.key,
                    f"What is the maximum {c.name} in {t.name}?",
                    f"SELECT max({c.quoted}) FROM {tq}",
                    f"SELECT {c.quoted} FROM {tq} WHERE {c.quoted} IS NOT NULL "
                    f"ORDER BY {c.quoted} DESC LIMIT 1",
                    "max", difficulty=2),
                Candidate(
                    S.key,
                    f"How many distinct {c.name} values are in {t.name}?",
                    f"SELECT count(DISTINCT {c.quoted}) FROM {tq}",
                    f"SELECT count(*) FROM (SELECT DISTINCT {c.quoted} FROM {tq} "
                    f"WHERE {c.quoted} IS NOT NULL) q",
                    "count_distinct_numeric", difficulty=3),
                Candidate(
                    S.key,
                    f"Show {t.name} where {c.name} is at least zero.",
                    f"SELECT * FROM {tq} WHERE {c.quoted} >= 0",
                    f"SELECT * FROM {tq} WHERE NOT ({c.quoted} < 0) "
                    f"AND {c.quoted} IS NOT NULL",
                    "numeric_gte", difficulty=2),
            ])

            # Ranking questions deliberately require the complete row shape.
            # This directly trains against the held-out failure where a model
            # returned a plausible subset/join instead of SELECT *.
            for n in (1, 2, 3, 5):
                out.append(Candidate(
                    S.key,
                    f"What are the top {n} {t.name} by {c.name}?",
                    f"SELECT * FROM {tq} ORDER BY {c.quoted} DESC NULLS LAST LIMIT {n}",
                    f"SELECT * FROM {tq} ORDER BY {c.quoted} DESC NULLS LAST LIMIT {n}",
                    "top_n", difficulty=2))
                out.append(Candidate(
                    S.key,
                    f"What are the bottom {n} {t.name} by {c.name}?",
                    f"SELECT * FROM {tq} ORDER BY {c.quoted} ASC NULLS LAST LIMIT {n}",
                    f"SELECT * FROM {tq} ORDER BY {c.quoted} ASC NULLS LAST LIMIT {n}",
                    "bottom_n", difficulty=2))

        # --- richer text semantics ------------------------------------------
        for g in textual:
            out.append(Candidate(
                S.key,
                f"What distinct {g.name} values occur in {t.name}?",
                f"SELECT DISTINCT {g.quoted} FROM {tq} ORDER BY {g.quoted} NULLS LAST",
                f"SELECT {g.quoted} FROM {tq} GROUP BY {g.quoted} "
                f"ORDER BY {g.quoted} NULLS LAST",
                "distinct_text", difficulty=2))
            out.append(Candidate(
                S.key,
                f"How many distinct {g.name} values are set in {t.name}?",
                f"SELECT count(DISTINCT {g.quoted}) FROM {tq}",
                f"SELECT count(*) FROM (SELECT DISTINCT {g.quoted} FROM {tq} "
                f"WHERE {g.quoted} IS NOT NULL) q",
                "count_distinct_text", difficulty=3))

        # --- date/timestamp semantics ---------------------------------------
        # Never add unrelated predicates to date questions. Multiple windows
        # teach clean temporal filtering and protect against invented filters.
        for d in dated:
            for year in (2025, 2026):
                out.append(Candidate(
                    S.key,
                    f"Which {t.name} happened in {year}?",
                    f"SELECT * FROM {tq} WHERE {d.quoted} >= '{year}-01-01' "
                    f"AND {d.quoted} < '{year + 1}-01-01'",
                    f"SELECT * FROM {tq} WHERE extract(year from {d.quoted}) = {year}",
                    "date_range", difficulty=3))
                out.append(Candidate(
                    S.key,
                    f"How many {t.name} happened in {year}?",
                    f"SELECT count(*) FROM {tq} WHERE {d.quoted} >= '{year}-01-01' "
                    f"AND {d.quoted} < '{year + 1}-01-01'",
                    f"SELECT count(*) FROM {tq} "
                    f"WHERE extract(year from {d.quoted}) = {year}",
                    "date_count", difficulty=3))
            out.append(Candidate(
                S.key,
                f"Which {t.name} happened in March 2026?",
                f"SELECT * FROM {tq} WHERE {d.quoted} >= '2026-03-01' "
                f"AND {d.quoted} < '2026-04-01'",
                f"SELECT * FROM {tq} WHERE extract(year from {d.quoted}) = 2026 "
                f"AND extract(month from {d.quoted}) = 3",
                "month_range", difficulty=3))

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
            out.append(Candidate(
                S.key,
                f"How many related {right.name} rows does each {left.name} have?",
                f"SELECT l.id, count(r.{fk.quoted}) FROM {left.quoted} l "
                f"LEFT JOIN {right.quoted} r ON r.{fk.quoted} = l.id GROUP BY l.id",
                f"SELECT l.id, (SELECT count(*) FROM {right.quoted} r2 "
                f"WHERE r2.{fk.quoted} = l.id) FROM {left.quoted} l",
                "join_count_projection", difficulty=4))
            out.append(Candidate(
                S.key,
                f"Which {left.name} rows have at least one related {right.name}?",
                f"SELECT l.* FROM {left.quoted} l WHERE EXISTS "
                f"(SELECT 1 FROM {right.quoted} r WHERE r.{fk.quoted} = l.id)",
                f"SELECT l.* FROM {left.quoted} l WHERE l.id IN "
                f"(SELECT r.{fk.quoted} FROM {right.quoted} r "
                f"WHERE r.{fk.quoted} IS NOT NULL)",
                "join_exists", difficulty=4))

    rng.shuffle(out)
    return out


def teacher_candidates(schema: Schema, rng: random.Random, generate,
                       limit: int = 32, prompt_style: str = "ddl") -> tuple[list[Candidate], int]:
    """Ask a larger model for alternate SQL, then let L4 decide whether it lives.

    The teacher never supplies truth. Reference SQL comes from the deterministic
    template generator; the teacher only proposes a different implementation for
    the same question. Malformed/refusal outputs are counted and skipped here.
    Semantic mistakes survive only until the normal execution gate rejects them.
    """
    bases = [x for x in template_candidates(schema, rng) if x.reference_sql]
    rng.shuffle(bases)
    bases = bases[:max(0, limit)]
    out: list[Candidate] = []
    malformed = 0

    for base in bases:
        text = generate(build_prompt(schema, base.question, prompt_style))
        blk = extract_one(text)
        if blk is None or blk.kind != "SQL" or not blk.payload.strip():
            malformed += 1
            continue
        if any(marker in blk.payload for marker in ("Ġ", "Ċ", "▁")):
            malformed += 1
            continue
        out.append(Candidate(
            schema_key=schema.key,
            question=base.question,
            sql=blk.payload.strip(),
            reference_sql=base.reference_sql,
            kind="teacher",
            difficulty=max(base.difficulty, 3),
        ))
    return out, malformed


# ----------------------------------------------------------------- prompt fmt --

SYSTEM = (
    "You are Imagine's PostgreSQL compiler. Follow this contract exactly.\n"
    "\n"
    "OUTPUT FORMAT — MANDATORY:\n"
    "- Return exactly ONE sentinel block and absolutely nothing before or after it.\n"
    "- Never use Markdown fences, prose, explanations, labels, or commentary.\n"
    "- Valid forms are only:\n"
    "  <<<SQL>>>\n<one read-only PostgreSQL statement>\n<<<END>>>\n"
    "  <<<UNANSWERABLE>>>\n<short reason>\n<<<END>>>\n"
    "  <<<CLARIFY>>>\n<one precise question>\n<<<END>>>\n"
    "\n"
    "SQL SAFETY:\n"
    "- SQL must be exactly one SELECT or WITH statement.\n"
    "- Never write INSERT, UPDATE, DELETE, MERGE, COPY, CALL, DO, DDL, SET, or multiple statements.\n"
    "- Never access pg_catalog, information_schema, server files, extensions, sleeps, or admin functions.\n"
    "\n"
    "SCHEMA GROUNDING:\n"
    "- Use ONLY tables and columns explicitly present in the supplied schema.\n"
    "- Never invent, rename, infer, or assume a missing table or column.\n"
    "- Preserve quoted/case-sensitive identifiers exactly when required.\n"
    "- If the schema cannot answer the question, use UNANSWERABLE.\n"
    "- If the question has multiple materially different interpretations, use CLARIFY.\n"
    "\n"
    "CORRECTNESS:\n"
    "- Answer the user's actual question, not merely a syntactically valid approximation.\n"
    "- Respect NULL semantics, aggregation semantics, requested ordering, limits, dates, and literal case.\n"
    "- Prefer the simplest query that is correct on the supplied schema.\n"
    "\n"
    "FINAL CHECK BEFORE RESPONDING:\n"
    "1. Exactly one sentinel block?\n"
    "2. No text outside it?\n"
    "3. Only schema-provided identifiers?\n"
    "4. Read-only single statement?\n"
    "5. Does the query answer the question exactly?\n"
    "If any check fails, fix the response before emitting it."
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
          rejects_path: str | None = None, teacher_generate=None,
          teacher_per_schema: int = 0) -> dict:
    """Run the full forge. Returns a stats dict.

    Rejects are written too, and that is not an afterthought: the rejected
    pile is where you find out your generator is broken. A forge that only
    reports admissions can be 90% wrong and look perfect.
    """
    keys = list(schema_keys or TRAIN_KEYS)
    rng = random.Random(seed)
    workers = workers or min(cpu_count(), 32)

    print(f"* materialising {len(keys)} schemas")
    for k in keys:
        materialise(CATALOG[k], admin_dsn)
        print(f"  {k:12} ready")

    cands: list[Candidate] = []
    teacher_malformed = 0
    for k in keys:
        templ = template_candidates(CATALOG[k], rng)
        cands.extend(templ)
        teacher_n = 0
        if teacher_generate is not None and teacher_per_schema > 0:
            taught, malformed = teacher_candidates(
                CATALOG[k], rng, teacher_generate,
                limit=teacher_per_schema, prompt_style=prompt_style,
            )
            cands.extend(taught)
            teacher_n = len(taught)
            teacher_malformed += malformed
        suffix = f" + {teacher_n} teacher" if teacher_generate is not None else ""
        print(f"  {k:12} {len(templ):5d} template{suffix}")
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
        "teacher_candidates": sum(1 for r in recs if r["kind"] == "teacher"),
        "teacher_malformed": teacher_malformed,
        "rejected_by_stop_level": by_level,
        "out": out_path,
    }
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="stealth corpus forge")
    ap.add_argument("--dsn", default=os.environ.get(
        "STEALTH_ADMIN_DSN",
        f"host={os.path.join(os.getcwd(), 'pgrun')} user=stealth dbname=postgres"))
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
