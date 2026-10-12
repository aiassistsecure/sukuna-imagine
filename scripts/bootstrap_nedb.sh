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

# seed all schemas (training + held-out) via the forge's materialise,
# so the gate tests AND the eval harness find their databases.
# (The eval's --materialise flag remains as a manual fallback.)
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from stealth.forge import materialise
from stealth.schemas import CATALOG, TRAIN_KEYS, HELDOUT_KEYS

admin_dsn = "host=127.0.0.1 port=5433 user=stealth dbname=postgres"
for key in list(TRAIN_KEYS) + list(HELDOUT_KEYS):
    dbname = materialise(CATALOG[key], admin_dsn)
    print(f"  materialised {key} -> {dbname}")
EOF

echo "NEDB pgwire on 127.0.0.1:$NEDB_PG_PORT (pid $(cat $PWD/nedb.pid))"
echo "DSN: host=127.0.0.1 port=$NEDB_PG_PORT user=$PGUSER_ dbname=postgres"
