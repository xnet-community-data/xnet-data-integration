#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DUNE_API_KEY:-}" ]]; then
  echo "ERROR: DUNE_API_KEY is not set"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$ROOT/state"
mkdir -p "$STATE_DIR"

POOL_SQL="$ROOT/dune/v3/sql/10_market_pool_base.sql"
SNAP_SQL="$ROOT/dune/v3/sql/11_market_snapshot.sql"

POOL_STATE="$STATE_DIR/v3_market_pool_query.json"
SNAP_STATE="$STATE_DIR/v3_market_snapshot_query.json"

json_id () {
  python3 -c 'import json,sys; p=json.load(sys.stdin); print(p.get("query_id") or p.get("id") or "")'
}

if [[ ! -s "$POOL_STATE" ]]; then
  echo "Creating private V3 Market Pool Base query..."
  dune query create \
    --name "XNET V3 — Market Pool Base" \
    --description "Canonical DexScreener pool-level source. This is the only V3 query that should call DexScreener." \
    --private \
    --sql "$(cat "$POOL_SQL")" \
    -o json | tee "$POOL_STATE"
fi

POOL_ID="$(cat "$POOL_STATE" | json_id)"
if [[ -z "$POOL_ID" ]]; then
  echo "ERROR: could not parse Market Pool Base query id"
  cat "$POOL_STATE"
  exit 1
fi

echo "Market Pool Base query id: $POOL_ID"

if ! dune matview get dune.xnet_community_data.result_xnet_v3_market_pools_live -o json >/dev/null 2>&1; then
  echo "Creating 15-minute materialized Market Pool Base..."
  dune matview create \
    --name result_xnet_v3_market_pools_live \
    --query-id "$POOL_ID" \
    --private \
    --performance small \
    --cron "*/15 * * * *" \
    -o json
else
  echo "Market pool materialized view already exists."
fi

echo "Refreshing market pool materialized view once now..."
dune matview refresh dune.xnet_community_data.result_xnet_v3_market_pools_live \
  --performance small \
  -o json

if [[ ! -s "$SNAP_STATE" ]]; then
  echo "Creating private V3 Market Snapshot query..."
  dune query create \
    --name "XNET V3 — Market Snapshot" \
    --description "Tiny presentation/derived query over the canonical 15-minute market-pool materialized view." \
    --private \
    --sql "$(cat "$SNAP_SQL")" \
    -o json | tee "$SNAP_STATE"
fi

SNAP_ID="$(cat "$SNAP_STATE" | json_id)"
if [[ -z "$SNAP_ID" ]]; then
  echo "ERROR: could not parse Market Snapshot query id"
  cat "$SNAP_STATE"
  exit 1
fi

echo
echo "=== RUNNING MARKET SNAPSHOT QA ==="
dune query run "$SNAP_ID" --limit 10 -o json

echo
echo "=== MARKET BASE BOOTSTRAP COMPLETE ==="
echo "Pool query:     $POOL_ID"
echo "Pool matview:   dune.xnet_community_data.result_xnet_v3_market_pools_live"
echo "Snapshot query: $SNAP_ID"
