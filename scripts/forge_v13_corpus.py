#!/usr/bin/env python3
"""forge_v13_corpus.py — Gate-4 admission forge for the sukuna-imagine v13 corpus.

Every candidate row from the four v13 generators (PR #29 tool-use builder,
discrimination, temporal, tool-protocol) passes through here. A row is
admitted only if EVERY applicable check passes; rejects are written with the
failing check named. Admitted rows are emitted with ``_verify`` stripped, in
generator order, deterministically. Admission stops at ``--target``; the rest
of the candidate file is counted as unprocessed, honestly, in the stats.

Checks (per the v13 build spec):
  1. Protocol gate (all rows): every assistant turn is exactly one
     END-terminated envelope parsed by toolproto.parser, a permitted grounded
     plain-text summary, or a write proposal (<<<SQL>>> envelope + exactly one
     authorization line). TOOL_CALL -> TOOL_RESULT relay names must match;
     <= 3 tool calls per row. NOTE (spec section 6 divergence): the PR #29
     builder emits the JSON-schema argument names ``max_rows`` / ``key`` where
     the normative toolproto registry uses ``limit`` / ``row_id``. Those two
     names are pure aliases (same types, same semantics), so the forge
     normalises them FOR VALIDATION ONLY before calling the parser; row
     content is never rewritten, and every normalised row is counted in the
     stats (``alias_normalised_rows``). All other parser failures reject.
  2. SQL execution: stealth.gate L0-L4 against the reference on the row's
     fixture DB (catalog schemas -> stealth_<key>). Temporal rows get
     symmetric clock injection (CURRENT_DATE / CURRENT_TIMESTAMP in BOTH
     candidate and reference replaced by the row's fixture instant) plus a
     Python (zoneinfo) bounds cross-check, reported in the stats.
  3. Tool/chain rows: host simulation — sql.execute / sql.explain arguments
     executed in chain order on the fixture DB (read-only via the gate);
     schema.inspect / nedb.provenance arguments must name real tables;
     summary facts must appear in TOOL_RESULT payloads or executed results;
     write proposals must carry no tool-executed write and are verified by
     the gate's run_write effect-equivalence path.
  4. PR #29 builder rows (no _verify): protocol gate + per-SQL gate L0/L1 and
     execution on the catalog schema whose table set matches the prompt DDL
     (no unique match -> L0/L1 + parser only, tier=parse); terminal-summary
     numbers must appear in the row's own TOOL_RESULT payloads or prompt.

Verification tiers (reported per family, never silently upgraded):
  execution    — at least one SQL check passed by real execution (L3/L4 or
                 run_write) and no check fell back to the deterministic tier.
  deterministic— all checks passed, but at least one relied on deterministic
                 verification because the NEDB engine cannot execute the
                 construct (its NQL translator refuses INTERVAL / date_trunc /
                 TIMESTAMPTZ / DATE literals): pglast L0 parse + L1 safety +
                 gate-catalog bind (stealth.schema.check_schema) + pglast AST
                 equivalence vs the reference modulo AND/OR operand order,
                 or an unmaterialisable custom fixture verified the same way.
  parse        — builder rows with no unique catalog match: protocol gate +
                 gate L0/L1 only.

Custom fixtures (schema_key temporal:asof / temporal:datecol /
protocol:custom, or _verify.fixture_ddl): the catalog is built from the
fixture DDL / prompt DDL and the row is verified at the deterministic tier
unless its SQL is plain enough for the engine to execute on a materialised
scratch DB (attempted only for catalog-free fixtures whose SQL carries no
known-unsupported construct).

Usage:
  python scripts/forge_v13_corpus.py --family discrimination \
      --candidates cand.jsonl --out admitted.jsonl --rejects rejects.jsonl \
      --stats stats.json --target 19710 [--pair-level]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from toolproto import parser as toolproto  # noqa: E402
from stealth.gate import Gate, check_parse, check_safety, check_safety_write  # noqa: E402
from stealth.schema import check_schema  # noqa: E402
from stealth.forge import schema_catalog, materialise, _swap_db  # noqa: E402
from stealth.schemas import CATALOG  # noqa: E402

# DSNs come from the environment the setup script establishes, never a
# hardcoded port: ADMIN from STEALTH_ADMIN_DSN, BASE from STEALTH_DSN
# (its dbname is stripped — callers append the per-schema stealth_<key>
# database). Defaults match the setup script: NEDB pgwire on 5433,
# admin against the postgres maintenance database.
ADMIN_DSN = os.environ.get(
    "STEALTH_ADMIN_DSN",
    "host=127.0.0.1 port=5433 user=stealth dbname=postgres")
BASE_DSN = re.sub(r"\s*dbname=\S+", "", os.environ.get("STEALTH_DSN", "")).strip() \
    or "host=127.0.0.1 port=5433 user=stealth"

RELAY_RE = re.compile(r'^<<<TOOL_RESULT name="([^"]+)" status="(ok|error)">>>\n(.*)\n<<<END>>>$', re.S)
WRITE_AUTH_RE = re.compile(r"^<<<SQL>>>\n(.*)\n<<<END>>>\n([^\n]+)$", re.S)
CREATE_TABLE_RE = re.compile(r"CREATE TABLE\s+(?:\"([^\"]+)\"|(\w+))\s*\((.*?)\)\s*;", re.S | re.I)

# Constructs the NEDB NQL translator demonstrably refuses (probed 2026-10-31).
UNSUPPORTED_HINTS = ("INTERVAL", "DATE_TRUNC", "TIMESTAMPTZ", " DATE '", "CURRENT_DATE", "CURRENT_TIMESTAMP")


def alias_normalise_args(name: str, args: dict) -> dict:
    """Spec section 6 aliases: max_rows->limit (sql.execute), key->row_id
    (nedb.provenance). Validation only; row content is never rewritten."""
    out = dict(args)
    if name == "sql.execute" and "max_rows" in out and "limit" not in out:
        out["limit"] = out.pop("max_rows")
    if name == "nedb.provenance" and "key" in out and "row_id" not in out:
        out["row_id"] = out.pop("key")
    return out


def parse_tool_call_content(content: str):
    """Parse one assistant turn that should be a TOOL_CALL envelope.

    Returns (payload_dict, used_alias, error)."""
    res = toolproto.parse(content)
    if res.ok and res.kind == toolproto.TOOL_CALL:
        return res.payload, False, None
    # Retry with alias-normalised arguments (validation only).
    m = re.match(r"\s*<<<TOOL_CALL>>>\s*(\{.*\})\s*<<<END>>>\s*$", content, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None, False, res.error
        if isinstance(data, dict) and isinstance(data.get("arguments"), dict):
            norm = dict(data)
            norm["arguments"] = alias_normalise_args(str(data.get("name")), data["arguments"])
            if norm["arguments"] != data["arguments"]:
                rebuilt = "<<<TOOL_CALL>>>\n" + json.dumps(norm) + "\n<<<END>>>"
                res2 = toolproto.parse(rebuilt)
                if res2.ok and res2.kind == toolproto.TOOL_CALL:
                    return res2.payload, True, None
    return None, False, res.error


# ------------------------------------------------------------ AST equivalence
def _canon(node):
    """Canonical form of a pglast AST: BoolExpr (AND/OR) argument order is
    normalised away; everything else is structural."""
    from pglast import ast as pgast

    if isinstance(node, pgast.Node):
        fields = []
        for slot in node.__slots__:
            if slot == "location":
                continue  # parse offsets differ under operand reordering
            fields.append((slot, _canon(getattr(node, slot))))
        if type(node).__name__ == "BoolExpr":
            d = dict(fields)
            if "args" in d:
                d["args"] = tuple(sorted(d["args"], key=repr))
            fields = sorted(d.items())
        return (type(node).__name__, tuple(fields))
    if isinstance(node, (list, tuple)):
        return tuple(_canon(x) for x in node)
    if isinstance(node, (str, int, float, bool)) or node is None:
        return node
    return repr(node)


def ast_equivalent(sql_a: str, sql_b: str) -> bool:
    from pglast import parse_sql

    try:
        ta = parse_sql(sql_a)
        tb = parse_sql(sql_b)
    except Exception:
        return False
    if len(ta) != len(tb):
        return False
    return all(_canon(a.stmt) == _canon(b.stmt) for a, b in zip(ta, tb))


# ------------------------------------------------------------ prompt catalogs
def _kind_of(type_word: str) -> str:
    t = type_word.lower()
    if "serial" in t or "int" in t:
        return "int"
    if t.startswith(("numeric", "decimal", "real", "double")):
        return "numeric"
    if t.startswith("timestamp"):
        return "timestamp"
    if t.startswith("date"):
        return "date"
    if t.startswith("bool"):
        return "bool"
    return "text"


def catalog_from_ddl_text(text: str) -> dict:
    """{table: {column: kind}} from CREATE TABLE statements in text."""
    catalog: dict[str, dict[str, str]] = {}
    for m in CREATE_TABLE_RE.finditer(text or ""):
        table = m.group(1) or m.group(2)
        body = m.group(3)
        cols: dict[str, str] = {}
        depth = 0
        parts, cur = [], ""
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        if cur.strip():
            parts.append(cur)
        for part in parts:
            part = part.strip()
            if not part or part.upper().startswith(("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT")):
                continue
            cm = re.match(r'(?:"([^"]+)"|(\w+))\s+(\w+)', part)
            if cm:
                cols[cm.group(1) or cm.group(2)] = _kind_of(cm.group(3))
        if cols:
            catalog[table] = cols
    return catalog


def prompt_table_set(text: str) -> set[str]:
    return set(catalog_from_ddl_text(text).keys())


CATALOG_TABLE_SETS = {k: {t.name for t in s.tables} for k, s in CATALOG.items()}
CATALOG_DICTS = {k: schema_catalog(s) for k, s in CATALOG.items()}


# ------------------------------------------------------------ row extraction
def extract_row(row: dict):
    """Returns dict with calls, relays, envelopes, summaries, problems."""
    msgs = row.get("messages") or []
    calls = []       # (msg_idx, name, arguments, used_alias)
    relays = []      # (msg_idx, name, status, payload_text)
    envelopes = []   # (msg_idx, kind, payload_text) SQL/CLARIFY/UNANSWERABLE
    summaries = []   # (msg_idx, text) plain assistant turns
    write_proposals = []  # (msg_idx, sql, auth_line)
    problems = []
    alias_used = False
    for i, m in enumerate(msgs):
        role, content = m.get("role"), m.get("content") or ""
        if role == "user" and content.startswith("<<<TOOL_RESULT"):
            rm = RELAY_RE.match(content)
            if not rm:
                problems.append("protocol_relay")
            else:
                relays.append((i, rm.group(1), rm.group(2), rm.group(3)))
            continue
        if role != "assistant":
            continue
        if "<<<" not in content:
            summaries.append((i, content))
            continue
        wm = WRITE_AUTH_RE.match(content)
        if wm and "authorization" in wm.group(2):
            envelopes.append((i, "SQL", wm.group(1)))
            write_proposals.append((i, wm.group(1).strip(), wm.group(2)))
            continue
        if "<<<TOOL_CALL>>>" in content:
            payload, used_alias, err = parse_tool_call_content(content)
            if err is not None:
                problems.append("protocol_parse")
            else:
                alias_used = alias_used or used_alias
                calls.append((i, payload["name"], payload["arguments"], used_alias))
            continue
        res = toolproto.parse(content)
        if not res.ok:
            problems.append("protocol_parse")
        elif res.kind == toolproto.TOOL_CALL:
            calls.append((i, res.payload["name"], res.payload["arguments"], False))
        else:
            envelopes.append((i, res.kind, res.payload))
    # Relay pairing: relays must follow calls in order with matching names.
    if len(relays) > len(calls):
        problems.append("protocol_relay")
    else:
        for j, (_ci, name, _st, _pl) in enumerate(relays):
            if calls[j][1] != name:
                problems.append("protocol_relay")
                break
    if len(calls) > 3:
        problems.append("protocol_calls_gt3")
    return {
        "calls": calls, "relays": relays, "envelopes": envelopes,
        "summaries": summaries, "write_proposals": write_proposals,
        "problems": problems, "alias_used": alias_used,
    }


class Forge:
    def __init__(self, family: str):
        self.family = family
        self.gates: dict[str, Gate] = {}
        self.exec_cache: dict[tuple, tuple] = {}
        self.stats_extra = Counter()
        self.write_counter = 0

    def gate_for(self, schema_key: str, catalog: dict | None = None) -> Gate:
        if schema_key not in self.gates:
            dsn = f"{BASE_DSN} dbname=stealth_{schema_key}"
            self.gates[schema_key] = Gate(dsn, schema=catalog or CATALOG_DICTS[schema_key])
        return self.gates[schema_key]

    def gate_run(self, schema_key: str, sql: str, reference_sql: str | None = None):
        """Cached gate.run -> (ok, reason, level_int, rows_text)."""
        key = (schema_key, sql, reference_sql)
        if key in self.exec_cache:
            return self.exec_cache[key]
        gate = self.gate_for(schema_key)
        r = gate.run(sql, reference_sql=reference_sql)
        rows_text = ""
        if r.sample:
            rows_text = json.dumps([list(x) for x in r.sample], default=str)
        out = (r.ok, r.reason or "", int(r.level), rows_text)
        self.exec_cache[key] = out
        return out

    def executed_rows_text(self, schema_key: str, sql: str) -> str:
        gate = self.gate_for(schema_key)
        ok, _reason, _cols, rows = gate.execute(sql)
        return json.dumps(rows, default=str) if ok else ""

    # -- deterministic fallback for one SQL vs reference -------------------
    def deterministic_sql_check(self, sql: str, reference_sql: str | None,
                                catalog: dict) -> tuple[bool, str]:
        ok, reason, _kinds = check_parse(sql)
        if not ok:
            return False, "gate_parse"
        ok, reason = check_safety(sql, _kinds)
        if not ok:
            return False, "gate_safety"
        ok, reason = check_schema(sql, catalog)
        if not ok:
            return False, "gate_bind"
        if reference_sql is not None and not ast_equivalent(sql, reference_sql):
            return False, "ast_mismatch"
        return True, ""

    def verify_sql_vs_reference(self, schema_key: str, sql: str,
                                reference_sql: str | None, catalog: dict):
        """Returns (ok, reason, tier) tier in {execution, deterministic}."""
        if schema_key in CATALOG_DICTS:
            ok, reason, level, _rt = self.gate_run(schema_key, sql, reference_sql)
            if ok:
                return True, "", "execution"
            if "L4 agree: result mismatch" in reason:
                return False, "gate_agree", "execution"
            if reason.startswith("L0"):
                return False, "gate_parse", "execution"
            if reason.startswith("L1"):
                return False, "gate_safety", "execution"
            # L2/L3 failure: separate a real bind failure (gate catalog) from
            # an engine limitation (NQL translator / EXPLAIN on NEDB).
            cok, _cr = check_schema(sql, catalog)
            if not cok:
                return False, "gate_bind", "execution"
            if reference_sql is not None:
                rok, _rr = check_schema(reference_sql, catalog)
                if not rok:
                    return False, "gate_bind_reference", "execution"
            det_ok, det_reason = self.deterministic_sql_check(sql, reference_sql, catalog)
            if det_ok:
                return True, "", "deterministic"
            return False, det_reason, "deterministic"
        det_ok, det_reason = self.deterministic_sql_check(sql, reference_sql, catalog)
        if det_ok:
            return True, "", "deterministic"
        return False, det_reason, "deterministic"

    # -- write verification --------------------------------------------------
    def verify_write(self, schema_key: str, sql: str, reference_sql: str | None,
                     catalog: dict):
        """(ok, reason, tier)."""
        ok, reason, kinds = check_parse(sql)
        if not ok:
            return False, "gate_parse", "deterministic"
        ok, reason = check_safety_write(sql, kinds)
        if not ok:
            return False, "gate_safety", "deterministic"
        ok, reason = check_schema(sql, catalog)
        if not ok:
            return False, "gate_bind", "deterministic"
        if reference_sql is None or schema_key not in CATALOG:
            if reference_sql is not None and not ast_equivalent(sql, reference_sql):
                return False, "ast_mismatch", "deterministic"
            return True, "", "deterministic"
        schema = CATALOG[schema_key]
        tables = [(t.name, [c.name for c in t.columns if "serial" not in c.type.lower()])
                  for t in schema.tables]
        self.write_counter += 1
        gate = self.gate_for(schema_key, catalog)

        def fresh(side: str) -> str:
            # Two fixed scratch DBs, rebuilt by materialise on every call:
            # an earlier build created ~2,000 one-shot scratch DBs and
            # bloated the NEDB data dir; reuse keeps the count constant.
            dbname = f"stealth_fw_{side}"
            materialise(schema, ADMIN_DSN, dbname=dbname)
            return _swap_db(ADMIN_DSN, dbname)

        try:
            r = gate.run_write(sql, reference_sql, fresh, tables, catalog=catalog)
        except Exception:
            if not ast_equivalent(sql, reference_sql):
                return False, "ast_mismatch", "deterministic"
            return True, "", "deterministic"
        if r.ok:
            return True, "", "execution"
        if "state mismatch" in (r.reason or ""):
            return False, "write_effect", "execution"
        if not ast_equivalent(sql, reference_sql):
            return False, "ast_mismatch", "deterministic"
        return True, "", "deterministic"


# ------------------------------------------------------------ temporal bounds
def _parse_instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def recompute_bounds(family: str, now_utc: str, zone: str):
    """Expected [start, end) window for derivable families, else None."""
    try:
        now_local = _parse_instant(now_utc).astimezone(ZoneInfo(zone))
    except Exception:
        return None
    today = now_local.date()
    day = timedelta(days=1)
    if family == "this_month":
        start = today.replace(day=1)
        return start, today + day
    if family == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today + day
    if family == "quarter_to_date_ordinary":
        start = today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
        return start, today + day
    if family == "prev_quarter":
        qstart = today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
        prev_end = qstart
        m = qstart.month - 3
        y = qstart.year + (m - 1) // 12
        prev_start = prev_end.replace(year=y, month=(m - 1) % 12 + 1, day=1)
        return prev_start, prev_end
    if family == "previous_year":
        return today.replace(year=today.year - 1, month=1, day=1), today.replace(month=1, day=1)
    if family == "year_to_date":
        return today.replace(month=1, day=1), today + day
    if family == "month_end_rollover":
        this_start = today.replace(day=1)
        m = this_start.month - 1
        y = this_start.year + (m - 1) // 12
        prev_start = this_start.replace(year=y, month=(m - 1) % 12 + 1, day=1)
        return prev_start, this_start
    return None


def temporal_bounds_check(forge: "Forge", row: dict, verify: dict, cand_sql: str) -> str | None:
    """Returns a reject reason or None. Recompute mismatches are soft
    (reported cross-check); literal bounds in the SQL contradicting
    _verify.expected_bounds are a hard inconsistency."""
    expected = verify.get("expected_bounds")
    if not expected or len(expected) != 2:
        return None
    try:
        exp = [_parse_instant(str(expected[0])), _parse_instant(str(expected[1]))]
    except Exception:
        return None
    if not exp[0] < exp[1]:
        return "bounds_mismatch"
    # Literal TIMESTAMPTZ bounds encoded in the candidate SQL must match.
    lits = re.findall(r"TIMESTAMPTZ\s+'([^']+)'", cand_sql)
    if len(lits) >= 2:
        zone = verify.get("session_zone") or "UTC"
        try:
            got = []
            for lit in lits[:2]:
                naive = datetime.strptime(lit.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M:%S")
                zname = lit.rsplit(" ", 1)[1]
                got.append(naive.replace(tzinfo=ZoneInfo(zname)))
            if got[0] != exp[0] or got[1] != exp[1]:
                return "bounds_mismatch"
            forge.stats_extra["bounds_literal_checked_ok"] += 1
        except Exception:
            pass
    # Python recomputation cross-check (reported, not a gate).
    family = (row.get("meta") or {}).get("family", "")
    rec = recompute_bounds(family, verify.get("now_utc", ""), verify.get("session_zone", "UTC"))
    if rec is not None:
        zone = ZoneInfo(verify.get("session_zone", "UTC"))
        rec_inst = [datetime(r.year, r.month, r.day, tzinfo=zone) for r in rec]
        if rec_inst[0] == exp[0] and rec_inst[1] == exp[1]:
            forge.stats_extra["bounds_recompute_ok"] += 1
        else:
            forge.stats_extra["bounds_recompute_mismatch"] += 1
    return None


def inject_clock(sql: str, verify: dict) -> str:
    """Symmetric clock injection: CURRENT_TIMESTAMP / CURRENT_DATE become the
    row's fixture instant (session-zone local, plain quoted literals — the
    form the NEDB engine accepts)."""
    now_utc, zone = verify.get("now_utc"), verify.get("session_zone") or "UTC"
    if not now_utc:
        return sql
    try:
        local = _parse_instant(now_utc).astimezone(ZoneInfo(zone))
    except Exception:
        return sql
    ts = local.strftime("%Y-%m-%d %H:%M:%S")
    dt = local.strftime("%Y-%m-%d")
    sql = re.sub(r"\bCURRENT_TIMESTAMP\b", f"'{ts}'", sql, flags=re.I)
    sql = re.sub(r"\bCURRENT_DATE\b", f"'{dt}'", sql, flags=re.I)
    return sql


# ------------------------------------------------------------ family verifiers
def _is_write_sql(sql: str) -> bool:
    return bool(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", sql or "", re.I))


def _combine_tiers(tiers: list[str]) -> str:
    if "deterministic" in tiers:
        return "deterministic"
    if "execution" in tiers:
        return "execution"
    return "deterministic"


def verify_chain_calls(forge: Forge, row: dict, ex: dict, schema_key: str,
                       catalog: dict, tiers: list[str]) -> str | None:
    """Host simulation over extracted calls. Returns reject reason or None."""
    relay_by_idx = {ci: forge_relay_status(ex, j) for j, ci in enumerate([c[0] for c in ex["calls"]])}
    for j, (idx, name, args, _alias) in enumerate(ex["calls"]):
        status = relay_by_idx.get(idx)
        if name == "schema.inspect":
            table = args.get("table")
            if table and status != "error" and schema_key in CATALOG_DICTS:
                if table not in CATALOG_DICTS[schema_key]:
                    return "inspect_table"
        elif name == "nedb.provenance":
            table = args.get("table")
            if table and schema_key in CATALOG_DICTS and table not in CATALOG_DICTS[schema_key]:
                return "provenance_table"
        elif name == "clock.now":
            tz = args.get("timezone")
            if tz:
                try:
                    ZoneInfo(tz)
                except Exception:
                    return "clock_zone"
        elif name in ("sql.execute", "sql.explain"):
            sql = args.get("sql", "")
            if _is_write_sql(sql):
                return "write_in_toolcall"
            if status == "error":
                ok, _r, _k = check_parse(sql)
                if not ok:
                    return "gate_parse"
                ok, _r = check_safety(sql, _k)
                if not ok:
                    return "gate_safety"
                tiers.append("deterministic")
                continue
            ok, reason, tier = forge.verify_sql_vs_reference(schema_key, sql, None, catalog)
            if not ok:
                return reason
            tiers.append(tier)
    return None


def forge_relay_status(ex: dict, call_pos: int) -> str | None:
    if call_pos < len(ex["relays"]):
        return ex["relays"][call_pos][2]
    return None


def verify_verify_family(forge: Forge, row: dict):
    """discrimination / protocol / temporal rows (carry _verify).
    Returns (ok, reason, tier)."""
    verify = row.get("_verify") or {}
    meta = row.get("meta") or {}
    schema_key = verify.get("schema_key") or meta.get("schema_key") or ""
    ex = extract_row(row)
    if ex["problems"]:
        return False, ex["problems"][0], "deterministic"
    if ex["alias_used"]:
        forge.stats_extra["alias_normalised_rows"] += 1
    # Chain consistency vs _verify.chain.
    chain = verify.get("chain")
    if chain is not None:
        got = [{"name": n, "arguments": alias_normalise_args(n, a)} for (_i, n, a, _u) in ex["calls"]]
        want = [{"name": c.get("name"), "arguments": alias_normalise_args(c.get("name"), c.get("arguments") or {})}
                for c in chain]
        if got != want:
            return False, "chain_mismatch", "deterministic"
    # Catalog for this row.
    if schema_key in CATALOG_DICTS:
        catalog = CATALOG_DICTS[schema_key]
    elif verify.get("fixture_ddl"):
        catalog = {}
        for ddl in verify["fixture_ddl"]:
            catalog.update(catalog_from_ddl_text(ddl if ddl.strip().endswith(";") else ddl + ";"))
        if not catalog:
            catalog = catalog_from_ddl_text(";".join(verify["fixture_ddl"]))
    else:
        prompt = next((m["content"] for m in row["messages"] if m["role"] == "user"), "")
        catalog = catalog_from_ddl_text(prompt)
    tiers: list[str] = []
    reason = verify_chain_calls(forge, row, ex, schema_key, catalog, tiers)
    if reason:
        return False, reason, _combine_tiers(tiers)
    reference_sql = verify.get("reference_sql")
    is_temporal = meta.get("kind") == "temporal" or "now_utc" in verify
    # SQL envelopes.
    for (_i, kind, payload) in ex["envelopes"]:
        if kind != "SQL":
            continue
        sql = payload.strip()
        if _is_write_sql(sql):
            if any(n in ("sql.execute",) for (_j, n, _a, _u) in ex["calls"]):
                return False, "write_in_toolcall", _combine_tiers(tiers)
            ok, reason, tier = forge.verify_write(schema_key, sql, reference_sql, catalog)
            if not ok:
                return False, reason, tier
            tiers.append(tier)
            continue
        cand, ref = sql, reference_sql
        if is_temporal:
            cand = inject_clock(cand, verify)
            ref = inject_clock(ref, verify) if ref else None
            breason = temporal_bounds_check(forge, row, verify, sql)
            if breason:
                return False, breason, _combine_tiers(tiers)
        ok, reason, tier = forge.verify_sql_vs_reference(schema_key, cand, ref, catalog)
        if not ok:
            return False, reason, tier
        tiers.append(tier)
    # Summary grounding.
    facts = verify.get("summary_facts") or []
    if facts:
        payload_text = "\n".join(pl for (_i, _n, _s, pl) in ex["relays"])
        extra_text = ""
        for fact in facts:
            if str(fact) in payload_text:
                continue
            if not extra_text and reference_sql and schema_key in CATALOG_DICTS \
                    and not _is_write_sql(reference_sql):
                extra_text = forge.executed_rows_text(schema_key, reference_sql)
            if str(fact) not in extra_text:
                return False, "summary_grounding", _combine_tiers(tiers)
    if not tiers:
        tiers.append("deterministic")
    return True, "", _combine_tiers(tiers)


def verify_builder_row(forge: Forge, row: dict):
    """PR #29 builder rows: no _verify. Returns (ok, reason, tier)."""
    ex = extract_row(row)
    if ex["problems"]:
        return False, ex["problems"][0], "parse"
    if ex["alias_used"]:
        forge.stats_extra["alias_normalised_rows"] += 1
    prompt = next((m["content"] for m in row["messages"] if m["role"] == "user"), "")
    tables = prompt_table_set(prompt)
    match = [k for k, ts in CATALOG_TABLE_SETS.items() if ts == tables] if tables else []
    sqls: list[str] = []
    for (_i, kind, payload) in ex["envelopes"]:
        if kind == "SQL":
            sqls.append(payload.strip())
    for (_i, name, args, _u) in ex["calls"]:
        if name in ("sql.execute", "sql.explain"):
            sqls.append(args.get("sql", ""))
        if name in ("sql.execute",) and _is_write_sql(args.get("sql", "")):
            return False, "write_in_toolcall", "parse"
    tiers: list[str] = []
    if len(match) == 1:
        key = match[0]
        catalog = CATALOG_DICTS[key]
        for sql in sqls:
            if _is_write_sql(sql):
                continue  # write proposals: L0/L1-write + catalog bind below
            ok, reason, tier = forge.verify_sql_vs_reference(key, sql, None, catalog)
            if not ok:
                return False, reason, tier
            tiers.append(tier)
        for (_i, wsql, _auth) in ex["write_proposals"]:
            ok, reason, _k = check_parse(wsql)
            if not ok:
                return False, "gate_parse", "execution"
            ok, reason = check_safety_write(wsql, _k)
            if not ok:
                return False, "gate_safety", "execution"
            ok, reason = check_schema(wsql, catalog)
            if not ok:
                return False, "gate_bind", "execution"
            tiers.append("execution")
        tier = _combine_tiers(tiers) if tiers else "execution"
    else:
        for sql in sqls:
            if _is_write_sql(sql):
                ok, _r, kinds = check_parse(sql)
                if not ok:
                    return False, "gate_parse", "parse"
                ok, _r = check_safety_write(sql, kinds)
                if not ok:
                    return False, "gate_safety", "parse"
                continue
            ok, _r, kinds = check_parse(sql)
            if not ok:
                return False, "gate_parse", "parse"
            ok, _r = check_safety(sql, kinds)
            if not ok:
                return False, "gate_safety", "parse"
        tier = "parse"
    # Terminal-summary grounding: numbers/dates must appear in the row's own
    # TOOL_RESULT payloads or in the prompt (schema/question literals).
    if ex["summaries"]:
        summary = ex["summaries"][-1][1]
        payload_text = "\n".join(pl for (_i, _n, _s, pl) in ex["relays"])
        haystack = payload_text + "\n" + prompt
        tokens = set(re.findall(r"\d{4}-\d{2}-\d{2}|\d+(?:\.\d+)?", summary))
        for tok in tokens:
            if tok not in haystack:
                return False, "summary_grounding", tier
    return True, "", tier


# ------------------------------------------------------------ main
def load_rows(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", required=True,
                    choices=["builder", "temporal", "protocol", "discrimination"])
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rejects", required=True)
    ap.add_argument("--stats", required=True)
    ap.add_argument("--target", type=int, required=True, help="admitted rows (pairs when --pair-level)")
    ap.add_argument("--pair-level", action="store_true", help="admit/reject whole pairs (discrimination)")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    rows = load_rows(a.candidates)
    forge = Forge(a.family)
    stats = {
        "family": a.family, "seed": a.seed, "target": a.target,
        "unit": "pairs" if a.pair_level else "rows",
        "candidates": 0, "candidates_rows": len(rows), "admitted": 0,
        "admitted_rows": 0, "rejected": 0, "unprocessed": 0, "admit_rate": 0.0,
        "tiers": Counter(), "reject_reasons": Counter(),
    }
    admitted_rows: list[dict] = []
    reject_recs: list[dict] = []

    # Build processing units: single rows, or pairs grouped by meta.pair_id.
    units: list[list[dict]] = []
    if a.pair_level:
        i = 0
        while i < len(rows):
            pid = (rows[i].get("meta") or {}).get("pair_id")
            unit = [rows[i]]
            j = i + 1
            while j < len(rows) and (rows[j].get("meta") or {}).get("pair_id") == pid and pid is not None:
                unit.append(rows[j])
                j += 1
            units.append(unit)
            i = j
    else:
        units = [[r] for r in rows]

    verify_fn = verify_builder_row if a.family == "builder" else verify_verify_family
    processed_units = 0
    admitted_units = 0
    # Incremental writes: admitted/rejected rows are flushed as they are
    # decided, so an interrupted run never loses verified work.
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    out_fh = open(a.out, "w", encoding="utf-8")
    rej_fh = open(a.rejects, "w", encoding="utf-8")
    try:
        for unit in units:
            if admitted_units >= a.target:
                break
            processed_units += 1
            stats["candidates"] += 1
            unit_ok, unit_reason, unit_tier = True, "", None
            for r in unit:
                ok, reason, tier = verify_fn(forge, r)
                if not ok:
                    unit_ok, unit_reason = False, reason
                    break
                unit_tier = tier if unit_tier is None else _combine_tiers([unit_tier, tier])
            if unit_ok:
                admitted_units += 1
                stats["admitted_rows"] += len(unit)
                stats["tiers"][unit_tier] += len(unit)
                for r in unit:
                    out_fh.write(json.dumps({k: v for k, v in r.items() if k != "_verify"},
                                            ensure_ascii=False) + "\n")
            else:
                stats["rejected"] += 1
                stats["reject_reasons"][unit_reason] += 1
                for r in unit:
                    rej_fh.write(json.dumps({"meta": r.get("meta"), "reason": unit_reason},
                                            ensure_ascii=False) + "\n")
            if processed_units % 50 == 0:
                out_fh.flush()
                rej_fh.flush()
    finally:
        out_fh.close()
        rej_fh.close()
    stats["admitted"] = admitted_units
    stats["unprocessed"] = len(units) - processed_units
    done = admitted_units + stats["rejected"]
    stats["admit_rate"] = round(admitted_units / done, 4) if done else 0.0
    stats["tiers"] = dict(stats["tiers"])
    stats["reject_reasons"] = dict(stats["reject_reasons"])
    stats["extra"] = dict(forge.stats_extra)

    with open(a.stats, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2, ensure_ascii=False)
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
