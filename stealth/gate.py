"""stealth :: the execution gate

The single most important component. Every training pair and every eval verdict
passes through here, and nothing enters the corpus that this refuses.

Carried from nedb-cast-slm: do not write a verifier. The engine already shipped
one. Here the verifier is PostgreSQL itself -- its real parser (libpg_query via
pglast) and a real server holding real rows.

FIVE LEVELS, each strictly stronger than the last:

  L0 PARSE     libpg_query accepts it as PostgreSQL. Not a regex. Not a
               "looks like SQL" heuristic. The actual grammar.
  L1 SAFETY    read-only and bounded: exactly one statement, SELECT/WITH only,
               no DDL/DML, no system catalogs, no pg_sleep, no COPY/file access.
  L2 BIND      EXPLAIN it against the live schema. This is where hallucinated
               tables, hallucinated columns, ambiguous references and type
               errors die -- BEFORE any row is read.
  L3 EXECUTE   run it under a statement timeout and a row cap.
  L4 AGREE     compare the result set against a reference query's result set.

L4 is the one that makes the corpus trustworthy. A pair is admitted only when
the candidate SQL produces THE SAME ANSWER as the reference on real data. That
is execution accuracy used as an entry gate rather than as a post-hoc metric.

Why not string comparison of SQL? Because `SELECT count(*)` and
`SELECT count(id)` are different strings and the same answer, while
`WHERE status='paid'` and `WHERE status='Paid'` are nearly the same string and
different answers. Strings measure the wrong thing.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import IntEnum

import psycopg2
import psycopg2.extras
from pglast import parse_sql
from pglast.parser import ParseError


class Level(IntEnum):
    REJECTED = -1
    PARSE = 0
    SAFETY = 1
    BIND = 2
    EXECUTE = 3
    AGREE = 4


# Statement node types libpg_query reports for read-only queries.
_READ_ONLY_STMTS = {"SelectStmt"}

# Substring denylist applied to the LOWERCASED sql. Coarse on purpose: this is
# a corpus forge, not a security boundary. The real boundary is the database
# role, which must be read-only (see connect()).
_DENY = (
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "lo_import", "lo_export", "copy ", "dblink", "pg_terminate_backend",
    "pg_cancel_backend", "current_setting('", "set_config",
    "pg_catalog.", "information_schema.", "pg_stat_", "pg_shadow",
    "pg_authid", "pg_user", "pg_roles",
)


@dataclass
class GateResult:
    ok: bool
    level: Level                    # highest level PASSED
    reason: str = ""
    rowcount: int | None = None
    result_digest: str | None = None
    columns: list[str] = field(default_factory=list)
    sample: list[tuple] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["level"] = int(self.level)
        d["sample"] = [list(r) for r in self.sample]
        return d


# ----------------------------------------------------------------- L0 parse --

def check_parse(sql: str) -> tuple[bool, str, list[str]]:
    """PostgreSQL's own grammar. Returns (ok, reason, statement_node_types)."""
    if not sql or not sql.strip():
        return False, "empty sql", []
    try:
        tree = parse_sql(sql)
    except ParseError as e:
        return False, f"ParseError: {e}", []
    except Exception as e:                       # pglast raises a few shapes
        return False, f"{type(e).__name__}: {e}", []
    if not tree:
        return False, "parsed to zero statements", []
    kinds = []
    for raw in tree:
        stmt = raw.stmt
        kinds.append(type(stmt).__name__)
    return True, "ok", kinds


# ---------------------------------------------------------------- L1 safety --

def check_safety(sql: str, kinds: list[str]) -> tuple[bool, str]:
    if len(kinds) != 1:
        return False, f"expected exactly 1 statement, got {len(kinds)}"
    if kinds[0] not in _READ_ONLY_STMTS:
        return False, f"not a read-only statement: {kinds[0]}"
    low = sql.lower()
    for bad in _DENY:
        if bad in low:
            return False, f"denylisted construct: {bad.strip()!r}"
    return True, "ok"


# ------------------------------------------------------- result canonicalise --

def _canon_value(v):
    """Make values comparable across equivalent-but-differently-typed results.

    count(*) may come back int while a SUM comes back Decimal; 1 and 1.0 are
    the same answer. Floats are rounded to 6 places so that two mathematically
    equal expressions computed differently still agree.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, float):
        if v == int(v):
            return int(v)
        return round(v, 6)
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return str(v)


def digest_rows(rows: list[tuple], ordered: bool) -> str:
    """Stable digest of a result set.

    ordered=False sorts the rows first, because two queries that answer the
    same question without an ORDER BY may legitimately return the same rows in
    a different sequence -- and PostgreSQL makes no ordering promise without
    ORDER BY. When the reference query DOES specify an order, sequence is part
    of the answer and we keep it.
    """
    canon = [tuple(_canon_value(c) for c in r) for r in rows]
    if not ordered:
        canon.sort(key=lambda r: tuple((v is None, str(v)) for v in r))
    h = hashlib.sha256()
    for r in canon:
        h.update(repr(r).encode())
        h.update(b"\x1e")
    return h.hexdigest()[:32]


def _has_order_by(sql: str) -> bool:
    try:
        tree = parse_sql(sql)
        stmt = tree[0].stmt
        return bool(getattr(stmt, "sortClause", None))
    except Exception:
        return "order by" in sql.lower()


# ------------------------------------------------------------------ runner --

class Gate:
    """Runs the levels against one live database.

    One Gate instance == one connection == one worker. The forge creates one
    per process; psycopg2 connections are not thread-safe to share.
    """

    def __init__(self, dsn: str, statement_timeout_ms: int = 5000,
                 max_rows: int = 2000):
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms
        self.max_rows = max_rows
        self._conn = None

    def connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.dsn)
            self._conn.set_session(readonly=True, autocommit=True)
            with self._conn.cursor() as cur:
                cur.execute("SET statement_timeout = %s",
                            (self.statement_timeout_ms,))
        return self._conn

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()

    # -- L2 -----------------------------------------------------------------
    def check_bind(self, sql: str) -> tuple[bool, str]:
        """EXPLAIN without ANALYZE: plans the query, touches no rows.

        This is where a hallucinated column dies. It is far cheaper than
        executing, and it produces PostgreSQL's own error message, which is
        exactly the feedback a repair loop wants to hand back to the model.
        """
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("EXPLAIN " + sql)
                cur.fetchall()
            return True, "ok"
        except psycopg2.Error as e:
            msg = (e.pgerror or str(e)).strip().splitlines()
            return False, msg[0] if msg else str(e)

    # -- L3 -----------------------------------------------------------------
    def execute(self, sql: str) -> tuple[bool, str, list[str], list[tuple]]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return False, "statement returned no result set", [], []
                cols = [d.name for d in cur.description]
                rows = cur.fetchmany(self.max_rows + 1)
                if len(rows) > self.max_rows:
                    return False, f"exceeded max_rows={self.max_rows}", cols, []
                return True, "ok", cols, rows
        except psycopg2.Error as e:
            msg = (e.pgerror or str(e)).strip().splitlines()
            return False, msg[0] if msg else str(e), [], []

    # -- full pipeline ------------------------------------------------------
    def run(self, sql: str, reference_sql: str | None = None) -> GateResult:
        import time
        t0 = time.perf_counter()

        def done(ok, level, reason, **kw):
            return GateResult(ok=ok, level=level, reason=reason,
                              elapsed_ms=round((time.perf_counter() - t0) * 1000, 2),
                              **kw)

        ok, reason, kinds = check_parse(sql)
        if not ok:
            return done(False, Level.REJECTED, f"L0 parse: {reason}")

        ok, reason = check_safety(sql, kinds)
        if not ok:
            return done(False, Level.PARSE, f"L1 safety: {reason}")

        ok, reason = self.check_bind(sql)
        if not ok:
            return done(False, Level.SAFETY, f"L2 bind: {reason}")

        ok, reason, cols, rows = self.execute(sql)
        if not ok:
            return done(False, Level.BIND, f"L3 execute: {reason}")

        ordered = _has_order_by(sql)
        digest = digest_rows(rows, ordered)

        if reference_sql is None:
            return done(True, Level.EXECUTE, "ok", rowcount=len(rows),
                        result_digest=digest, columns=cols, sample=rows[:5])

        rok, rreason, _rcols, rrows = self.execute(reference_sql)
        if not rok:
            return done(False, Level.EXECUTE,
                        f"L4 agree: REFERENCE query failed: {rreason}",
                        rowcount=len(rows), result_digest=digest,
                        columns=cols, sample=rows[:5])

        # Order matters only if the REFERENCE asked for an order.
        ref_ordered = _has_order_by(reference_sql)
        use_ordered = ordered and ref_ordered
        if digest_rows(rows, use_ordered) != digest_rows(rrows, use_ordered):
            return done(False, Level.EXECUTE,
                        f"L4 agree: result mismatch "
                        f"(candidate {len(rows)} rows, reference {len(rrows)} rows)",
                        rowcount=len(rows), result_digest=digest,
                        columns=cols, sample=rows[:5])

        return done(True, Level.AGREE, "ok", rowcount=len(rows),
                    result_digest=digest, columns=cols, sample=rows[:5])
