"""stealth :: L2 schema validation against the gate's own catalog.

NEDB is schemaless by design, so EXPLAIN cannot catch hallucinated tables,
hallucinated columns, ambiguous references, or type errors -- it happily plans
all of them. The gate therefore carries its own schema catalog and validates
the parse tree against it BEFORE any row is read.

Deliberately separate from the engine: the engine stays schemaless, the gate
stays strict. A query dies here in PostgreSQL's own vocabulary
('column "x" does not exist'), which is exactly what a repair loop wants
handed back.

Scope is kept narrow on purpose:
  - every table in FROM exists (catalog table or an in-query CTE)
  - every column reference resolves to exactly one table in scope
  - string literals compared against date/int/numeric columns are valid
    literals of that type ('not-a-date' dies; '2026-01-01' lives)
  - incompatible column-vs-column comparisons die (int = text)

Anything the walker does not understand is SKIPPED, not rejected. R2: a gate
that rejects correct SQL starves the corpus of the examples worth learning
from, and nothing downstream would ever report it.
"""
from __future__ import annotations

from datetime import datetime, date

from pglast import ast, parse_sql


class SchemaError(Exception):
    """A query that fails L2 schema validation. str(e) is repair-loop feedback."""


# ------------------------------------------------------------------ catalog --

# Canonical shop schema (mirrors scripts/bootstrap_nedb.sh seed).
# Types are the gate's own small lattice: int, numeric, text, date, bool,
# timestamp, unknown.
SHOP_SCHEMA: dict[str, dict[str, str]] = {
    "customers": {
        "id": "int",
        "name": "text",
        "city": "text",
        "tier": "text",
        "lifetime_value": "numeric",
    },
    "products": {
        "id": "int",
        "title": "text",
        "category": "text",
        "price": "numeric",
        "stock": "int",
    },
    "orders": {
        "id": "int",
        "customer_id": "int",
        "status": "text",
        "total": "numeric",
        "placed_at": "date",
    },
}

_COMPARISONS = {"=", "<", ">", "<=", ">=", "<>", "!="}

_DATE_KEYWORDS = {"now", "today", "yesterday", "tomorrow", "epoch",
                  "infinity", "-infinity"}
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%b-%Y",
                 "%b %d %Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S")

_PG_TYPE_MAP = {
    "int2": "int", "int4": "int", "int8": "int",
    "integer": "int", "bigint": "int", "smallint": "int", "serial": "int",
    "numeric": "numeric", "decimal": "numeric",
    "float4": "numeric", "float8": "numeric", "real": "numeric",
    "double precision": "numeric",
    "text": "text", "varchar": "text", "char": "text", "bpchar": "text",
    "character varying": "text", "character": "text",
    "date": "date",
    "timestamp": "timestamp", "timestamptz": "timestamp",
    "bool": "bool", "boolean": "bool",
}


def _valid_date_literal(s: str) -> bool:
    s = s.strip()
    if s.lower() in _DATE_KEYWORDS:
        return True
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            pass
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _valid_numeric_literal(s: str) -> bool:
    try:
        float(s.strip())
        return True
    except ValueError:
        return False


# ------------------------------------------------------------ tree walking --

def _children(node):
    """Yield child AST nodes. Cycle-safe via the caller's seen-set."""
    for attr in dir(node):
        if attr.startswith("_"):
            continue
        try:
            v = getattr(node, attr)
        except Exception:
            continue
        if isinstance(v, ast.Node):
            yield v
        elif isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, ast.Node):
                    yield x


def _field_names(colref):
    """ColumnRef fields -> list[str], or None for `*` / unrecognized shapes."""
    fields = []
    for f in colref.fields or []:
        if type(f).__name__ == "A_Star":
            return None
        if type(f).__name__ == "String":
            fields.append(f.sval)
        else:
            return None
    return fields


def _is_plain_column(node) -> bool:
    return (type(node).__name__ == "ColumnRef"
            and _field_names(node) is not None
            and 1 <= len(_field_names(node)) <= 2)


def _is_string_const(node) -> bool:
    return (type(node).__name__ == "A_Const"
            and node.val is not None
            and type(node.val).__name__ == "String")


def _resolve_column(fields, scope, from_tables, aliases):
    """Resolve a column reference. Returns its type. Raises SchemaError."""
    if fields is None:
        return "unknown"                       # `*`: nothing to resolve
    if len(fields) == 2:
        tname, cname = fields
        if tname not in scope:
            raise SchemaError(f'missing FROM-clause entry for table "{tname}"')
        cols = scope[tname]
        if cname not in cols:
            raise SchemaError(f'column "{cname}" does not exist in table "{tname}"')
        return cols[cname]
    if len(fields) == 1:
        cname = fields[0]
        if aliases and cname in aliases:
            return "unknown"                   # SELECT output alias
        hits = [t for t in from_tables if cname in scope.get(t, {})]
        if not hits:
            raise SchemaError(f'column "{cname}" does not exist')
        if len(hits) > 1:
            raise SchemaError(f'column "{cname}" is ambiguous ({", ".join(hits)})')
        return scope[hits[0]][cname]
    # db.schema.table or deeper: beyond this checker's scope; EXPLAIN is the backstop
    return "unknown"


def _pg_type_name(type_name) -> str:
    names = [s.sval.lower() for s in (type_name.names or [])
             if type(s).__name__ == "String"]
    if not names:
        return "unknown"
    return _PG_TYPE_MAP.get(names[-1], "unknown")


def _infer_type(node, scope, from_tables, aliases):
    t = type(node).__name__
    if t == "ColumnRef":
        try:
            return _resolve_column(_field_names(node), scope, from_tables, aliases)
        except SchemaError:
            # The ColumnRef branch of _walk_expr reports this with full context.
            return "unknown"
    if t == "A_Const":
        v = node.val
        vt = type(v).__name__ if v is not None else "Null"
        return {"String": "text", "Integer": "int", "Float": "numeric"}.get(vt, "unknown")
    if t == "TypeCast":
        return _pg_type_name(node.typeName)
    if t == "FuncCall":
        fn = "".join(getattr(s, "sval", "") for s in (node.funcname or [])
                     ).lower().split(".")[-1]
        if fn == "count":
            return "int"
        if fn in ("sum", "avg"):
            return "numeric"
        return "unknown"
    return "unknown"


def _compatible(t1: str, t2: str) -> bool:
    if t1 == "unknown" or t2 == "unknown":
        return True
    if t1 == t2:
        return True
    if {t1, t2} <= {"int", "numeric"}:
        return True
    if {t1, t2} == {"date", "timestamp"}:
        return True
    return False


def _check_comparison(node, scope, from_tables, aliases):
    op = "".join(getattr(s, "sval", "") for s in (node.name or []))
    if op not in _COMPARISONS:
        return
    lexpr, rexpr = node.lexpr, node.rexpr
    # Literal-vs-column first: precise, actionable feedback for the repair loop.
    for colnode, constnode in ((lexpr, rexpr), (rexpr, lexpr)):
        if _is_plain_column(colnode) and _is_string_const(constnode):
            ctype = _resolve_column(_field_names(colnode), scope, from_tables, aliases)
            lit = constnode.val.sval
            cname = _field_names(colnode)[-1]
            if ctype == "date" and not _valid_date_literal(lit):
                raise SchemaError(f'type error: "{lit}" is not a valid date '
                                  f'for column "{cname}"')
            if ctype in ("int", "numeric") and not _valid_numeric_literal(lit):
                raise SchemaError(f'type error: "{lit}" is not a valid {ctype} '
                                  f'for column "{cname}"')
            return                             # literal vetted; nothing more to prove
    lt = _infer_type(lexpr, scope, from_tables, aliases)
    rt = _infer_type(rexpr, scope, from_tables, aliases)
    if not _compatible(lt, rt):
        raise SchemaError(f"type error: cannot compare {lt} with {rt}")


def _walk_expr(node, scope, from_tables, aliases, catalog, seen=None):
    if node is None:
        return
    if seen is None:
        seen = set()
    if isinstance(node, (list, tuple)):
        for x in node:
            _walk_expr(x, scope, from_tables, aliases, catalog, seen)
        return
    if not isinstance(node, ast.Node):
        return
    if id(node) in seen:
        return
    seen.add(id(node))

    t = type(node).__name__
    if t == "ColumnRef":
        _resolve_column(_field_names(node), scope, from_tables, aliases)
        return                                # fields are Strings; nothing below
    if t == "A_Expr":
        _check_comparison(node, scope, from_tables, aliases)
    skip = set()
    if t == "SubLink":
        sub = getattr(node, "subselect", None)
        if sub is not None and type(sub).__name__ == "SelectStmt":
            _validate_select(sub, catalog, dict(scope))
            skip.add(id(sub))
    for ch in _children(node):
        if id(ch) not in skip:
            _walk_expr(ch, scope, from_tables, aliases, catalog, seen)


# --------------------------------------------------------------- structure --

def _process_from_item(item, catalog, scope, from_tables):
    t = type(item).__name__
    if t == "RangeVar":
        name = item.relname
        key = item.alias.aliasname if item.alias is not None else name
        if key in from_tables:
            raise SchemaError(f'table name "{key}" specified more than once')
        if name in scope:
            cols = scope[name]                # CTE shadows catalog
        elif name in catalog:
            cols = catalog[name]
        else:
            raise SchemaError(f'relation "{name}" does not exist')
        scope[key] = cols
        from_tables.append(key)
    elif t == "JoinExpr":
        _process_from_item(item.larg, catalog, scope, from_tables)
        _process_from_item(item.rarg, catalog, scope, from_tables)
        _walk_expr(item.quals, scope, from_tables, None, catalog)
    elif t == "RangeSubselect":
        alias = item.alias.aliasname if item.alias is not None else None
        if not alias:
            raise SchemaError("subquery in FROM must have an alias")
        if alias in from_tables:
            raise SchemaError(f'table name "{alias}" specified more than once')
        _, out_cols = _validate_select(item.subquery, catalog, dict(scope))
        scope[alias] = out_cols
        from_tables.append(alias)
    else:
        # RangeFunction and friends: walk for column refs, register nothing.
        _walk_expr(item, scope, from_tables, None, catalog)


def _output_columns(stmt, scope, from_tables):
    """Column name -> type for a validated SELECT's output."""
    cols: dict[str, str] = {}
    for rt in stmt.targetList or []:
        val = rt.val
        if type(val).__name__ == "ColumnRef" and _field_names(val) is None:
            for t in from_tables:             # `*`: expand in FROM order
                for c, ty in scope.get(t, {}).items():
                    cols.setdefault(c, ty)
            continue
        name = getattr(rt, "name", None)
        if not name and type(val).__name__ == "FuncCall":
            fn = val.funcname or []
            name = fn[-1].sval.lower() if fn else None   # PG names it e.g. "count"
        if not name and type(val).__name__ == "ColumnRef":
            fields = _field_names(val)
            if fields:
                name = fields[-1]
                cols[name] = _resolve_column(fields, scope, from_tables, None)
                continue
        if name:
            cols.setdefault(name, "unknown")
    return cols


def _validate_select(stmt, catalog, outer_scope):
    """Returns (scope, output_columns). Raises SchemaError."""
    if stmt.larg is not None or stmt.rarg is not None:
        # UNION / INTERSECT / EXCEPT: validate both branches; output = left's.
        _, out = _validate_select(stmt.larg, catalog, outer_scope)
        _validate_select(stmt.rarg, catalog, outer_scope)
        return dict(outer_scope), out

    scope = dict(outer_scope)
    with_clause = getattr(stmt, "withClause", None)
    if with_clause is not None:
        for cte in with_clause.ctes or []:
            _, out_cols = _validate_select(cte.ctequery, catalog, scope)
            scope[cte.ctename] = out_cols

    from_tables: list[str] = []
    for item in stmt.fromClause or []:
        _process_from_item(item, catalog, scope, from_tables)

    aliases: dict[str, str] = {}
    for rt in stmt.targetList or []:
        if getattr(rt, "name", None):
            aliases[rt.name] = "unknown"

    # SELECT aliases are NOT visible in the select list itself or in WHERE.
    _walk_expr(stmt.targetList, scope, from_tables, None, catalog)
    _walk_expr(stmt.whereClause, scope, from_tables, None, catalog)
    # ... but they ARE visible to GROUP BY / HAVING / ORDER BY.
    _walk_expr(stmt.groupClause, scope, from_tables, aliases, catalog)
    _walk_expr(stmt.havingClause, scope, from_tables, aliases, catalog)
    _walk_expr(stmt.sortClause, scope, from_tables, aliases, catalog)

    return scope, _output_columns(stmt, scope, from_tables)


# ------------------------------------------------------------------ entry --

def check_schema(sql: str, catalog: dict | None = None) -> tuple[bool, str]:
    """Validate sql against catalog. Returns (ok, reason)."""
    catalog = SHOP_SCHEMA if catalog is None else catalog
    try:
        tree = parse_sql(sql)
    except Exception as e:
        return False, f"parse failed in schema check: {e}"
    if not tree:
        return False, "no statements"
    try:
        _validate_statement(tree[0].stmt, catalog)
    except SchemaError as e:
        return False, str(e)
    return True, "ok"


def _validate_statement(stmt, catalog):
    if type(stmt).__name__ == "SelectStmt":
        _validate_select(stmt, catalog, {})
    elif type(stmt).__name__ == "InsertStmt":
        _validate_insert(stmt, catalog)
    elif type(stmt).__name__ == "UpdateStmt":
        _validate_update(stmt, catalog)
    elif type(stmt).__name__ == "DeleteStmt":
        _validate_delete(stmt, catalog)
    # Anything else was already stopped at L1; stay permissive here.


def _write_target(stmt, catalog) -> tuple[str, dict, list]:
    """Resolve the target table of a write. Returns (name, cols, from_tables)."""
    rel = getattr(stmt, "relation", None)
    name = getattr(rel, "relname", None) if rel is not None else None
    if not name or name not in catalog:
        raise SchemaError(f'relation "{name}" does not exist')
    return name, catalog[name], [name]


def _validate_insert(stmt, catalog):
    name, cols, from_tables = _write_target(stmt, catalog)
    scope = {name: cols}
    for res in stmt.cols or []:
        col = getattr(res, "name", None)
        if col and col not in cols:
            raise SchemaError(f'column "{col}" of relation "{name}" does not exist')
    # INSERT ... SELECT: validate the source query too.
    sel = getattr(stmt, "selectStmt", None)
    if sel is not None and type(sel).__name__ == "SelectStmt":
        _validate_select(sel, catalog, {})


def _validate_update(stmt, catalog):
    name, cols, from_tables = _write_target(stmt, catalog)
    scope = {name: cols}
    for res in stmt.targetList or []:
        col = getattr(res, "name", None)
        if col and col not in cols:
            raise SchemaError(f'column "{col}" of relation "{name}" does not exist')
        _walk_expr(getattr(res, "val", None), scope, from_tables, None, catalog)
    _walk_expr(getattr(stmt, "whereClause", None), scope, from_tables, None, catalog)


def _validate_delete(stmt, catalog):
    name, cols, from_tables = _write_target(stmt, catalog)
    scope = {name: cols}
    _walk_expr(getattr(stmt, "whereClause", None), scope, from_tables, None, catalog)
