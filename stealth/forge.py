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

READ/WRITE. The forge mints both. Read candidates go through the gate's
`run()` (verified by answer); write candidates (INSERT/UPDATE/DELETE) go
through `run_write()` (verified by effect on fresh databases). The corpus is
read/write because the model being trained is.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.request
from urllib.parse import quote
from dataclasses import dataclass, asdict
from datetime import date
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
            cur.execute("SELECT version()")
            if "nedb" in str(cur.fetchone()[0]).lower():
                return _materialise_nedb(schema, dbname)
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


def schema_catalog(schema: Schema) -> dict:
    """Give each forge/eval gate its own declared schema, including empty tables."""
    def kind(typ):
        typ = typ.lower()
        if "serial" in typ or "int" in typ:
            return "int"
        if typ.startswith(("numeric", "decimal", "real", "double")):
            return "numeric"
        if typ.startswith("timestamp"):
            return "timestamp"
        if typ.startswith("date"):
            return "date"
        if typ.startswith("bool"):
            return "bool"
        return "text"
    return {t.name: {c.name: kind(c.type) for c in t.columns} for t in schema.tables}


def _materialise_nedb(schema: Schema, dbname: str) -> str:
    """Rebuild only forge-owned databases; seed typed documents without SQL DDL."""
    if not dbname.startswith("stealth_"):
        raise ValueError("NEDB materialisation may only replace stealth_* databases")
    base = os.environ.get("STEALTH_NEDB_URL", "http://127.0.0.1:7070").rstrip("/")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("STEALTH_NEDB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    def request(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read() or b"null")
    path = "/v1/databases/" + quote(dbname, safe="")
    # Validate row widths before replacing an existing fixture database.
    schema.seed()
    request("DELETE", path)
    request("POST", "/v1/databases", {"name": dbname})
    for table in schema.tables:
        columns = [c for c in table.columns if "serial" not in c.type.lower()]
        for index, values in enumerate(table.rows, 1):
            doc = dict(zip([c.name for c in columns], values))
            for column in table.columns:
                if "serial" in column.type.lower():
                    doc[column.name] = index
            key = doc.get(table.primary_key) if table.primary_key else None
            request("POST", path + "/put", {
                "coll": table.name, "id": str(index if key is None else key), "doc": doc})
        if not table.rows:
            # A tombstone registers the collection without leaving a fake row.
            key = "__stealth_empty_fixture__"
            request("POST", path + "/put", {
                "coll": table.name, "id": key,
                "doc": {c.name: None for c in table.columns}})
            request("DELETE", path + "/rows/" + quote(table.name, safe="") + "/" + key)
    result = request("GET", path + "/verify")
    if result.get("ok") is not True:
        raise RuntimeError(f"NEDB fixture verification failed: {result}")
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
    is_write: bool = False      # True -> verified by run_write (effect), not run (answer)


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

        # Contrast row counts, populated cells, and unique populated values.
        for c in cols:
            if c.name == t.primary_key:
                continue
            for question in (
                f"How many {t.name} have {c.name} set?",
                f"Count {t.name} rows with a non-null {c.name}.",
            ):
                out.append(Candidate(
                    S.key, question, f"SELECT count({c.quoted}) FROM {tq}",
                    f"SELECT count(*) FROM {tq} WHERE {c.quoted} IS NOT NULL",
                    "count_non_null", difficulty=2))
            out.append(Candidate(
                S.key, f"How many {t.name} have no {c.name} set?",
                f"SELECT count(*) FROM {tq} WHERE {c.quoted} IS NULL",
                f"SELECT count(*) - count({c.quoted}) FROM {tq}",
                "count_null", difficulty=2))
            out.append(Candidate(
                S.key, f"How many different non-null {c.name} values occur in {t.name}?",
                f"SELECT count(DISTINCT {c.quoted}) FROM {tq}",
                f"SELECT count(*) FROM (SELECT {c.quoted} FROM {tq} "
                f"WHERE {c.quoted} IS NOT NULL GROUP BY {c.quoted}) q",
                "count_distinct", difficulty=3))

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
            # Skip min/max when the column has no non-null seed values: on an
            # empty (or all-NULL) column, SELECT min(x) returns one NULL row
            # while the ORDER BY ... LIMIT 1 reference returns zero rows.
            # Seed rows omit serial columns, so index into the non-serial list.
            nonserial = [cc for cc in cols if "serial" not in cc.type.lower()]
            if t.rows and c.name in [cc.name for cc in nonserial]:
                idx = [cc.name for cc in nonserial].index(c.name)
                if not any(r[idx] is not None for r in t.rows):
                    continue
            elif not t.rows:
                continue
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

        # Explicit date columns keep multi-date schemas unambiguous. Half-open
        # ranges include midnight at the start and exclude the next period.
        for d in dated:
            for year in (2024, 2025, 2026, 2027):
                windows = [(f"{year}", date(year, 1, 1), date(year + 1, 1, 1),
                            f"extract(year from {d.quoted}) = {year}")]
                for month in range(1, 13):
                    start = date(year, month, 1)
                    end = date(year + (month == 12), month % 12 + 1, 1)
                    windows.append((start.strftime("%B %Y"), start, end,
                                    f"extract(year from {d.quoted}) = {year} AND "
                                    f"extract(month from {d.quoted}) = {month}"))
                for label, start, end, ref_pred in windows:
                    predicate = f"{d.quoted} >= '{start}' AND {d.quoted} < '{end}'"
                    for projection, question, kind in (
                        ("*", f"Show all {t.name} with {d.name} in {label}.", "date_range"),
                        ("count(*)", f"How many {t.name} have {d.name} in {label}?", "date_count"),
                    ):
                        out.append(Candidate(
                            S.key, question,
                            f"SELECT {projection} FROM {tq} WHERE {predicate}",
                            f"SELECT {projection} FROM {tq} WHERE {ref_pred}",
                            kind, difficulty=3))
                    # Pair an unfiltered window with an explicitly requested
                    # business filter; never infer one for the broad question.
                    for c in textual[:1]:
                        for value in _seed_col_values(t).get(c.name, [])[:2]:
                            lit = _lit(value)
                            out.append(Candidate(
                                S.key, f"Show {t.name} with {d.name} in {label} "
                                f"where {c.name} is {lit}.",
                                f"SELECT * FROM {tq} WHERE {predicate} AND {c.quoted} = {lit}",
                                f"SELECT * FROM {tq} WHERE {ref_pred} AND {c.quoted} = {lit}",
                                "date_filtered", difficulty=3))

    # Relationships are declared, never guessed from an arbitrary *_id field.
    for parent_name, child_name, fk_name in S.relationships:
        left = next(t for t in S.tables if t.name == parent_name)
        right = next(t for t in S.tables if t.name == child_name)
        pk = next(c for c in left.columns if c.name == left.primary_key)
        fk = next(c for c in right.columns if c.name == fk_name)
        scalar = (f"(SELECT count(*) FROM {right.quoted} r "
                  f"WHERE r.{fk.quoted} = l.{pk.quoted})")
        for question in (
            f"Show each {left.name} ID alongside its related {right.name} count, including zero counts.",
            f"How many related {right.name} rows does each {left.name} ID have? Include IDs with none.",
        ):
            out.append(Candidate(
                S.key, question, f"SELECT l.{pk.quoted}, {scalar} FROM {left.quoted} l",
                f"SELECT l.{pk.quoted}, count(r.{fk.quoted}) FROM {left.quoted} l "
                f"LEFT JOIN {right.quoted} r ON r.{fk.quoted} = l.{pk.quoted} "
                f"GROUP BY l.{pk.quoted}", "join_count", difficulty=4))
        columns = ", ".join(f"l.{c.quoted}" for c in left.columns)
        out.append(Candidate(
            S.key, f"Show each {left.name} row alongside its related {right.name} count, including zero counts.",
            f"SELECT {columns}, {scalar} FROM {left.quoted} l",
            f"SELECT {columns}, {scalar} FROM {left.quoted} l",
            "join_count_full_row", difficulty=4))
        out.append(Candidate(
            S.key, f"Which {left.name} rows have at least one related {right.name}?",
            f"SELECT l.* FROM {left.quoted} l WHERE EXISTS "
            f"(SELECT 1 FROM {right.quoted} r WHERE r.{fk.quoted} = l.{pk.quoted})",
            f"SELECT l.* FROM {left.quoted} l WHERE l.{pk.quoted} IN "
            f"(SELECT r.{fk.quoted} FROM {right.quoted} r WHERE r.{fk.quoted} IS NOT NULL)",
            "join_exists", difficulty=4))
        out.append(Candidate(
            S.key, f"Which {left.name} rows have no related {right.name}?",
            f"SELECT l.* FROM {left.quoted} l WHERE NOT EXISTS "
            f"(SELECT 1 FROM {right.quoted} r WHERE r.{fk.quoted} = l.{pk.quoted})",
            f"SELECT l.* FROM {left.quoted} l WHERE l.{pk.quoted} NOT IN "
            f"(SELECT r.{fk.quoted} FROM {right.quoted} r WHERE r.{fk.quoted} IS NOT NULL)",
            "join_absent", difficulty=4))

    rng.shuffle(out)
    return out


def analytical_template_candidates(schema: Schema, rng: random.Random) -> list[Candidate]:
    """Complex analytical query templates for v10.

    Targets v9's failure mode on multi-clause analytical queries:
    LEFT JOIN + GROUP BY + COALESCE + complex ORDER BY, subqueries,
    window functions, HAVING, multi-table JOINs.
    """
    out: list[Candidate] = []
    S = schema

    # Find parent-child relationships via the schema's declared relationships
    # (name-heuristic matching fails on plural table names, e.g.
    # "customers" vs "customer_id" -- see predicate_placement_candidates).
    for t in S.tables:
        tq = t.quoted
        cols = t.columns
        numeric = [c for c in cols if any(k in c.type for k in ("int", "numeric", "serial", "real", "double"))
                   and c.name != "id"]
        # Find tables that reference this one (children)
        children = []
        for parent_name, child_name, fk_name in S.relationships:
            if parent_name != t.name:
                continue
            child = next((ot for ot in S.tables if ot.name == child_name), None)
            if child is None:
                continue
            fk = next((c for c in child.columns if c.name == fk_name), None)
            if fk is None:
                continue
            children.append((child, fk))

        if not children or not numeric:
            continue

        child, fk = children[0]
        cq = child.quoted
        num_col = numeric[0]

        # --- Category 1: Aggregation with LEFT JOIN (the v9 failure mode) ---
        out.append(Candidate(
            S.key,
            f"List every {t.name} ID, name, and total {num_col.name} from {child.name}. Include {t.name}s with no {child.name}s (show 0). Sort by total descending, then ID ascending.",
            f"SELECT t.id, t.name, COALESCE(SUM(c.{num_col.quoted}), 0) AS total "
            f"FROM {tq} t LEFT JOIN {cq} c ON t.id = c.{fk.quoted} "
            f"GROUP BY t.id, t.name ORDER BY total DESC, t.id ASC",
            f"SELECT t.id, t.name, COALESCE(SUM(c.{num_col.quoted}), 0) AS total "
            f"FROM {tq} AS t LEFT JOIN {cq} AS c ON c.{fk.quoted} = t.id "
            f"GROUP BY t.id, t.name ORDER BY 3 DESC, 1 ASC",
            "analytical_left_join_agg", difficulty=5))

        # --- Category 2: GROUP BY with HAVING ---
        out.append(Candidate(
            S.key,
            f"Find {t.name}s with more than 2 related {child.name}s. Show ID, name, and count. Sort by count descending.",
            f"SELECT t.id, t.name, COUNT(c.id) AS cnt FROM {tq} t "
            f"JOIN {cq} c ON t.id = c.{fk.quoted} "
            f"GROUP BY t.id, t.name HAVING COUNT(c.id) > 2 ORDER BY cnt DESC",
            f"SELECT t.id, t.name, COUNT(*) AS cnt FROM {tq} t, {cq} c "
            f"WHERE t.id = c.{fk.quoted} GROUP BY t.id, t.name "
            f"HAVING COUNT(*) > 2 ORDER BY 3 DESC",
            "analytical_having", difficulty=5))

        # --- Category 3: Subquery comparison ---
        if len(numeric) >= 1:
            out.append(Candidate(
                S.key,
                f"Find {t.name}s whose total {num_col.name} exceeds the average.",
                f"SELECT id, name, total FROM (SELECT t.id, t.name, SUM(c.{num_col.quoted}) AS total "
                f"FROM {tq} t JOIN {cq} c ON t.id = c.{fk.quoted} GROUP BY t.id, t.name) sub "
                f"WHERE total > (SELECT AVG(total) FROM (SELECT SUM({num_col.quoted}) AS total "
                f"FROM {cq} GROUP BY {fk.quoted}) avg_sub) ORDER BY total DESC",
                f"SELECT t.id, t.name, SUM(c.{num_col.quoted}) AS total FROM {tq} t "
                f"JOIN {cq} c ON t.id = c.{fk.quoted} GROUP BY t.id, t.name "
                f"HAVING SUM(c.{num_col.quoted}) > (SELECT AVG(s) FROM "
                f"(SELECT SUM({num_col.quoted}) AS s FROM {cq} GROUP BY {fk.quoted}) x)",
                "analytical_subquery", difficulty=6))

    rng.shuffle(out)
    return out


def predicate_placement_candidates(schema: Schema, rng: random.Random) -> list[Candidate]:
    """v11: WHERE-vs-ON predicate placement on outer joins.

    Targets v10's live failure mode (2026-10-07): asked for "every X with
    total Y from <status> Zs, include Xs with none (show 0)", v10 emitted
    LEFT JOIN ... WHERE child.status = '<status>' -- the WHERE runs after
    the join, silently converting it to an inner join and dropping the
    zero-rows the question explicitly asked for. The predicate belongs in
    the ON clause (or the aggregation must be conditional).
    """
    out: list[Candidate] = []
    S = schema

    for t in S.tables:
        tq = t.quoted
        cols = t.columns
        # Parent display column: prefer `name`, else first text column.
        name_col = next((c for c in cols if c.name == "name"), None)
        if name_col is None:
            name_col = next((c for c in cols if "text" in c.type), None)
        if name_col is None:
            continue
        # Children via the schema's declared relationships (parent, child, fk).
        # (Name-heuristic matching fails on plural table names, e.g.
        # "customers" vs "customer_id", so trust the declaration.)
        children = []
        for parent_name, child_name, fk_name in S.relationships:
            if parent_name != t.name:
                continue
            child = next((ot for ot in S.tables if ot.name == child_name), None)
            if child is None:
                continue
            fk = next((c for c in child.columns if c.name == fk_name), None)
            if fk is None:
                continue
            children.append((child, fk))
        if not children:
            continue
        child, fk = children[0]
        cq = child.quoted
        numeric = [c for c in child.columns
                   if any(k in c.type for k in ("int", "numeric", "serial", "real", "double"))
                   and c.name not in ("id", fk.name)]
        # Status-like column: text with pipe-separated allowed values in note.
        status_col = next((c for c in child.columns
                           if "text" in c.type and "|" in (c.note or "")), None)
        if not numeric or status_col is None:
            continue
        num_cols = numeric[:2]
        nq = name_col.quoted
        sq = status_col.quoted
        status_vals = [v.strip() for v in status_col.note.split("|")][:3]

        for num in num_cols:
            for status_val in status_vals:
                # --- Category 1: predicate in ON clause (the v10 failure, fixed) ---
                out.append(Candidate(
                    S.key,
                    f"List every {t.name} ID, {name_col.name}, and total {num.name} from {status_val} {child.name}. "
                    f"Include {t.name}s with no {status_val} {child.name}s (show 0). "
                    f"Sort by total descending, then ID ascending.",
                    f"SELECT t.id, t.{nq}, COALESCE(SUM(c.{num.quoted}), 0) AS total "
                    f"FROM {tq} t LEFT JOIN {cq} c ON t.id = c.{fk.quoted} AND c.{sq} = '{status_val}' "
                    f"GROUP BY t.id, t.{nq} ORDER BY total DESC, t.id ASC",
                    f"SELECT t.id, t.{nq}, SUM(COALESCE(c.{num.quoted}, 0)) AS total "
                    f"FROM {tq} AS t LEFT JOIN {cq} AS c ON c.{fk.quoted} = t.id AND c.{sq} = '{status_val}' "
                    f"GROUP BY t.id, t.{nq} ORDER BY 3 DESC, 1 ASC",
                    "predicate_on_clause", difficulty=6))

                # --- Category 2: conditional aggregation (equivalent, no ON filter) ---
                out.append(Candidate(
                    S.key,
                    f"For each {t.name}, show ID, {name_col.name}, and the sum of {num.name} over {status_val} {child.name} only. "
                    f"{t.name}s with none show 0. Sort by the sum descending.",
                    f"SELECT t.id, t.{nq}, SUM(CASE WHEN c.{sq} = '{status_val}' THEN c.{num.quoted} ELSE 0 END) AS total "
                    f"FROM {tq} t LEFT JOIN {cq} c ON t.id = c.{fk.quoted} "
                    f"GROUP BY t.id, t.{nq} ORDER BY total DESC",
                    f"SELECT t.id, t.{nq}, COALESCE(SUM(c.{num.quoted}), 0) AS total "
                    f"FROM {tq} t LEFT JOIN {cq} c ON t.id = c.{fk.quoted} AND c.{sq} = '{status_val}' "
                    f"GROUP BY t.id, t.{nq} ORDER BY 3 DESC",
                    "predicate_conditional_agg", difficulty=6))

                # --- Category 3: anti-join via NOT EXISTS (reference: LEFT JOIN + IS NULL) ---
                out.append(Candidate(
                    S.key,
                    f"Find {t.name}s with no {status_val} {child.name}s. Show ID and {name_col.name}. Sort by ID ascending.",
                    f"SELECT t.id, t.{nq} FROM {tq} t WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {cq} c WHERE c.{fk.quoted} = t.id AND c.{sq} = '{status_val}') "
                    f"ORDER BY t.id ASC",
                    f"SELECT t.id, t.{nq} FROM {tq} t LEFT JOIN {cq} c "
                    f"ON t.id = c.{fk.quoted} AND c.{sq} = '{status_val}' "
                    f"WHERE c.id IS NULL ORDER BY 1 ASC",
                    "predicate_anti_join", difficulty=6))

    rng.shuffle(out)
    return out


def _lit(v) -> str:
    """Render a Python value as a SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _seed_col_values(table) -> dict[str, list]:
    """Map column name -> distinct non-null seed values for that column."""
    insertable = [c for c in table.columns if "serial" not in c.type.lower()]
    vals: dict[str, list] = {c.name: [] for c in insertable}
    for row in table.rows:
        for c, v in zip(insertable, row):
            if v is not None and v not in vals[c.name]:
                vals[c.name].append(v)
    return vals


def write_template_candidates(schema: Schema, rng: random.Random,
                              per_table: int = 170) -> list[Candidate]:
    """Deterministic write generators at scale. Perfect labels by construction.

    Every generator emits a candidate and an equivalent-but-differently-
    written reference. The gate verifies by EFFECT: both run on fresh
    databases from identical state, and the pair is admitted only when both
    end in identical state. Serial columns are omitted from INSERTs -- the
    database generates them, identically on both fresh sides.

    Generates ~per_table writes per table, combinatorially across columns,
    values, and predicate forms. Relational writes use the schema's declared
    relationships.
    """
    out: list[Candidate] = []
    S = schema

    # Declared relationships: (parent, child, fk_col)
    rel_map: dict[str, list[tuple[str, str]]] = {}  # child -> [(parent, fk)]
    for parent_name, child_name, fk_name in S.relationships:
        rel_map.setdefault(child_name, []).append((parent_name, fk_name))

    for t in S.tables:
        if not t.rows:
            continue
        tq = t.quoted
        insertable = [c for c in t.columns if "serial" not in c.type.lower()]
        if not insertable:
            continue
        colvals = _seed_col_values(t)
        numeric = [c for c in insertable
                   if any(k in c.type for k in ("int", "numeric"))]
        textual = [c for c in insertable if c.type.startswith("text")]
        dated = [c for c in insertable if "date" in c.type]
        singular = t.name[:-1] if t.name.endswith("s") else t.name
        n0 = len(out)

        def synth_val(col, base_val):
            """Generate a synthetic variant of a seed value for SET clauses."""
            if isinstance(base_val, (int, float)) and not isinstance(base_val, bool):
                return base_val + rng.choice([1, -1, 10, -10, 100, 2, 5, 50])
            elif isinstance(base_val, str):
                return base_val + rng.choice([" Jr", " II", " (updated)", " X", " Pro"])
            return base_val

        def cap() -> bool:
            return len(out) - n0 >= per_table

        # --- inserts: variants of each seed row --------------------------
        for row in t.rows:
            if cap():
                break
            variants = [row]
            for _ in range(24):
                lst = list(row)
                i = rng.randrange(len(lst))
                v = lst[i]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    lst[i] = v + rng.choice([1, -1, 10, -10, 100, 2, 5])
                elif isinstance(v, str):
                    lst[i] = v + rng.choice([" Jr", " Sr", " II", " (new)",
                                             " X", " Pro", " Lite"])
                variants.append(tuple(lst))
            for vrow in variants:
                if cap():
                    break
                vals = [_lit(v) for v in vrow]
                cols_fwd = ", ".join(c.quoted for c in insertable)
                vals_fwd = ", ".join(vals)
                order = list(range(len(insertable)))
                rng.shuffle(order)
                cols_rev = ", ".join(insertable[i].quoted for i in order)
                vals_rev = ", ".join(vals[i] for i in order)
                desc = ", ".join(f"{c.name} {_lit(v)}"
                                 for c, v in list(zip(insertable, vrow))[:2])
                out.append(Candidate(
                    S.key, f"Add a new {singular} with {desc}.",
                    f"INSERT INTO {tq} ({cols_fwd}) VALUES ({vals_fwd})",
                    f"INSERT INTO {tq} ({cols_rev}) VALUES ({vals_rev})",
                    "insert_single", difficulty=2, is_write=True))

        # --- updates: set-col x predicate --------------------------------
        for c in numeric:
            if cap():
                break
            if not colvals.get(c.name):
                continue
            for tc in textual:
                if cap():
                    break
                for pick in colvals.get(tc.name, []):
                    if cap():
                        break
                    base_v = rng.choice(colvals[c.name])
                    for new_v in [base_v, synth_val(c, base_v),
                                  synth_val(c, base_v)]:
                        if cap():
                            break
                        out.append(Candidate(
                            S.key,
                            f"Set {c.name} to {_lit(new_v)} for {t.name} "
                            f"where {tc.name} is {_lit(pick)}.",
                            f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                            f"WHERE {tc.quoted} = {_lit(pick)}",
                            f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                            f"WHERE {tc.quoted} IN ({_lit(pick)})",
                            "update_where", difficulty=2, is_write=True))
        for c in textual:
            if cap():
                break
            if not colvals.get(c.name):
                continue
            for tc in textual:
                if tc.name == c.name or cap():
                    continue
                for pick in colvals.get(tc.name, [])[:2]:
                    if cap():
                        break
                    new_v = rng.choice(colvals[c.name])
                    out.append(Candidate(
                        S.key,
                        f"Set {c.name} to {_lit(new_v)} for {t.name} "
                        f"where {tc.name} is {_lit(pick)}.",
                        f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                        f"WHERE {tc.quoted} = {_lit(pick)}",
                        f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                        f"WHERE {tc.quoted} IN ({_lit(pick)})",
                        "update_where", difficulty=3, is_write=True))

        # --- deletes -----------------------------------------------------
        for tc in textual + dated:
            if cap():
                break
            for pick in colvals.get(tc.name, []):
                if cap():
                    break
                out.append(Candidate(
                    S.key, f"Remove {t.name} where {tc.name} is {_lit(pick)}.",
                    f"DELETE FROM {tq} WHERE {tc.quoted} = {_lit(pick)}",
                    f"DELETE FROM {tq} WHERE {tc.quoted} IN ({_lit(pick)})",
                    "delete_where", difficulty=2, is_write=True))
        if len(textual) >= 2 and not cap():
            tc1, tc2 = textual[0], textual[1]
            for p1 in colvals.get(tc1.name, [])[:2]:
                if cap():
                    break
                for p2 in colvals.get(tc2.name, [])[:2]:
                    if cap():
                        break
                    out.append(Candidate(
                        S.key,
                        f"Remove {t.name} where {tc1.name} is {_lit(p1)} "
                        f"and {tc2.name} is {_lit(p2)}.",
                        f"DELETE FROM {tq} WHERE {tc1.quoted} = {_lit(p1)} "
                        f"AND {tc2.quoted} = {_lit(p2)}",
                        f"DELETE FROM {tq} WHERE {tc2.quoted} = {_lit(p2)} "
                        f"AND {tc1.quoted} = {_lit(p1)}",
                        "delete_where", difficulty=3, is_write=True))

        # --- multi-column updates ----------------------------------------
        if len(numeric) >= 2 and not cap():
            c1, c2 = numeric[0], numeric[1]
            if colvals.get(c1.name) and colvals.get(c2.name):
                for tc in textual[:1]:
                    if cap():
                        break
                    for pick in colvals.get(tc.name, [])[:2]:
                        if cap():
                            break
                        v1 = rng.choice(colvals[c1.name])
                        v2 = rng.choice(colvals[c2.name])
                        out.append(Candidate(
                            S.key,
                            f"Set {c1.name} to {_lit(v1)} and {c2.name} to "
                            f"{_lit(v2)} for {t.name} where {tc.name} "
                            f"is {_lit(pick)}.",
                            f"UPDATE {tq} SET {c1.quoted} = {_lit(v1)}, "
                            f"{c2.quoted} = {_lit(v2)} "
                            f"WHERE {tc.quoted} = {_lit(pick)}",
                            f"UPDATE {tq} SET {c2.quoted} = {_lit(v2)}, "
                            f"{c1.quoted} = {_lit(v1)} "
                            f"WHERE {tc.quoted} IN ({_lit(pick)})",
                            "update_where", difficulty=3, is_write=True))

        # --- updates with OR predicates ----------------------------------
        if textual and numeric and not cap():
            for c in numeric[:2]:
                if cap() or not colvals.get(c.name):
                    break
                for i, tc1 in enumerate(textual[:2]):
                    if cap():
                        break
                    for tc2 in textual[i+1:i+2]:
                        if cap():
                            break
                        p1 = rng.choice(colvals[tc1.name]) if colvals.get(tc1.name) else None
                        p2 = rng.choice(colvals[tc2.name]) if colvals.get(tc2.name) else None
                        if p1 is None or p2 is None:
                            continue
                        new_v = rng.choice(colvals[c.name])
                        out.append(Candidate(
                            S.key,
                            f"Set {c.name} to {_lit(new_v)} for {t.name} "
                            f"where {tc1.name} is {_lit(p1)} or {tc2.name} "
                            f"is {_lit(p2)}.",
                            f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                            f"WHERE {tc1.quoted} = {_lit(p1)} "
                            f"OR {tc2.quoted} = {_lit(p2)}",
                            f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                            f"WHERE {tc2.quoted} = {_lit(p2)} "
                            f"OR {tc1.quoted} = {_lit(p1)}",
                            "update_where", difficulty=3, is_write=True))

        # --- deletes with OR predicates ----------------------------------
        if len(textual) >= 2 and not cap():
            tc1, tc2 = textual[0], textual[1]
            for p1 in colvals.get(tc1.name, [])[:3]:
                if cap():
                    break
                for p2 in colvals.get(tc2.name, [])[:3]:
                    if cap():
                        break
                    out.append(Candidate(
                        S.key,
                        f"Remove {t.name} where {tc1.name} is {_lit(p1)} "
                        f"or {tc2.name} is {_lit(p2)}.",
                        f"DELETE FROM {tq} WHERE {tc1.quoted} = {_lit(p1)} "
                        f"OR {tc2.quoted} = {_lit(p2)}",
                        f"DELETE FROM {tq} WHERE {tc2.quoted} = {_lit(p2)} "
                        f"OR {tc1.quoted} = {_lit(p1)}",
                        "delete_where", difficulty=3, is_write=True))

        # --- relational writes via declared relationships -----------------
        for parent_name, fk_name in rel_map.get(t.name, []):
            if cap():
                break
            parent = next(p for p in S.tables if p.name == parent_name)
            pq = parent.quoted
            fk_q = f'"{fk_name}"' if " " in fk_name else fk_name
            n_parents = len(parent.rows)
            if not n_parents:
                continue
            pid = rng.randint(1, n_parents)
            # insert with FK
            if not cap() and t.rows:
                row = rng.choice(t.rows)
                vals = [_lit(v) for v in row]
                try:
                    fi = [c.name for c in insertable].index(fk_name)
                    vals[fi] = str(pid)
                except ValueError:
                    pass
                cols_fwd = ", ".join(c.quoted for c in insertable)
                order = list(range(len(insertable)))
                rng.shuffle(order)
                cols_rev = ", ".join(insertable[i].quoted for i in order)
                vals_rev = ", ".join(vals[i] for i in order)
                out.append(Candidate(
                    S.key,
                    f"Add a new {singular} linked to {parent_name} {pid}.",
                    f"INSERT INTO {tq} ({cols_fwd}) VALUES ({', '.join(vals)})",
                    f"INSERT INTO {tq} ({cols_rev}) VALUES ({vals_rev})",
                    "insert_fk", difficulty=3, is_write=True))
            # update via parent subquery
            if numeric and not cap():
                c = rng.choice(numeric)
                new_v = rng.choice(colvals[c.name]) if colvals.get(c.name) else 0
                out.append(Candidate(
                    S.key,
                    f"Set {c.name} to {_lit(new_v)} for {t.name} "
                    f"linked to {parent_name} {pid}.",
                    f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                    f"WHERE {fk_q} IN (SELECT id FROM {pq} WHERE id = {pid})",
                    f"UPDATE {tq} SET {c.quoted} = {_lit(new_v)} "
                    f"WHERE {fk_q} = {pid}",
                    "update_fk", difficulty=4, is_write=True))
            # delete via parent subquery
            if not cap():
                out.append(Candidate(
                    S.key,
                    f"Remove {t.name} linked to {parent_name} {pid}.",
                    f"DELETE FROM {tq} WHERE {fk_q} IN "
                    f"(SELECT id FROM {pq} WHERE id = {pid})",
                    f"DELETE FROM {tq} WHERE {fk_q} = {pid}",
                    "delete_fk", difficulty=4, is_write=True))

    rng.shuffle(out)
    seen = set()
    uniq = []
    for c in out:
        if c.sql not in seen:
            seen.add(c.sql)
            uniq.append(c)
    return uniq


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
                 max_rows=_WORKER["max_rows"], schema=schema_catalog(CATALOG[schema_key]))
        g.connect()
        gates[schema_key] = g
    return gates[schema_key]


def _catalog_dict(schema: Schema) -> dict:
    """Build the gate's catalog dict from a Schema. Types are simplified to
    the gate's vocabulary (int/text/numeric/date/timestamp/bool)."""
    from .schema import _PG_TYPE_MAP
    out = {}
    for t in schema.tables:
        cols = {}
        for c in t.columns:
            base = c.type.split()[0].lower()
            cols[c.name] = _PG_TYPE_MAP.get(base, base)
        out[t.name] = cols
    return out


def _fresh_dsn(schema_key: str, tag: str) -> str:
    """Materialise a fresh database for one side of a write verification.

    Fixed names per worker+tag: materialise drops first, so nothing
    accumulates across candidates.
    """
    admin_dsn = _WORKER["admin_dsn"]
    dbname = f"stealth_{schema_key}_w_{os.getpid()}_{tag}"
    materialise(CATALOG[schema_key], admin_dsn, dbname)
    return _swap_db(admin_dsn, dbname)


def _process(c: Candidate) -> dict:
    # Workers must never raise: an exception holding a DB connection
    # (BufferedReader) cannot be pickled back to the parent, which masks
    # the real error as MaybeEncodingError. Convert to a reject record.
    try:
        return _process_inner(c)
    except Exception as e:  # noqa: BLE001
        return {
            "schema_key": c.schema_key,
            "question": c.question,
            "sql": c.sql,
            "kind": c.kind,
            "difficulty": c.difficulty,
            "admitted": False,
            "level": 0,
            "reason": f"WORKER: {type(e).__name__}: {e}",
            "rowcount": None,
            "result_digest": None,
        }


def _process_inner(c: Candidate) -> dict:
    g = _gate_for(c.schema_key)
    if c.is_write:
        schema = CATALOG[c.schema_key]
        # Keep only business columns for the state digest: serial/id
        # columns are auto-generated and not deterministic across fresh
        # databases, so including them would fail identical writes.
        tables = []
        for t in schema.tables:
            keep = [col.quoted for col in t.columns
                    if "serial" not in col.type.lower()
                    and col.name not in ("id", "_id")]
            tables.append((t.quoted, keep))
        catalog = _catalog_dict(schema)
        r = g.run_write(c.sql, c.reference_sql,
                        fresh_dsn=lambda tag: _fresh_dsn(c.schema_key, tag),
                        tables=tables, catalog=catalog)
    else:
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
        analyt = analytical_template_candidates(CATALOG[k], rng)
        cands.extend(analyt)
        pred = predicate_placement_candidates(CATALOG[k], rng)
        cands.extend(pred)
        writes = write_template_candidates(CATALOG[k], rng)
        cands.extend(writes)
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
        print(f"  {k:12} {len(templ):5d} template + {len(analyt):3d} analytical + {len(pred):3d} predicate + {len(writes):3d} write{suffix}")
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
