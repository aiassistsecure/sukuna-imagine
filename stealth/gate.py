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
  L2 BIND      Two parts. First, the gate's OWN schema catalog: NEDB is schemaless
               by design, so EXPLAIN cannot catch hallucinated tables, hallucinated
               columns, ambiguous references or type errors -- it plans them all.
               The gate walks the parse tree against its catalog and kills them
               here, in PostgreSQL's own vocabulary. Second, EXPLAIN against the
               live engine for whatever the catalog cannot see.
  L3 EXECUTE   run it under a statement timeout and a row cap.
  L4 AGREE     compare the result set against a reference query's result set.

L4 is the one that makes the corpus trustworthy. A pair is admitted only when
the candidate SQL produces THE SAME ANSWER as the reference on real data. That
is execution accuracy used as an entry gate rather than as a post-hoc metric.

Why not string comparison of SQL? Because `SELECT count(*)` and
`SELECT count(id)` are different strings and the same answer, while
`WHERE status='paid'` and `WHERE status='Paid'` are nearly the same string and
different answers. Strings measure the wrong thing.

THE WRITE PATH (run_write). Reads are verified by answer; writes are verified
by effect. NEDB is append-only -- no transactions, no rollback, no truncate --
so a write cannot be tested in place and unwound. Each side gets a FRESH
database from an identical starting state; the candidate is admitted only when
both end in identical state. L1 runs in write mode (exactly one
INSERT/UPDATE/DELETE, same denylist, no DDL). The append-only history is what
makes the comparison sound.
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

from stealth.schema import SHOP_SCHEMA, check_schema


class Level(IntEnum):
    REJECTED = -1
    PARSE = 0
    SAFETY = 1
    BIND = 2
    EXECUTE = 3
    AGREE = 4


# Statement node types libpg_query reports for read-only queries.
_READ_ONLY_STMTS = {"SelectStmt"}

# Statement node types for the write path. Exactly one of these, no DDL,
# no multi-statements. The write gate verifies by effect, not by result set.
_WRITE_STMTS = {"InsertStmt", "UpdateStmt", "DeleteStmt"}

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


def check_safety_write(sql: str, kinds: list[str]) -> tuple[bool, str]:
    """L1 for the write path: exactly one INSERT/UPDATE/DELETE.

    Same denylist as the read path. DDL stays out -- the write gate is for
    DML only. Multi-statements stay out -- one write, one verification.
    """
    if len(kinds) != 1:
        return False, f"expected exactly 1 statement, got {len(kinds)}"
    if kinds[0] not in _WRITE_STMTS:
        return False, f"not a write statement: {kinds[0]}"
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
                 max_rows: int = 2000, schema: dict | None = None):
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms
        self.max_rows = max_rows
        # The gate's own catalog. NEDB stays schemaless; the strictness lives here.
        self.schema = SHOP_SCHEMA if schema is None else schema
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
        """Schema-catalog validation first, EXPLAIN second.

        The catalog check is deterministic and needs no round trip; it is also
        the only thing that can catch hallucinations, because NEDB is
        schemaless and EXPLAIN will plan anything. EXPLAIN stays as the
        backstop for whatever the catalog cannot see.
        """
        ok, reason = check_schema(sql, self.schema)
        if not ok:
            return False, reason
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

    # -- write pipeline -----------------------------------------------------
    def run_write(self, sql: str, reference_sql: str, fresh_dsn,
                  tables: list[tuple[str, list[str]]],
                  catalog: dict | None = None) -> GateResult:
        """Verify a write by effect, not by result set.

        NEDB is append-only: no transactions, no rollback, no truncate. So a
        write cannot be tested in place and unwound. Instead each side gets a
        FRESH database: `fresh_dsn(tag)` must return a DSN for a newly
        materialised database (`"cand"` for the candidate, `"ref"` for the
        reference). Both start from identical state; the write is admitted
        only if both end in identical state.

        This is the write analogue of L4: the candidate and the reference
        agree when they produce the same effect on the same starting state.
        The append-only history is what makes the comparison sound -- no
        write can be lost or silently reordered between the two runs.
        """
        import time
        t0 = time.perf_counter()

        def done(ok, level, reason, **kw):
            return GateResult(ok=ok, level=level, reason=reason,
                              elapsed_ms=round((time.perf_counter() - t0) * 1000, 2),
                              **kw)

        ok, reason, kinds = check_parse(sql)
        if not ok:
            return done(False, Level.REJECTED, f"L0 parse: {reason}")

        ok, reason = check_safety_write(sql, kinds)
        if not ok:
            return done(False, Level.PARSE, f"L1 safety: {reason}")

        ok, reason = check_schema(sql, catalog if catalog is not None else self.schema)
        if not ok:
            return done(False, Level.SAFETY, f"L2 bind: {reason}")

        # Reference gets the same three levels. A reference that cannot pass
        # the gate is a generator bug, not a candidate bug.
        rok, rreason, rkinds = check_parse(reference_sql)
        if not rok:
            return done(False, Level.PARSE, f"REFERENCE L0 parse: {rreason}")
        rok, rreason = check_safety_write(reference_sql, rkinds)
        if not rok:
            return done(False, Level.PARSE, f"REFERENCE L1 safety: {rreason}")
        rok, rreason = check_schema(reference_sql, catalog if catalog is not None else self.schema)
        if not rok:
            return done(False, Level.SAFETY, f"REFERENCE L2 bind: {rreason}")

        ok, reason, cand_state = self._run_write_side(fresh_dsn("cand"), sql, tables)
        if not ok:
            return done(False, Level.BIND, f"L3 execute: {reason}")

        ok, reason, ref_state = self._run_write_side(fresh_dsn("ref"), reference_sql, tables)
        if not ok:
            return done(False, Level.EXECUTE,
                        f"L4 agree: REFERENCE write failed: {reason}")

        if cand_state != ref_state:
            return done(False, Level.EXECUTE,
                        f"L4 agree: state mismatch "
                        f"(candidate {cand_state} vs reference {ref_state})")

        return done(True, Level.AGREE, "ok", result_digest=cand_state)

    def _run_write_side(self, dsn: str, sql: str,
                        tables: list[tuple[str, list[str]]]) -> tuple[bool, str, str | None]:
        """Execute one write on a fresh database; digest the end state.

        `tables` is (quoted_name, [columns_to_keep]) per table. Auto-generated
        id columns are excluded by the caller: NEDB's `_id` is not
        deterministic across fresh databases, so two identical writes would
        digest differently if ids were included. The effect lives in the
        business columns.
        """
        try:
            con = psycopg2.connect(dsn)
            con.autocommit = True
            try:
                with con.cursor() as cur:
                    cur.execute("SET statement_timeout = %s",
                                (self.statement_timeout_ms,))
                    cur.execute(sql)
            finally:
                con.close()
        except psycopg2.Error as e:
            msg = (e.pgerror or str(e)).strip().splitlines()
            return False, msg[0] if msg else str(e), None

        # Digest every table. Unordered: without ORDER BY the engine makes no
        # ordering promise, and two equivalent writes may land rows in a
        # different physical sequence.
        try:
            con = psycopg2.connect(dsn)
            try:
                h = hashlib.sha256()
                for t, keep in tables:
                    cols = ", ".join(keep) if keep else "*"
                    with con.cursor() as cur:
                        cur.execute(f"SELECT {cols} FROM {t}")
                        rows = cur.fetchmany(self.max_rows + 1)
                        if len(rows) > self.max_rows:
                            return False, f"table {t} exceeded max_rows", None
                        h.update(digest_rows([tuple(r) for r in rows],
                                            ordered=False).encode())
                        h.update(b"\x1f")
            finally:
                con.close()
        except psycopg2.Error as e:
            msg = (e.pgerror or str(e)).strip().splitlines()
            return False, f"state dump: {msg[0] if msg else str(e)}", None
        return True, "ok", h.hexdigest()[:32]
