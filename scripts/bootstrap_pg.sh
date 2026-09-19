#!/usr/bin/env bash
# Bring up a throwaway PostgreSQL for the gate. Durability is OFF on purpose:
# this database exists to be asked questions, never to survive a crash.
set -euo pipefail
PGDATA="${PGDATA:-$PWD/pgdata}"
PGSOCK="${PGSOCK:-$PWD/pgrun}"
PGUSER_="${PGUSER_:-stealth}"

mkdir -p "$PGSOCK"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  initdb -D "$PGDATA" -U "$PGUSER_" --auth=trust -E UTF8 >/dev/null
fi
pg_ctl -D "$PGDATA" -l "$PWD/pg.log" -o \
  "-k $PGSOCK -h '' -c fsync=off -c synchronous_commit=off -c full_page_writes=off" \
  start >/dev/null 2>&1 || true
sleep 2
psql -h "$PGSOCK" -U "$PGUSER_" -d postgres -c "select version();" -t
echo "DSN: host=$PGSOCK user=$PGUSER_ dbname=postgres"
