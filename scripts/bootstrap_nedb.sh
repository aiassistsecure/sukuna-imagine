#!/usr/bin/env bash
# Bring up NEDB as the execution backend for the gate. Replaces bootstrap_pg.sh.
# Starts nedbd-v2 (Rust binary bundled in the nedb-engine pip package) with a
# PostgreSQL wire-protocol endpoint, then seeds the shop schema the gate tests
# expect. No postgres install, no root required, no socket paths.
set -euo pipefail

NEDB_DATA="${NEDB_DATA:-$PWD/nedb-data}"
NEDB_PG_PORT="${NEDB_PG_PORT:-5433}"
PGUSER_="${PGUSER_:-stealth}"

BIN="$(python3 -c "import nedb, os; print(os.path.join(os.path.dirname(nedb.__file__), 'nedbd-v2'))")"
if [ ! -x "$BIN" ]; then
  echo "nedbd-v2 binary not found (pip install nedb-engine)" >&2
  exit 1
fi

mkdir -p "$NEDB_DATA"
# kill any previous instance on this data dir
pkill -f "nedbd-v2.*$NEDB_DATA" 2>/dev/null || true
sleep 1

"$BIN" --data "$NEDB_DATA" --pg-port "$NEDB_PG_PORT" > "$PWD/nedb.log" 2>&1 &
echo $! > "$PWD/nedb.pid"

# wait for pgwire to accept connections
for i in $(seq 1 30); do
  if python3 -c "
import socket
s = socket.create_connection(('127.0.0.1', $NEDB_PG_PORT), timeout=2)
s.close()
" 2>/dev/null; then
    break
  fi
  sleep 1
done

# seed the shop schema the gate tests expect
# (create the database via HTTP first, then seed tables over pgwire)
python3 - "$NEDB_PG_PORT" "$PGUSER_" <<'EOF'
import sys, urllib.request, json, psycopg2
port, user = sys.argv[1], sys.argv[2]
# create the shop database via HTTP API
req = urllib.request.Request(f"http://127.0.0.1:7070/v1/databases",
                             data=json.dumps({"name": "shop"}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req, timeout=5)
except Exception as e:
    pass  # already exists
con = psycopg2.connect(f"host=127.0.0.1 port={port} user={user} dbname=shop")
con.autocommit = True
cur = con.cursor()
stmts = [
    "CREATE TABLE IF NOT EXISTS customers (id serial PRIMARY KEY, name text NOT NULL, city text, tier text, lifetime_value numeric(10,2))",
    "CREATE TABLE IF NOT EXISTS products (id serial PRIMARY KEY, title text NOT NULL, category text, price numeric(10,2), stock int)",
    "CREATE TABLE IF NOT EXISTS orders (id serial PRIMARY KEY, customer_id int, status text, total numeric(10,2), placed_at date)",
]
for s in stmts:
    try: cur.execute(s)
    except Exception: pass
# seed only if empty
cur.execute("SELECT count(*) FROM customers")
if cur.fetchone()[0] == 0:
    cur.execute("""INSERT INTO customers (name, city, tier, lifetime_value) VALUES
      ('Ada','Orlando','pro',1200.50),('Grace','Winter Park','free',80.00),
      ('Linus','Orlando','enterprise',9400.00),('Barbara','Maitland','pro',430.25)""")
    cur.execute("""INSERT INTO products (title, category, price, stock) VALUES
      ('Widget','hardware',19.99,100),('Gizmo','hardware',249.00,5),
      ('Manual','books',12.50,0),('Server','hardware',1899.00,2),
      ('Sticker','swag',3.00,500)""")
    cur.execute("""INSERT INTO orders (customer_id, status, total, placed_at) VALUES
      (1,'paid',249.00,'2026-01-05'),(1,'paid',19.99,'2026-02-11'),
      (2,'pending',12.50,'2026-02-14'),(3,'paid',1899.00,'2026-03-02'),
      (3,'refunded',3.00,'2026-03-09'),(4,'paid',430.25,'2026-04-01')""")
con.close()
print("shop schema seeded")
EOF

echo "NEDB pgwire on 127.0.0.1:$NEDB_PG_PORT (pid $(cat $PWD/nedb.pid))"
echo "DSN: host=127.0.0.1 port=$NEDB_PG_PORT user=$PGUSER_ dbname=postgres"
