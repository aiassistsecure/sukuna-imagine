"""stealth :: schema catalog

THE most important design decision in this project lives here.

The previous model (nedb-cast-slm) memorised six synthetic domains. Name your
collection `purchases` with a `cost` column and it would still emit
`FROM orders WHERE total`, because it had learned the schema rather than
learning to READ one. Fine for a demo, fatal for a product.

So: train across MANY schemas, with deliberately colliding and deliberately
weird naming, so the only strategy that survives is "read the schema you were
handed". Schema goes in the prompt, never in the weights.

Each schema is real DDL, created in a real PostgreSQL database and seeded with
real rows, because the gate needs something to execute against.

Deliberate adversarial properties across the catalog:
  * the SAME concept named differently   (orders/purchases/transactions)
  * the SAME name meaning different things (`total` = money here, count there)
  * snake_case, camelCase and quoted "Mixed Case" identifiers
  * reserved-ish words that must be quoted ("order", "user", "group")
  * nullable columns, so COUNT(col) != COUNT(*) is a real distinction
  * one table with no rows at all, so "zero results" is a trained-for answer
"""
from __future__ import annotations

from dataclasses import dataclass, field


# SQL keywords that are legal-looking identifiers but blow up unquoted. Not
# exhaustive -- these are the ones the catalog deliberately uses, because a
# model that never sees a quoted identifier will never emit one.
RESERVED = {
    "order", "user", "group", "table", "select", "from", "where", "limit",
    "offset", "default", "check", "column", "constraint", "primary", "references",
    "desc", "asc", "all", "any", "case", "end", "to", "union", "using", "window",
}


def _needs_quoting(name: str) -> bool:
    bare = name.replace("_", "")
    if not bare.isalnum():          # spaces, punctuation
        return True
    if not name.islower():          # camelCase / Mixed Case
        return True
    if name.lower() in RESERVED:    # reserved word
        return True
    if name[:1].isdigit():          # leading digit
        return True
    return False


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    note: str = ""

    @property
    def quoted(self) -> str:
        return f'"{self.name}"' if _needs_quoting(self.name) else self.name


@dataclass(frozen=True)
class Table:
    name: str
    columns: list[Column]
    rows: list[tuple]
    primary_key: str | None = "id"

    @property
    def quoted(self) -> str:
        return f'"{self.name}"' if _needs_quoting(self.name) else self.name


@dataclass(frozen=True)
class Schema:
    key: str
    domain: str
    tables: list[Table]
    notes: str = ""
    tags: list[str] = field(default_factory=list)

    # ---- DDL ------------------------------------------------------------
    def ddl(self) -> list[str]:
        out = []
        for t in self.tables:
            cols = ", ".join(f"{c.quoted} {c.type}" for c in t.columns)
            out.append(f"CREATE TABLE {t.quoted} ({cols});")
        return out

    def seed(self) -> list[tuple[str, list[tuple]]]:
        """(INSERT template, rows) per table. Empty tables are skipped.

        `serial` columns are EXCLUDED from the insert: the database generates
        them. Seed rows therefore carry only the non-serial columns, and a
        mismatch between the two is a schema-definition bug, so it is asserted
        here rather than surfacing as a confusing driver error at insert time.
        """
        out = []
        for t in self.tables:
            if not t.rows:
                continue
            insertable = [c for c in t.columns if "serial" not in c.type.lower()]
            width = len(insertable)
            for i, row in enumerate(t.rows):
                if len(row) != width:
                    raise ValueError(
                        f"{self.key}.{t.name} row {i} has {len(row)} values but "
                        f"{width} insertable columns "
                        f"({[c.name for c in insertable]}) — serial columns are "
                        f"auto-generated and must be omitted from seed rows")
            cols = ", ".join(c.quoted for c in insertable)
            ph = ", ".join(["%s"] * width)
            out.append((f"INSERT INTO {t.quoted} ({cols}) VALUES ({ph})", t.rows))
        return out

    # ---- the prompt-side view -------------------------------------------
    def prompt_text(self, style: str = "ddl") -> str:
        """How the schema is shown to the model.

        Two styles on purpose. A model trained on only one presentation learns
        the presentation as much as the content; varying it forces the skill to
        be "read a schema" rather than "parse our particular format".
        """
        if style == "ddl":
            lines = []
            for t in self.tables:
                cols = ",\n  ".join(
                    f"{c.quoted} {c.type}" + (f"  -- {c.note}" if c.note else "")
                    for c in t.columns)
                lines.append(f"CREATE TABLE {t.quoted} (\n  {cols}\n);")
            return "\n\n".join(lines)
        if style == "compact":
            return "\n".join(
                f"{t.quoted}(" + ", ".join(f"{c.quoted}:{c.type}" for c in t.columns) + ")"
                for t in self.tables)
        raise ValueError(f"unknown schema style: {style!r}")


def _C(n, t, note=""):
    return Column(n, t, note)


# ---------------------------------------------------------------------------
# The catalog. Small on purpose right now -- correctness of the pipeline first,
# breadth second. Every schema here is executable and seeded.
# ---------------------------------------------------------------------------

SHOP = Schema(
    key="shop", domain="e-commerce",
    tags=["classic", "snake_case"],
    notes="The canonical example. Deliberately the most conventional schema "
          "in the catalog so we can measure how much the model leans on it.",
    tables=[
        Table("customers", [
            _C("id", "serial PRIMARY KEY"), _C("name", "text NOT NULL"),
            _C("city", "text"), _C("tier", "text", "free|pro|enterprise"),
            _C("lifetime_value", "numeric(10,2)"),
        ], [("Ada", "Orlando", "pro", 1200.50), ("Grace", "Winter Park", "free", 80.00),
            ("Linus", "Orlando", "enterprise", 9400.00), ("Barbara", "Maitland", "pro", 430.25)]),
        Table("products", [
            _C("id", "serial PRIMARY KEY"), _C("title", "text NOT NULL"),
            _C("category", "text"), _C("price", "numeric(10,2)"), _C("stock", "int"),
        ], [("Widget", "hardware", 19.99, 100), ("Gizmo", "hardware", 249.00, 5),
            ("Manual", "books", 12.50, 0), ("Server", "hardware", 1899.00, 2),
            ("Sticker", "swag", 3.00, 500)]),
        Table("orders", [
            _C("id", "serial PRIMARY KEY"), _C("customer_id", "int"),
            _C("status", "text", "paid|pending|refunded"),
            _C("total", "numeric(10,2)", "money"), _C("placed_at", "date"),
        ], [(1, "paid", 249.00, "2026-01-05"), (1, "paid", 19.99, "2026-02-11"),
            (2, "pending", 12.50, "2026-02-14"), (3, "paid", 1899.00, "2026-03-02"),
            (3, "refunded", 3.00, "2026-03-09"), (4, "paid", 430.25, "2026-04-01")]),
    ],
)

CLINIC = Schema(
    key="clinic", domain="healthcare scheduling",
    tags=["same-concept-different-name", "nullable"],
    notes="`appointments` is the orders-analogue under a different name, and "
          "`duration_min` is an int where shop's `total` is money -- so a model "
          "that memorised 'total means money' has to actually look.",
    tables=[
        Table("patients", [
            _C("id", "serial PRIMARY KEY"), _C("full_name", "text NOT NULL"),
            _C("dob", "date"), _C("insurer", "text", "NULLABLE - uninsured patients"),
        ], [("Rosa Diaz", "1988-04-02", "Aetna"), ("Amir Khan", "1975-11-30", None),
            ("Mei Chen", "1999-07-19", "BlueCross"), ("Tom Ford", "1960-01-15", None)]),
        Table("practitioners", [
            _C("id", "serial PRIMARY KEY"), _C("name", "text NOT NULL"),
            _C("speciality", "text"), _C("hourly_rate", "numeric(8,2)"),
        ], [("Dr Vale", "cardiology", 320.00), ("Dr Okoro", "dermatology", 210.00),
            ("Dr Singh", "cardiology", 295.00)]),
        Table("appointments", [
            _C("id", "serial PRIMARY KEY"), _C("patient_id", "int"),
            _C("practitioner_id", "int"), _C("state", "text", "booked|attended|no_show"),
            _C("duration_min", "int", "MINUTES, not money"), _C("scheduled_for", "date"),
        ], [(1, 1, "attended", 45, "2026-02-03"), (2, 2, "no_show", 30, "2026-02-04"),
            (3, 1, "attended", 60, "2026-02-10"), (1, 3, "booked", 30, "2026-05-01"),
            (4, 2, "attended", 15, "2026-03-22")]),
    ],
)

LIBRARY = Schema(
    key="library", domain="lending",
    tags=["reserved-words", "quoted-identifiers", "empty-table"],
    notes='Uses "order" and "Mixed Case" identifiers that MUST be quoted, and '
          "ships an empty table so 'zero rows' is a trained-for answer rather "
          "than a surprise.",
    tables=[
        Table("books", [
            _C("id", "serial PRIMARY KEY"), _C("title", "text NOT NULL"),
            _C("author", "text"), _C("Shelf Code", "text", "quoted identifier"),
            _C("copies", "int"),
        ], [("Dune", "Herbert", "SF-01", 4), ("SICP", "Abelson", "CS-11", 2),
            ("Ficciones", "Borges", "LIT-03", 1), ("TAOCP", "Knuth", "CS-01", 0)]),
        Table("members", [
            _C("id", "serial PRIMARY KEY"), _C("name", "text NOT NULL"),
            _C("joined", "date"), _C("fines_owed", "numeric(6,2)"),
        ], [("Iris", "2024-03-01", 0.00), ("Jonah", "2025-06-15", 12.40),
            ("Kalu", "2026-01-09", 0.00)]),
        Table("loans", [
            _C("id", "serial PRIMARY KEY"), _C("book_id", "int"),
            _C("member_id", "int"), _C("order", "int", "RESERVED WORD - queue position"),
            _C("returned", "boolean"),
        ], [(1, 1, 1, True), (2, 2, 1, False), (3, 1, 2, True), (4, 3, 1, False)]),
        # Deliberately empty.
        Table("reservations", [
            _C("id", "serial PRIMARY KEY"), _C("book_id", "int"),
            _C("member_id", "int"), _C("created", "date"),
        ], []),
    ],
)

FLEET = Schema(
    key="fleet", domain="logistics",
    tags=["camelCase", "colliding-names"],
    notes="camelCase identifiers throughout (so they must be quoted), and a "
          "`total` column that means a COUNT of packages -- directly colliding "
          "with shop.orders.total meaning money.",
    tables=[
        Table("vehicles", [
            _C("id", "serial PRIMARY KEY"), _C("plate", "text NOT NULL"),
            _C("vehicleType", "text", "van|truck|bike"), _C("capacityKg", "int"),
        ], [("FL-100", "van", 900), ("FL-220", "truck", 7500),
            ("FL-330", "bike", 25), ("FL-440", "van", 900)]),
        Table("routes", [
            _C("id", "serial PRIMARY KEY"), _C("vehicleId", "int"),
            _C("region", "text"), _C("total", "int", "COUNT of packages, NOT money"),
            _C("runDate", "date"),
        ], [(1, "central", 42, "2026-03-01"), (2, "north", 310, "2026-03-01"),
            (1, "central", 38, "2026-03-02"), (3, "downtown", 9, "2026-03-02"),
            (4, "south", 77, "2026-03-03")]),
    ],
)

TELEMETRY = Schema(
    key="telemetry", domain="observability",
    tags=["timestamps", "wide-numeric", "nullable"],
    notes="Timestamps rather than dates, and a nullable metric so COUNT(col) "
          "genuinely differs from COUNT(*) -- a distinction the gate can prove "
          "and a lazy model will get wrong.",
    tables=[
        Table("hosts", [
            _C("id", "serial PRIMARY KEY"), _C("hostname", "text NOT NULL"),
            _C("region", "text"), _C("cores", "int"),
        ], [("alpha", "us-east", 8), ("beta", "us-east", 16),
            ("gamma", "eu-west", 4), ("delta", "ap-south", 32)]),
        Table("samples", [
            _C("id", "serial PRIMARY KEY"), _C("host_id", "int"),
            _C("taken_at", "timestamp"), _C("cpu_pct", "numeric(5,2)"),
            _C("mem_pct", "numeric(5,2)", "NULLABLE - agent sometimes omits it"),
        ], [(1, "2026-03-01 00:00:00", 12.50, 40.10), (1, "2026-03-01 01:00:00", 88.20, None),
            (2, "2026-03-01 00:00:00", 5.00, 22.00), (3, "2026-03-01 00:00:00", 97.75, 91.30),
            (3, "2026-03-01 01:00:00", 99.10, None), (4, "2026-03-01 00:00:00", 44.00, 60.00)]),
    ],
)

CATALOG: dict[str, Schema] = {
    s.key: s for s in (SHOP, CLINIC, LIBRARY, FLEET, TELEMETRY)
}

# Held out from training entirely. Generalisation is measured here: if the
# model only works on schemas it was trained on, it memorised and we shipped
# cast again.
HELDOUT_KEYS = ("telemetry",)
TRAIN_KEYS = tuple(k for k in CATALOG if k not in HELDOUT_KEYS)


def get(key: str) -> Schema:
    return CATALOG[key]


def all_schemas(include_heldout: bool = True):
    for k, s in CATALOG.items():
        if include_heldout or k not in HELDOUT_KEYS:
            yield s
